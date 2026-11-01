"""Conservative bitmap sampling bounds for Illustrator-generated SVGs.

No images are rendered and no external URLs are opened. Unsupported geometry
returns factor 1. The result bounds source-pixel enlargement; it is a heuristic
for the native EMF export cost, not a guarantee about Illustrator's renderer.
"""
import base64
import binascii
import math
import re
import struct


IDENTITY = (1.0, 0.0, 0.0, 1.0)  # SVG's a,b,c,d linear matrix coefficients.
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER)
_TRANSFORM_RE = re.compile(r"([A-Za-z]+)\s*\(([^()]*)\)")
_GEOMETRY_CSS = re.compile(
    r"(?:^|[;{])\s*(?:-[a-z]+-)?(?:transform(?:-origin|-box)?|width|height|x|y|zoom|"
    r"scale|rotate|translate|perspective(?:-origin)?|offset(?:-[a-z]+)?|all)\s*:|@import",
    re.IGNORECASE,
)


class UnknownGeometry(ValueError):
    """The SVG is outside the deliberately supported Illustrator subset."""


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _numbers(text):
    values, end = [], 0
    for match in _NUMBER_RE.finditer(text):
        if text[end:match.start()].strip(" \t\r\n,"):
            raise UnknownGeometry("invalid_number_list")
        values.append(float(match.group()))
        end = match.end()
    if text[end:].strip(" \t\r\n,") or not all(math.isfinite(x) for x in values):
        raise UnknownGeometry("nonfinite_or_invalid_numbers")
    return values


def _multiply(left, right):
    a, b, c, d = left
    e, f, g, h = right
    result = (a*e + c*f, b*e + d*f, a*g + c*h, b*g + d*h)
    if not all(math.isfinite(x) for x in result):
        raise UnknownGeometry("transform_overflow")
    return result


def _transform(text):
    """Only the linear part matters for sampling; translations cancel."""
    result, end = IDENTITY, 0
    for match in _TRANSFORM_RE.finditer(text):
        if text[end:match.start()].strip(" \t\r\n,"):
            raise UnknownGeometry("unsupported_transform")
        name, value = match.group(1), _numbers(match.group(2))
        if name == "matrix" and len(value) == 6:
            local = tuple(value[:4])
        elif name == "translate" and len(value) in (1, 2):
            local = IDENTITY
        elif name == "scale" and len(value) in (1, 2):
            local = (value[0], 0.0, 0.0, value[-1])
        elif name == "rotate" and len(value) in (1, 3):
            angle = math.radians(value[0])
            cosine, sine = math.cos(angle), math.sin(angle)
            local = (cosine, sine, -sine, cosine)
        elif name in ("skewX", "skewY") and len(value) == 1:
            tangent = math.tan(math.radians(value[0]))
            local = (1.0, 0.0, tangent, 1.0) if name == "skewX" else (1.0, tangent, 0.0, 1.0)
        else:
            raise UnknownGeometry("unsupported_transform")
        result = _multiply(result, local)
        end = match.end()
    if text[end:].strip(" \t\r\n,"):
        raise UnknownGeometry("unsupported_transform")
    return result


def _length(value):
    # Illustrator emits user-unit numbers; px has the same SVG meaning.
    if not re.fullmatch(_NUMBER + r"(?:px)?", value.strip()):
        raise UnknownGeometry("unsupported_image_length")
    number = float(value.strip().removesuffix("px"))
    if not math.isfinite(number) or number <= 0:
        raise UnknownGeometry("invalid_image_size")
    return number


def _fit(width, height, natural_width, natural_height, preserve):
    sx, sy = width/natural_width, height/natural_height
    parts = preserve.strip().split() or ["xMidYMid", "meet"]
    if parts[0] == "none" and len(parts) == 1:
        return sx, 0.0, 0.0, sy
    if len(parts) == 1:
        parts.append("meet")
    if (len(parts) != 2 or not re.fullmatch(r"x(?:Min|Mid|Max)Y(?:Min|Mid|Max)", parts[0])
            or parts[1] not in ("meet", "slice")):
        raise UnknownGeometry("unsupported_preserve_aspect_ratio")
    scale = min(sx, sy) if parts[1] == "meet" else max(sx, sy)
    return scale, 0.0, 0.0, scale


def _href(element):
    plain, xlink = element.get("href"), element.get("{http://www.w3.org/1999/xlink}href")
    if plain and xlink and plain != xlink:
        raise UnknownGeometry("ambiguous_image_reference")
    return plain or xlink or ""


def _bitmap_size(href):
    header, separator, payload = href.partition(",")
    if not separator or header.lower() not in ("data:image/png;base64", "data:image/jpeg;base64"):
        raise UnknownGeometry("unsupported_or_external_image")
    if len(payload) > 180_000_000:
        raise UnknownGeometry("image_payload_too_large")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (ValueError, binascii.Error):
        raise UnknownGeometry("invalid_base64_image")
    if header.lower() == "data:image/png;base64":
        if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[8:16] != b"\0\0\0\rIHDR":
            raise UnknownGeometry("invalid_png_header")
        width, height = struct.unpack_from(">II", raw, 16)
    else:
        if raw[:2] != b"\xff\xd8":
            raise UnknownGeometry("invalid_jpeg_header")
        offset, dimensions = 2, None
        while offset < len(raw):
            if raw[offset] != 255:
                raise UnknownGeometry("invalid_jpeg_marker")
            while offset < len(raw) and raw[offset] == 255:
                offset += 1
            if offset >= len(raw):
                raise UnknownGeometry("truncated_jpeg")
            marker = raw[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            if offset + 2 > len(raw):
                raise UnknownGeometry("truncated_jpeg_segment")
            length = struct.unpack_from(">H", raw, offset)[0]
            if length < 2 or offset + length > len(raw):
                raise UnknownGeometry("truncated_jpeg_segment")
            if marker == 0xE1 and raw[offset+2:offset+8] == b"Exif\0\0":
                # Automatic EXIF rotation can swap pixel axes. Generated
                # Illustrator JPEGs normally have no EXIF; stay conservative.
                raise UnknownGeometry("jpeg_exif_orientation_unresolved")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if length < 8:
                    raise UnknownGeometry("invalid_jpeg_frame")
                height, width = struct.unpack_from(">HH", raw, offset+3)
                dimensions = width, height
            offset += length
        if dimensions is None:
            raise UnknownGeometry("missing_jpeg_dimensions")
        width, height = dimensions
    if width <= 0 or height <= 0:
        raise UnknownGeometry("empty_bitmap")
    return width, height


def _largest_scale(matrix):
    a, b, c, d = matrix
    # Largest singular value: catches shear/anisotropic scale, while a pure
    # rotation does not falsely enlarge the image's sampling requirement.
    return (math.hypot(a+d, b-c) + math.hypot(a-d, b+c)) / 2


def analyze_embedded_images(root, width_pt, height_pt):
    """Return a conservative factor 1/4 and per-instance sampling diagnostics.

    ``width_pt`` and ``height_pt`` are the desired SVG print dimensions BEFORE
    native EMF supersampling. At factor 4 the reference density is 384 pixels
    per inch. All traversed bitmap instances must retain at least one source
    pixel per output sample in every direction. Clipping is deliberately
    ignored: it reduces visible area, but does not improve pixel density.

    Supports Illustrator's direct images and local <use> of images/groups,
    ancestor affine transforms, root viewBox and preserveAspectRatio. CSS
    geometry, nested SVG/symbol/pattern/mask image contexts, remote images and
    reference cycles choose factor 1 without trying to approximate them.
    """
    images = []
    result = {"recommended_factor": 1, "reason": None, "images": images,
              "max_pixel_scale_at_4x": None}
    if any(_tag(element) in ("feImage", "foreignObject") for element in root.iter()):
        result["reason"] = "unsupported_raster_context"
        return result
    for element in root.iter():
        if _tag(element) == "use":
            try:
                reference = _href(element)
            except UnknownGeometry as error:
                result["reason"] = str(error)
                return result
            if not reference.startswith("#"):
                result["reason"] = "external_use_reference"
                return result
    if not any(_tag(element) == "image" for element in root.iter()):
        result.update(recommended_factor=4, reason="no_embedded_images", max_pixel_scale_at_4x=0.0)
        return result
    try:
        if not all(math.isfinite(n) and n > 0 for n in (width_pt, height_pt)):
            raise UnknownGeometry("invalid_print_size")
        viewbox = _numbers(root.get("viewBox", ""))
        if len(viewbox) != 4 or min(viewbox[2:]) <= 0:
            raise UnknownGeometry("invalid_root_viewbox")
        if root.get("transform", "").strip():
            raise UnknownGeometry("root_transform")
        root_matrix = _fit(width_pt, height_pt, viewbox[2], viewbox[3], root.get("preserveAspectRatio", ""))
        identifiers, parents = {}, {child: parent for parent in root.iter() for child in parent}
        for element in root.iter():
            identifier = element.get("id")
            if identifier:
                if identifier in identifiers:
                    raise UnknownGeometry("duplicate_svg_ids")
                identifiers[identifier] = element
            style = element.get("style", "")
            if _tag(element) == "style":
                style += "".join(element.itertext())
            if _GEOMETRY_CSS.search(style):
                raise UnknownGeometry("css_geometry")
            if _tag(element) in ("animate", "animateTransform", "set"):
                raise UnknownGeometry("animated_geometry")
            if _tag(element) == "image":
                ancestor = parents.get(element)
                while ancestor is not None and ancestor is not root:
                    if _tag(ancestor) not in ("g", "a", "defs"):
                        raise UnknownGeometry("unsupported_image_context")
                    ancestor = parents.get(ancestor)
        pixel_sizes = {}

        def visit(element, matrix, reference_stack=()):
            tag = _tag(element)
            if tag in ("defs", "clipPath", "mask", "pattern", "symbol", "metadata", "title", "desc", "style"):
                return
            if tag == "svg" and element is not root:
                raise UnknownGeometry("nested_svg_viewport")
            matrix = _multiply(matrix, _transform(element.get("transform", "")))
            if tag == "use":
                reference = _href(element)
                if not reference.startswith("#") or reference[1:] not in identifiers:
                    raise UnknownGeometry("unknown_use_reference")
                reference = reference[1:]
                if reference in reference_stack or len(reference_stack) >= 32:
                    raise UnknownGeometry("recursive_use_reference")
                target = identifiers[reference]
                if _tag(target) not in ("image", "g", "a", "use"):
                    raise UnknownGeometry("unsupported_use_target")
                visit(target, matrix, reference_stack + (reference,))
            elif tag == "image":
                href = _href(element)
                if href not in pixel_sizes:
                    pixel_sizes[href] = _bitmap_size(href)
                pixel_width, pixel_height = pixel_sizes[href]
                viewport_width, viewport_height = _length(element.get("width", "")), _length(element.get("height", ""))
                fit = _fit(viewport_width, viewport_height, pixel_width, pixel_height,
                           element.get("preserveAspectRatio", ""))
                physical_matrix = _multiply(matrix, fit)
                scale = _largest_scale(physical_matrix) * 4 * 96 / 72
                if not math.isfinite(scale):
                    raise UnknownGeometry("sampling_scale_overflow")
                images.append({"id": element.get("id"), "pixel_width": pixel_width,
                               "pixel_height": pixel_height, "pixel_scale_at_4x": scale,
                               "display_axis_width_pt": math.hypot(physical_matrix[0], physical_matrix[1])*pixel_width,
                               "display_axis_height_pt": math.hypot(physical_matrix[2], physical_matrix[3])*pixel_height})
            else:
                for child in element:
                    visit(child, matrix, reference_stack)

        visit(root, root_matrix)
        maximum = max((item["pixel_scale_at_4x"] for item in images), default=0.0)
        safe = maximum <= 1.0 + 1e-9
        result.update(recommended_factor=4 if safe else 1,
                      reason="source_pixels_sufficient" if safe else "source_pixels_would_be_upsampled",
                      max_pixel_scale_at_4x=maximum)
    except RecursionError:
        result["reason"] = "deeply_nested_geometry"
    except (UnknownGeometry, ValueError, OverflowError, struct.error) as error:
        result["reason"] = str(error)
    return result


def recommended_supersample_factor(root, width_pt, height_pt):
    return analyze_embedded_images(root, width_pt, height_pt)["recommended_factor"]
