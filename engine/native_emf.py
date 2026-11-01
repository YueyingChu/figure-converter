"""Build Illustrator EMF export Actions without invoking the application."""

import json
import math
from pathlib import Path
import shutil
import struct
import uuid
import xml.etree.ElementTree as ET


class UnsupportedEMFMapping(ValueError):
    """A valid native EMF whose coordinates cannot safely be rescaled."""


def _has_embedded_images(root):
    return any(element.tag.rsplit("}", 1)[-1] == "image" for element in root.iter())


def _svg_geometry(root):
    parts = root.get("viewBox", "").replace(",", " ").split()
    try:
        viewbox = [float(value) for value in parts]
        dimensions = []
        for key in ("width", "height"):
            value = root.get(key, "")
            if not value.endswith("pt"):
                raise ValueError()
            dimensions.append(float(value[:-2]))
    except ValueError:
        raise ValueError("EMF 需要有效的 SVG viewBox 和以 pt 表示的画板尺寸。")
    if (len(viewbox) != 4 or not all(math.isfinite(n) for n in viewbox + dimensions)
            or min(viewbox[2:] + dimensions) <= 0):
        raise ValueError("EMF 需要有效、有限且大于零的 SVG 画板尺寸。")
    width, height = dimensions
    def fits(factor):
        return max(width, height) * factor <= 16300 and width * height * (96 / 72 * factor) ** 2 <= 100000000
    if not fits(1):
        raise ValueError("EMF 画板过大，请缩小画板后重试。")
    try:
        from .svg_geometry import recommended_supersample_factor
    except ImportError:
        from svg_geometry import recommended_supersample_factor
    # Enlarging low-resolution alpha pixels can stall the native exporter.
    # High-resolution embedded pictures can still use the denser path grid.
    factor = recommended_supersample_factor(root, width, height)
    return parts, width, height, 4 if fits(4) and factor == 4 else 1


def restore_emf_mapping(data, factor, width_pt, height_pt, exact_physical_size=False):
    """Restore an Illustrator export's display size, retaining dense rasters.

    The tested exporter uses one identity anisotropic window/viewport pair.
    Reject different mappings rather than silently distorting an unknown EMF.
    Drawing coordinates, bitmap samples and the logical window remain intact;
    viewport, device bounds and native physical frame shrink together. Keep
    Illustrator's reference-device metrics by default. For exact publication
    dimensions, update the frame AND reference-device density together;
    changing the frame alone clips the drawing in LibreOffice.
    """
    if factor not in (1, 4) or not all(math.isfinite(n) and n > 0 for n in (width_pt, height_pt)):
        raise ValueError("EMF 缩放参数无效。")
    raw = bytearray(data)
    if (len(raw) < 88 or struct.unpack_from("<I", raw)[0] != 1 or raw[40:44] != b" EMF"
            or struct.unpack_from("<I", raw, 48)[0] != len(raw)):
        raise ValueError("EMF 文件头无效。")
    offset, records, windows, viewports, origins, modes, eof = 0, 0, [], [], [], [], False
    transformed = False
    while offset + 8 <= len(raw):
        kind, size = struct.unpack_from("<II", raw, offset)
        if size < 8 or size % 4 or offset + size > len(raw):
            raise ValueError("EMF 记录不完整。")
        records += 1
        if kind in (9, 10, 11, 12):
            if size != 16:
                raise ValueError("EMF 映射记录无效。")
            pair = struct.unpack_from("<2i", raw, offset + 8)
            if kind == 9: windows.append(pair)
            elif kind == 11: viewports.append((offset, pair))
            else: origins.append(pair)
        elif kind == 17:
            if size != 12: raise ValueError("EMF 映射模式无效。")
            modes.append(struct.unpack_from("<i", raw, offset + 8)[0])
        elif kind in (35, 36):
            transformed = True
        offset += size
        if kind == 14:
            eof = True
            break
    if not eof or offset != len(raw):
        raise ValueError("EMF 结束标记无效。")
    # Illustrator can undercount nRecords when exporting embedded PNGs.
    # Every record length and the final EOF have been checked above, so
    # repair this bookkeeping field without touching drawing/bitmap data.
    struct.pack_into("<I", raw, 52, records)
    if transformed:
        raise UnsupportedEMFMapping("EMF 含特殊坐标变换。")
    if (modes != [8] or len(windows) != 1 or len(viewports) != 1
            or windows[0] != viewports[0][1] or min(windows[0]) <= 0
            or any(pair != (0, 0) for pair in origins)):
        raise UnsupportedEMFMapping("EMF 使用特殊坐标映射。")
    bounds = struct.unpack_from("<4i", raw, 8)
    if bounds != (0, -windows[0][1], windows[0][0], 0):
        raise UnsupportedEMFMapping("EMF 使用特殊画板边界。")
    scaled = tuple(round(value / factor) for value in windows[0])
    if min(scaled) <= 0:
        raise ValueError("EMF 画板尺寸过小。")
    struct.pack_into("<2i", raw, viewports[0][0] + 8, *scaled)
    struct.pack_into("<4i", raw, 8, 0, -scaled[1], scaled[0], 0)
    frame = struct.unpack_from("<4i", raw, 24)
    if frame[0] != 0 or frame[3] != 0 or frame[1] >= 0 or frame[2] <= 0:
        raise ValueError("EMF 原生物理边界无效。")
    struct.pack_into("<4i", raw, 24, *(round(value / factor) for value in frame))
    if exact_physical_size:
        frame_width = max(1, round(width_pt / 72 * 2540))
        frame_height = max(1, round(height_pt / 72 * 2540))
        struct.pack_into("<4i", raw, 24, 0, -frame_height, frame_width, 0)
        # EMF expresses its frame in 0.01 mm but reference-device size in
        # integer mm. A virtual device 100 times the image gives an exact
        # rational density, even when the requested width is not whole pixels.
        struct.pack_into("<4i", raw, 72, scaled[0] * 100, scaled[1] * 100,
                         frame_width, frame_height)
        if struct.unpack_from("<I", raw, 4)[0] >= 108:
            struct.pack_into("<2i", raw, 100, frame_width * 1000, frame_height * 1000)
    return bytes(raw)


def aia_string(value):
    data = str(value).encode("utf-8")
    return "[ %d\n%s\n]" % (len(data), data.hex())


def emf_action(output_path, set_name, action_name="Export EMF", artboard_range=None, raw=None):
    """Return a temporary action set targeting an explicit output file.

    Illustrator's exportFile API lacks EMF, while its native export Action
    exposes the installed Enhanced Metafile exporter. artboard_range is a
    one-based range string, but current EMF export ignores those parameters;
    production callers isolate one artboard before invoking this action.
    """
    parameters = []
    if raw is not None:
        data = bytes(raw)
        parameters.append((1885434477, "raw", "< %d\n%s\n>\n/size %d" %
                           (len(data), data.hex(), len(data))))
    parameters.extend([
        (1851878757, "ustring", aia_string(output_path)),
        (1718775156, "ustring", aia_string("Enhanced Metafile")),
        (1702392942, "ustring", aia_string("emf")),
        (1936548194, "boolean", "1" if artboard_range is not None else "0"),
    ])
    if artboard_range is not None:
        parameters.extend([
            (1935764588, "boolean", "0"),
            (1936875886, "ustring", aia_string(artboard_range)),
        ])
    lines = ["/version 3", "/name " + aia_string(set_name), "/isOpen 1", "/actionCount 1",
             "/action-1 {", "/name " + aia_string(action_name), "/keyIndex 0", "/colorIndex 0",
             "/isOpen 1", "/eventCount 1", "/event-1 {", "/useRulersIn1stQuadrant 0",
             "/internalName (adobe_exportDocument)", "/isOpen 0", "/isOn 1",
             "/hasDialog 1", "/showDialog 0", "/parameterCount %d" % len(parameters)]
    for i, (key, kind, value) in enumerate(parameters, 1):
        lines.extend(["/parameter-%d {" % i, "/key %d" % key, "/showInPalette 4294967295",
                      "/type (%s)" % kind, "/value " + value, "}"])
    return "\n".join(lines + ["}", "}", ""])


def export_svg_emf(svg, target, app_path, scratch):
    """Use Illustrator's native exporter on one already outlined SVG artboard.

    Callers serialize Illustrator access. A private copy is opened and closed
    without saving. Existing documents and the previous interaction mode are
    restored. No user action sets are replaced.
    """
    try:
        from .converter import run_illustrator, inspect_emf
    except ImportError:
        from converter import run_illustrator, inspect_emf
    folder = Path(scratch) / ("native-emf-" + uuid.uuid4().hex[:12])
    folder.mkdir()
    source = folder / "figure.svg"
    shutil.copy2(svg, source)
    # The native EMF exporter ignores artboard-range action parameters and
    # otherwise crops to painted artwork. One isolated SVG plus an explicit
    # viewport clipping group keeps the whole selected artboard, including
    # its whitespace, and prevents off-artboard artwork leaking into EMF.
    tree = ET.parse(source)
    root = tree.getroot()
    viewbox, width_pt, height_pt, factor = _svg_geometry(root)
    root.set("width", "%.12gpt" % (width_pt * factor))
    root.set("height", "%.12gpt" % (height_pt * factor))
    clipped = False
    if len(viewbox) == 4:
        values = [float(value) for value in viewbox]
        if all(math.isfinite(value) for value in values) and values[2] > 0 and values[3] > 0:
            namespace = "http://www.w3.org/2000/svg"
            ET.register_namespace("", namespace)
            ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
            tag = lambda name: "{" + namespace + "}" + name
            clip_id = "fc-artboard-" + uuid.uuid4().hex
            group = ET.Element(tag("g"), {"clip-path": "url(#" + clip_id + ")"})
            for child in list(root):
                root.remove(child)
                group.append(child)
            defs = ET.SubElement(root, tag("defs"))
            clip = ET.SubElement(defs, tag("clipPath"),
                                 {"id": clip_id, "clipPathUnits": "userSpaceOnUse"})
            ET.SubElement(clip, tag("rect"), dict(zip(("x", "y", "width", "height"), viewbox)))
            root.append(group)
            tree.write(source, encoding="utf-8", xml_declaration=True)
            clipped = True
    output = folder / "figure.emf"
    set_name = "FigureConverterEMF_" + uuid.uuid4().hex
    action = folder / "export.aia"
    action.write_text(emf_action(output, set_name), encoding="utf-8")
    config = {"source": str(source), "action": str(action), "setName": set_name,
              "stage": str(folder / "stage.txt")}
    js = "var nativeConfig = " + json.dumps(config) + ";\n" + r'''
(function () {
    var c = nativeConfig;
    var previous = app.documents.length ? app.activeDocument : null;
    var interaction = app.userInteractionLevel;
    var doc = null, loaded = false;
    function stage(value) {
        var f = File(c.stage); f.encoding = "UTF-8";
        if (f.open("w")) { f.write(value); f.close(); }
    }
    try {
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
        stage("Opening temporary SVG");
        doc = app.open(File(c.source));
        stage("Loading native export action");
        app.loadAction(File(c.action));
        loaded = true;
        stage("Exporting native EMF");
        app.doScript("Export EMF", c.setName, false);
        stage("Native export complete");
        return "OK";
    } catch (e) {
        return "ERROR: " + e.toString();
    } finally {
        if (loaded) { try { app.unloadAction(c.setName, ""); } catch (e) {} }
        if (doc) { try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e) {} }
        if (previous) { try { app.activeDocument = previous; } catch (e) {} }
        app.userInteractionLevel = interaction;
    }
})();
'''
    script = folder / "export.jsx"
    script.write_text(js, encoding="utf-8")
    mapping_fallback = False
    for attempt in range(2):
        result = run_illustrator(script, app_path)
        if result != "OK" or not output.is_file():
            raise RuntimeError("Illustrator 原生 EMF 导出失败：" + result)
        metadata = inspect_emf(output)
        try:
            restored = restore_emf_mapping(output.read_bytes(), factor, width_pt, height_pt,
                                           exact_physical_size=True)
            output.write_bytes(restored)
            break
        except UnsupportedEMFMapping:
            mapping_fallback = True
            if factor == 1:
                # Keep a valid native output intact when its coordinate
                # system is unfamiliar. Rescaling that file would risk
                # distorting rotated images, clipping masks, or gradients.
                raw = bytearray(output.read_bytes())
                struct.pack_into("<I", raw, 52, metadata["records"])
                output.write_bytes(raw)
                break
            factor = 1
            root.set("width", "%.12gpt" % width_pt)
            root.set("height", "%.12gpt" % height_pt)
            tree.write(source, encoding="utf-8", xml_declaration=True)
    inspect_emf(output)
    shutil.copy2(output, target)
    metadata["engine"] = "Illustrator native EMF export"
    metadata["artboard_clipped"] = clipped
    metadata["raster_supersample_factor"] = factor
    frame = struct.unpack_from("<4i", output.read_bytes(), 24)
    metadata["emf_frame_width_mm"] = (frame[2] - frame[0]) / 100
    metadata["emf_frame_height_mm"] = (frame[3] - frame[1]) / 100
    metadata["source_artboard_width_mm"] = width_pt / 72 * 25.4
    metadata["source_artboard_height_mm"] = height_pt / 72 * 25.4
    metadata["native_physical_size_differs"] = (
        abs(metadata["emf_frame_width_mm"] - metadata["source_artboard_width_mm"]) > .05
        or abs(metadata["emf_frame_height_mm"] - metadata["source_artboard_height_mm"]) > .05)
    metadata["raster_quality_limited_by_size"] = factor != 4 and not _has_embedded_images(root) and not mapping_fallback
    metadata["raster_quality_limited_by_embedded_images"] = factor != 4 and _has_embedded_images(root)
    metadata["coordinate_mapping_fallback"] = mapping_fallback
    return metadata
