"""Alternate Draw export with full-artboard sizing; appearance needs review."""

from pathlib import Path
import shutil
import struct
import subprocess
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr


def _draw_document(source, width_mm, height_mm):
    # Importing SVG directly into Draw crops to painted content. An explicit
    # graphic frame on an equally sized page preserves the requested viewport,
    # including transparent margins, without adding a white rectangle.
    width, height = "%.9fmm" % width_mm, "%.9fmm" % height_mm
    return '''<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink"
 office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.graphics">
 <office:styles/>
 <office:automatic-styles><style:page-layout style:name="Page">
  <style:page-layout-properties fo:page-width="%s" fo:page-height="%s" fo:margin="0mm"/>
 </style:page-layout></office:automatic-styles>
 <office:master-styles><style:master-page style:name="Default" style:page-layout-name="Page"/></office:master-styles>
 <office:body><office:drawing><draw:page draw:name="Figure" draw:master-page-name="Default">
  <draw:frame svg:x="0mm" svg:y="0mm" svg:width="%s" svg:height="%s">
   <draw:image xlink:href=%s xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>
  </draw:frame>
 </draw:page></office:drawing></office:body>
</office:document>''' % (width, height, width, height, quoteattr(source.resolve().as_uri()))


def export_svg_emf(svg, target, executable, scratch):
    """Export one isolated SVG through Draw with explicit page dimensions.

    Native Illustrator EMF quantizes small outlined text at standard size;
    enlarging a document with transparent PNGs can stall its native exporter.
    Draw retains fine path coordinates, but some image/fill combinations can
    render differently. This optional engine must not replace native export
    automatically. A private profile, SVG copy, and full-page graphic frame
    keep the conversion self-contained.
    """
    try:
        from .converter import inspect_emf
        from .native_emf import _svg_geometry
    except ImportError:
        from converter import inspect_emf
        from native_emf import _svg_geometry
    folder = Path(scratch) / ("compat-emf-" + uuid.uuid4().hex[:12])
    folder.mkdir()
    source = folder / "figure.svg"
    tree = ET.parse(svg)
    root = tree.getroot()
    viewbox, width_pt, height_pt, _ = _svg_geometry(root)
    width_mm, height_mm = width_pt / 72 * 25.4, height_pt / 72 * 25.4
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
    document = folder / "figure.fodg"
    document.write_text(_draw_document(source, width_mm, height_mm), encoding="utf-8")
    output_folder = folder / "output"
    output_folder.mkdir()
    command = [str(executable), "-env:UserInstallation=" + (folder / "profile").resolve().as_uri(),
               "--headless", "--nologo", "--nodefault", "--norestore", "--draw",
               "--convert-to", "emf:draw_emf_Export", "--outdir", str(output_folder), str(document)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("含图片的 EMF 导出超时；已完成的 SVG 和 PNG 会保留。")
    output = output_folder / "figure.emf"
    if process.returncode or not output.is_file():
        detail = (process.stderr or process.stdout).strip()[-1200:]
        raise RuntimeError("含图片的 EMF 未能完成导出：" + detail)
    metadata = inspect_emf(output)
    frame = struct.unpack_from("<4i", output.read_bytes(), 24)
    actual_width = (frame[2] - frame[0]) / 100
    actual_height = (frame[3] - frame[1]) / 100
    # EMF frames have 0.01 mm precision; Draw rounds the last coordinate down.
    # A larger discrepancy indicates cropping or a wrong page, not rounding.
    if abs(actual_width - width_mm) > .025 or abs(actual_height - height_mm) > .025:
        raise RuntimeError("EMF 画板尺寸校验未通过，请保留 SVG 或 PNG 并重试。")
    metadata.update({
        "engine": "LibreOffice vector EMF",
        "artboard_clipped": True,
        "embedded_image_compatibility": True,
        "appearance_requires_review": True,
        "emf_frame_width_mm": actual_width,
        "emf_frame_height_mm": actual_height,
        "source_artboard_width_mm": width_mm,
        "source_artboard_height_mm": height_mm,
        "native_physical_size_differs": False,
    })
    shutil.copy2(output, target)
    return metadata
