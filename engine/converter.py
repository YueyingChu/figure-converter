#!/usr/bin/env python3
"""Local Illustrator-backed figure conversion; no uploads and no Python packages."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parent
SUPPORTED = {".ai", ".eps", ".svg", ".pdf"}


def probe():
    candidates = sorted(Path("/Applications").glob("Adobe Illustrator */Adobe Illustrator.app"), reverse=True)
    illustrator = next((p for p in candidates if p.is_dir()), None)
    lo = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if not lo.is_file():
        found = shutil.which("soffice")
        lo = Path(found) if found else None
    return {"illustrator": {"available": illustrator is not None, "path": str(illustrator) if illustrator else None},
            "libreoffice": {"available": lo is not None, "path": str(lo) if lo else None}}


def applescript_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_js_script(path, source):
    """ExtendScript may read unmarked UTF-8 as a legacy encoding. Use ASCII JS."""
    def escape(character):
        code = ord(character)
        if code < 128:
            return character
        if code <= 0xFFFF:
            return "\\u%04x" % code
        code -= 0x10000
        return "\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
    Path(path).write_text("".join(escape(ch) for ch in source), encoding="ascii")


def run_illustrator(script, app_path, timeout=930):
    code = ('on run argv\nset jsFile to POSIX file (item 1 of argv)\n'
            'with timeout of 900 seconds\n'
            'tell application ' + applescript_string(app_path) + ' to do javascript jsFile\n'
            'end timeout\nend run\n')
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", code, str(script)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Illustrator 转换超时。请检查 Illustrator 是否有等待处理的对话框。")
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        if "1743" in detail or "not authorized" in detail.lower():
            raise RuntimeError("macOS 尚未允许控制 Illustrator。请在系统设置 → 隐私与安全性 → 自动化中允许 Figure Converter 控制 Illustrator，然后重试。")
        raise RuntimeError("Illustrator 未能完成转换：" + detail[-1800:])
    return proc.stdout.strip()


@contextlib.contextmanager
def illustrator_lock():
    # All local instances share this lock; Illustrator's document API is global.
    lock_path = Path(tempfile.gettempdir()) / ("figure-converter-" + str(os.getuid()) + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个 Figure Converter 任务正在使用 Illustrator，请稍后重试。")
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def inspect_svg(path):
    tree = ET.parse(path)
    counts = {k: 0 for k in ("path", "text", "image", "linearGradient", "radialGradient", "mask", "clipPath", "filter")}
    dashed = 0
    for element in tree.iter():
        name = element.tag.split("}")[-1]
        if name in counts:
            counts[name] += 1
        if "stroke-dasharray" in element.attrib or "stroke-dasharray" in element.attrib.get("style", ""):
            dashed += 1
    counts["dashed_elements"] = dashed
    counts["viewBox"] = tree.getroot().get("viewBox")
    return counts


def set_svg_physical_size(path, width_pt, height_pt):
    """Make Illustrator's responsive SVG keep the artboard's print dimensions."""
    data = path.read_bytes()
    match = re.search(rb"<svg\b[^>]*>", data)
    if not match or width_pt <= 0 or height_pt <= 0:
        raise ValueError("SVG 画板尺寸无效")
    tag = re.sub(rb'\s(?:width|height)\s*=\s*(?:"[^"]*"|\x27[^\x27]*\x27)', b"", match.group())
    dimensions = (' width="%.8gpt" height="%.8gpt"' % (width_pt, height_pt)).encode("ascii")
    tag = tag[:-1] + dimensions + b">"
    path.write_bytes(data[:match.start()] + tag + data[match.end():])


def png_metadata(path, dpi):
    """Set physical DPI without decoding/resampling any pixels; validate chunk CRCs."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 输出无效")
    result, offset, dimensions, ended, has_data = [raw[:8]], 8, None, False, False
    ppm = round(dpi / 0.0254)
    data = b"pHYs" + struct.pack(">IIB", ppm, ppm, 1)
    physical = struct.pack(">I", 9) + data + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError("PNG 文件不完整")
        kind = raw[offset + 4:offset + 8]
        if zlib.crc32(raw[offset + 4:end - 4]) & 0xFFFFFFFF != struct.unpack_from(">I", raw, end - 4)[0]:
            raise ValueError("PNG 校验失败")
        if kind != b"pHYs":
            result.append(raw[offset:end])
        if kind == b"IHDR":
            if offset != 8 or length != 13:
                raise ValueError("PNG 文件头无效")
            dimensions = struct.unpack_from(">II", raw, offset + 8)
            if not all(dimensions):
                raise ValueError("PNG 尺寸无效")
            result.append(physical)
        if kind == b"IDAT" and length:
            has_data = True
        if kind == b"IEND":
            ended = True
            break
        offset = end
    if not dimensions or not ended or not has_data:
        raise ValueError("PNG 缺少图像数据")
    path.write_bytes(b"".join(result))
    return {"width_px": dimensions[0], "height_px": dimensions[1], "dpi": dpi}


def inspect_emf(path):
    raw = path.read_bytes()
    if len(raw) < 88 or struct.unpack_from("<I", raw)[0] != 1 or raw[40:44] != b" EMF":
        raise ValueError("EMF 输出无效")
    if struct.unpack_from("<I", raw, 48)[0] != len(raw):
        raise ValueError("EMF 文件长度校验失败")
    offset, records, bitmap, gradient, eof = 0, 0, 0, 0, False
    while offset + 8 <= len(raw):
        kind, size = struct.unpack_from("<II", raw, offset)
        if size < 8 or size % 4 or offset + size > len(raw):
            raise ValueError("EMF 记录不完整")
        records += 1
        bitmap += kind in {76, 77, 78, 79, 80, 81, 114, 116}
        gradient += kind == 118
        offset += size
        if kind == 14:
            eof = True
            break
    if not eof or offset != len(raw):
        raise ValueError("EMF 缺少结束标记")
    return {"records": records, "bitmap_records": bitmap, "gradient_records": gradient}


def export_emf_libreoffice(svg, target, executable, scratch):
    try:
        from .compat_emf import export_svg_emf
    except ImportError:
        from compat_emf import export_svg_emf
    return export_svg_emf(svg, target, executable, scratch)


def export_emf(svg, target, app_path, scratch):
    try:
        from .native_emf import export_svg_emf
    except ImportError:
        from native_emf import export_svg_emf
    diagnostics = export_svg_emf(svg, target, app_path, scratch)
    metadata = inspect_emf(target)
    metadata.update(diagnostics)
    metadata["engine"] = "Adobe Illustrator"
    return metadata


def inspect_document(path, output_format):
    """Reject missing/truncated native documents without a runtime PDF dependency."""
    raw = Path(path).read_bytes()
    if output_format == "pdf":
        if not re.match(rb"%PDF-1\.[0-9]\b", raw) or b"%%EOF" not in raw[-2048:]:
            raise ValueError("PDF 输出不完整或文件头无效")
        return {"header_valid": True}
    if output_format != "eps":
        raise ValueError("不支持的文档格式：" + str(output_format))
    # Some EPS writers prepend the DOS binary preview header.
    if raw[:4] == b"\xc5\xd0\xd3\xc6":
        if len(raw) < 30:
            raise ValueError("EPS 预览文件头不完整")
        offset, length = struct.unpack_from("<II", raw, 4)
        if offset < 30 or not length or offset + length > len(raw):
            raise ValueError("EPS 图形数据不完整")
        raw = raw[offset:offset + length]
    if not re.match(rb"%!PS-Adobe-[^\r\n]*\bEPSF-", raw):
        raise ValueError("EPS 输出文件头无效")
    bounds = None
    for label in (b"HiResBoundingBox", b"BoundingBox"):
        for match in re.finditer(rb"(?m)^%%" + label + rb":[ \t]*([^\r\n]+)", raw):
            fields = match.group(1).split()
            if fields == [b"(atend)"]:
                continue
            try:
                values = [float(value) for value in fields]
            except ValueError:
                continue
            if (len(values) == 4 and all(math.isfinite(value) for value in values)
                    and values[2] > values[0] and values[3] > values[1]):
                bounds = values
                break
        if bounds:
            break
    if bounds is None:
        raise ValueError("EPS 缺少有效的画板边界")
    return {"header_valid": True, "eps_bounds_pt": bounds,
            "width_pt": bounds[2] - bounds[0], "height_pt": bounds[3] - bounds[1]}


def export_native_documents(checkpoint, artboards, formats, app_path, scratch):
    """Save directly from prepared AI, retaining native layers and transparency."""
    if not Path(checkpoint).is_file():
        raise RuntimeError("未生成 PDF / EPS 所需的临时文档，请重试。")
    requested = [kind for kind in ("pdf", "eps") if kind in formats]
    folder = scratch / "documents"
    folder.mkdir()
    result_path = folder / "result.json"
    entries, expected = [], {}
    for item in artboards:
        index = item["index"]
        entry = {"index": index, "bounds": item["crop_bounds"],
                 "width_pt": item["width_pt"], "height_pt": item["height_pt"]}
        for kind in requested:
            path = folder / ("artboard-%s.%s" % (index, kind))
            entry[kind] = str(path)
            expected[(index, kind)] = path
        entries.append(entry)
    config = {"source": str(checkpoint), "result": str(result_path), "entries": entries}
    script = folder / "save-documents.jsx"
    write_js_script(script, "var documentConfig = " + json.dumps(config, ensure_ascii=True) + ";\n" +
                    (ROOT / "native_documents.jsx").read_text(encoding="utf-8"))
    run_illustrator(script, app_path)
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    seen = set()
    for item in result.get("outputs", []):
        key = (item["index"], item["format"])
        if key not in expected or key in seen or Path(item["path"]) != expected[key]:
            raise RuntimeError("PDF / EPS 输出与请求的画板不匹配。")
        seen.add(key)
    result.setdefault("errors", [])
    for index, kind in expected.keys() - seen:
        result["errors"].append("画板 %s 的 %s 未能导出。" % (index, kind.upper()))
    return result


def convert(args):
    source = Path(args.input).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in SUPPORTED:
        raise ValueError("请选择 AI、EPS、SVG 或 PDF 文件。")
    if not 72 <= args.dpi <= 1200:
        raise ValueError("PNG 分辨率应在 72–1200 DPI 之间。")
    width_mm = getattr(args, "width_mm", None)
    if width_mm is not None and (not math.isfinite(width_mm) or not 0 < width_mm <= 1000):
        raise ValueError("自定义宽度必须大于 0 且不超过 1000 mm。")
    trim_artboard = bool(getattr(args, "trim_artboard", False))
    formats = list(dict.fromkeys(args.formats or ["svg", "png"]))
    capabilities = probe()
    if not capabilities["illustrator"]["available"]:
        raise RuntimeError("未找到 Adobe Illustrator。当前版本需要已安装并激活的 Illustrator。")
    emf_engine = getattr(args, "emf_engine", "illustrator")
    if "emf" in formats and emf_engine == "libreoffice" and not capabilities["libreoffice"]["available"]:
        raise RuntimeError("导出 EMF 需要 LibreOffice；请安装后重试，或先只选择 SVG / PNG。")
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else source.parent / "Figure Exports"
    output_root.mkdir(parents=True, exist_ok=True)
    result = {"success": False, "outputs": [], "warnings": [], "error": None, "output_dir": None}
    with illustrator_lock(), tempfile.TemporaryDirectory(prefix="figure-converter-") as temporary:
        scratch = Path(temporary)
        # Illustrator only opens this copy; existing user documents are never saved/closed.
        working = scratch / ("source" + source.suffix.lower())
        shutil.copy2(source, working)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_dir = output_root / (source.stem + "-" + stamp + "-" + uuid.uuid4().hex[:6])
        job_dir.mkdir()
        result["output_dir"] = str(job_dir)
        config = {"input": str(working), "original": str(source), "output": str(scratch / "native"),
                  "result": str(scratch / "native-result.json"), "dpi": args.dpi,
                  "svg": "svg" in formats or "emf" in formats, "png": "png" in formats,
                  "outlineText": not args.keep_text, "outlineStrokes": not args.keep_strokes,
                  "transparent": not args.white_background, "firstArtboard": args.first_artboard,
                  "trimArtboard": trim_artboard, "widthMm": width_mm,
                  "documentSource": str(scratch / "prepared.ai") if {"pdf", "eps"}.intersection(formats) else None}
        script = scratch / "convert.jsx"
        write_js_script(script, "var FC_CONFIG = " + json.dumps(config, ensure_ascii=True) + ";\n" +
                        (ROOT / "illustrator.jsx").read_text(encoding="utf-8"))
        report = {"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "settings": {"formats": formats, "dpi": args.dpi, "emf_engine": emf_engine, "outline_text": not args.keep_text,
                               "outline_strokes": not args.keep_strokes, "transparent": not args.white_background,
                               "first_artboard": args.first_artboard, "trim_artboard": trim_artboard,
                               "width_mm": width_mm},
                  "engines": capabilities}
        try:
            run_illustrator(script, capabilities["illustrator"]["path"])
            native = json.loads(Path(config["result"]).read_text(encoding="utf-8-sig"))
            result["warnings"].extend(native.get("warnings", []))
            report["preprocessing"] = native.get("preprocessing", {})
            report["artboards"] = native.get("artboards", [])
            errors = [] if native.get("success") else [native.get("error", "Illustrator 转换失败")]
            for item in native.get("artboards", []):
                base = source.stem + ("-画板" + str(item["index"]).zfill(2) if native["totalArtboards"] > 1 else "")
                dimensions = {}
                if item.get("width_pt") and item.get("height_pt"):
                    dimensions = {"width_mm": item["width_pt"] / 72 * 25.4,
                                  "height_mm": item["height_pt"] / 72 * 25.4}
                if item.get("png"):
                    target = job_dir / (base + ".png")
                    shutil.copy2(item["png"], target)
                    info = png_metadata(target, args.dpi)
                    result["outputs"].append({"path": str(target), "format": "png", "artboard": item["index"], **info, **dimensions})
                if item.get("svg"):
                    svg = Path(item["svg"])
                    if item.get("width_pt") and item.get("height_pt"):
                        set_svg_physical_size(svg, item["width_pt"], item["height_pt"])
                    info = inspect_svg(svg)
                    if info["image"]:
                        result["warnings"].append("原图中的 PNG 或照片已保留。这不影响导出；图片放大后的清晰度取决于原始分辨率。")
                    if "svg" in formats:
                        target = job_dir / (base + ".svg")
                        shutil.copy2(svg, target)
                        result["outputs"].append({"path": str(target), "format": "svg", "artboard": item["index"], "inspection": info, **dimensions})
                    if "emf" in formats:
                        target = job_dir / (base + ".emf")
                        try:
                            if emf_engine == "libreoffice":
                                emf_info = export_emf_libreoffice(svg, target, capabilities["libreoffice"]["path"], scratch)
                                result["warnings"].append("当前使用实验性的 LibreOffice EMF 导出。它可能遗漏透明图片或改变细线，请与 SVG 核对；正式交付建议使用默认导出方式。")
                            else:
                                emf_info = export_emf(svg, target, capabilities["illustrator"]["path"], scratch)
                            actual_dimensions = dict(dimensions)
                            if emf_info.get("emf_frame_width_mm") and emf_info.get("emf_frame_height_mm"):
                                actual_dimensions = {"width_mm": emf_info["emf_frame_width_mm"],
                                                     "height_mm": emf_info["emf_frame_height_mm"]}
                            result["outputs"].append({"path": str(target), "format": "emf", "artboard": item["index"], "inspection": emf_info, **actual_dimensions})
                            if emf_info["bitmap_records"] and not info["image"]:
                                result["warnings"].append("为保留渐变或透明效果，EMF 中的部分区域以图片保存；文字和线条仍可保留为矢量。")
                            if emf_info.get("native_physical_size_differs"):
                                result["warnings"].append("EMF 插入 Word 或 PowerPoint 后可能显示为不同大小，请在其中设置所需宽度并锁定长宽比。")
                            if emf_info.get("raster_quality_limited_by_size"):
                                result["warnings"].append("画板 %s 较大，EMF 使用标准精度导出，以避免内存不足。" % item["index"])
                            if emf_info.get("raster_quality_limited_by_embedded_images"):
                                result["warnings"].append("为兼容内嵌图片，EMF 使用标准精度导出；若小字边缘不够清晰，请优先使用 SVG 或高分辨率 PNG。")
                        except Exception as exc:
                            errors.append(str(exc))
            if config["documentSource"] and native.get("artboards"):
                try:
                    if native.get("documentError"):
                        raise RuntimeError(native["documentError"])
                    documents = export_native_documents(Path(config["documentSource"]), native["artboards"],
                                                        formats, capabilities["illustrator"]["path"], scratch)
                    result["warnings"].extend(documents.get("warnings", []))
                    errors.extend(documents.get("errors", []))
                    for item in documents.get("outputs", []):
                        try:
                            dimensions = [float(item[key]) for key in ("width_pt", "height_pt")]
                            if not all(math.isfinite(value) and value > 0 for value in dimensions):
                                raise ValueError("PDF / EPS 的输出尺寸无效")
                            info = inspect_document(Path(item["path"]), item["format"])
                            if item["format"] == "eps" and any(abs(info[key] - value) > 1.1 for key, value in zip(("width_pt", "height_pt"), dimensions)):
                                raise ValueError("EPS 实际边界与目标尺寸不一致，已停止输出该文件")
                            base = source.stem + ("-画板" + str(item["index"]).zfill(2) if native["totalArtboards"] > 1 else "")
                            target = job_dir / (base + "." + item["format"])
                            shutil.copy2(item["path"], target)
                            result["outputs"].append({"path": str(target), "format": item["format"],
                                                      "artboard": item["index"], "inspection": info,
                                                      "width_mm": dimensions[0] / 72 * 25.4,
                                                      "height_mm": dimensions[1] / 72 * 25.4})
                        except Exception as exc:
                            errors.append("画板 %s 的 %s：%s" % (item.get("index", "?"), str(item.get("format", "PDF / EPS")).upper(), exc))
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError("\n".join(errors))
            result["success"] = True
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            result["warnings"] = list(dict.fromkeys(result["warnings"]))
            report.update(result)
            (job_dir / "conversion-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="科研图本地转换：Illustrator → SVG / EMF / PNG / PDF / EPS")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe")
    convert_parser = sub.add_parser("convert")
    convert_parser.add_argument("input")
    convert_parser.add_argument("--format", dest="formats", action="append", choices=["svg", "emf", "png", "pdf", "eps"])
    convert_parser.add_argument("--dpi", type=int, default=600)
    convert_parser.add_argument("--output-dir")
    convert_parser.add_argument("--keep-text", action="store_true")
    convert_parser.add_argument("--keep-strokes", action="store_true")
    convert_parser.add_argument("--white-background", action="store_true")
    convert_parser.add_argument("--first-artboard", action="store_true")
    convert_parser.add_argument("--emf-engine", choices=["illustrator", "libreoffice"], default="illustrator")
    convert_parser.add_argument("--trim-artboard", action="store_true")
    convert_parser.add_argument("--width-mm", type=float)
    args = parser.parse_args()
    try:
        result = probe() if args.command == "probe" else convert(args)
    except Exception as exc:
        result = {"success": False, "outputs": [], "warnings": [], "error": str(exc), "output_dir": None}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success", True) else 1


if __name__ == "__main__":
    sys.exit(main())
