"""Regression checks for conversion integrity; never launch desktop applications."""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib


ENGINE = Path(__file__).resolve().parents[1] / "engine" / "converter.py"
sys.path.insert(0, str(ENGINE.parent))  # Same sibling-module lookup as the CLI.
spec = importlib.util.spec_from_file_location("figure_converter", ENGINE)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)
native_spec = importlib.util.spec_from_file_location("figure_native_emf", ENGINE.parent / "native_emf.py")
native_emf = importlib.util.module_from_spec(native_spec)
native_spec.loader.exec_module(native_emf)


def chunk(kind, data):
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def png_bytes(width=2, height=1, include_pixels=True, include_end=True, with_dpi=False):
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    result = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    if with_dpi:
        result += chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))
    if include_pixels:
        result += chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff\x00\x00\xff" * width))
    if include_end:
        result += chunk(b"IEND", b"")
    return result


def png_chunks(data):
    result = []
    offset = 8
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        result.append((data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]))
        offset += size + 12
    return result


def emf_bytes(extra_records=()):
    eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
    body = b"".join(extra_records) + eof
    header = bytearray(88)
    struct.pack_into("<II", header, 0, 1, 88)
    header[40:44] = b" EMF"
    struct.pack_into("<III", header, 44, 0x10000, 88 + len(body), 2 + len(extra_records))
    struct.pack_into("<H", header, 56, 1)
    return bytes(header) + body


def native_emf_bytes():
    """Mock a 4x Illustrator export of a 144 x 72 pt artboard at 96 dpi."""
    pixels = b"dense-raster-samples".ljust(88, b"\x00")
    records = (
        struct.pack("<IIi", 17, 12, 8),
        struct.pack("<IIii", 10, 16, 0, 0),
        struct.pack("<IIii", 12, 16, 0, 0),
        struct.pack("<IIii", 9, 16, 768, 384),
        struct.pack("<IIii", 11, 16, 768, 384),
        struct.pack("<II", 81, 96) + pixels,
    )
    raw = bytearray(emf_bytes(records))
    struct.pack_into("<4i", raw, 8, 0, -384, 768, 0)
    # Illustrator's reference-device frame differs from requested print size.
    # Deliberately keep that distinction observable after restoration.
    struct.pack_into("<4i", raw, 24, 0, -12000, 24000, 0)
    return bytes(raw)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_png_changes_only_resolution_not_compressed_pixels(self):
        before = png_bytes(with_dpi=True)
        path = self.write("figure.png", before)
        info = converter.png_metadata(path, 600)
        after = png_chunks(path.read_bytes())
        self.assertEqual(info, {"width_px": 2, "height_px": 1, "dpi": 600})
        self.assertEqual([p for k, p in png_chunks(before) if k == b"IDAT"],
                         [p for k, p in after if k == b"IDAT"])
        physical = [p for k, p in after if k == b"pHYs"]
        self.assertEqual(len(physical), 1)
        self.assertEqual(struct.unpack(">IIB", physical[0]), (23622, 23622, 1))

    def test_png_rejects_bad_crc_without_mutating_file(self):
        corrupted = bytearray(png_bytes())
        corrupted[-5] ^= 1
        path = self.write("corrupt.png", corrupted)
        with self.assertRaises(ValueError):
            converter.png_metadata(path, 300)
        self.assertEqual(path.read_bytes(), corrupted)

    def test_png_rejects_truncated_output(self):
        path = self.write("truncated.png", png_bytes()[:-8])
        with self.assertRaises(ValueError):
            converter.png_metadata(path, 600)

    def test_png_requires_actual_pixel_data(self):
        path = self.write("empty.png", png_bytes(include_pixels=False))
        with self.assertRaises(ValueError):
            converter.png_metadata(path, 600)

    def test_png_rejects_zero_sized_canvas(self):
        path = self.write("zero.png", png_bytes(width=0))
        with self.assertRaises(ValueError):
            converter.png_metadata(path, 600)

    def test_emf_counts_vector_gradient_and_bitmap_records(self):
        bitmap = struct.pack("<II", 81, 80) + bytes(72)
        gradient = struct.pack("<II", 118, 36) + bytes(28)
        path = self.write("figure.emf", emf_bytes((bitmap, gradient)))
        self.assertEqual(converter.inspect_emf(path),
                         {"records": 4, "bitmap_records": 1, "gradient_records": 1})

    def test_emf_rejects_mismatched_file_length(self):
        path = self.write("truncated.emf", emf_bytes()[:-4])
        with self.assertRaises(ValueError):
            converter.inspect_emf(path)

    def test_emf_rejects_invalid_record_bounds(self):
        raw = bytearray(emf_bytes())
        struct.pack_into("<I", raw, 92, 0xFFFFFFFC)
        path = self.write("bad-record.emf", raw)
        with self.assertRaises(ValueError):
            converter.inspect_emf(path)

    def test_emf_requires_eof_record(self):
        raw = bytearray(emf_bytes())
        struct.pack_into("<I", raw, 88, 3)
        path = self.write("no-eof.emf", raw)
        with self.assertRaises(ValueError):
            converter.inspect_emf(path)

    def test_svg_reports_embedded_images_and_text_instead_of_claiming_all_vector(self):
        path = self.write("mixed.svg", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 10"><defs><linearGradient id="g"/></defs><path d="M0 0L20 10" stroke-dasharray="2 2"/><text>x</text><image href="data:image/png;base64,AA=="/></svg>')
        info = converter.inspect_svg(path)
        self.assertEqual((info["image"], info["text"], info["path"], info["linearGradient"]), (1, 1, 1, 1))
        self.assertEqual(info["dashed_elements"], 1)

    def test_svg_physical_size_preserves_path_precision_and_xml_bytes(self):
        prefix = b'<?xml version="1.0" encoding="UTF-8"?>\n<!-- Illustrator export -->\n'
        body = (b'\n  <path d="M .0000001,2.1234567 C3.000000 4.2,5e-7 6,7 8"'
                b' fill="url(#gradient-1)"/>\n  <linearGradient id="gradient-1"/>\n</svg>')
        original = prefix + b'<svg xmlns="http://www.w3.org/2000/svg" width=\'96px\' height="48px" viewBox="0 0 96 48">' + body
        path = self.write("precision.svg", original)
        converter.set_svg_physical_size(path, 72, 36)
        rewritten = path.read_bytes()
        self.assertTrue(rewritten.startswith(prefix))
        self.assertTrue(rewritten.endswith(body))
        root = ET.fromstring(rewritten)
        self.assertEqual((root.get("width"), root.get("height")), ("72pt", "36pt"))
        self.assertEqual(root.get("viewBox"), "0 0 96 48")
        converter.set_svg_physical_size(path, 144, 72)
        root = ET.parse(path).getroot()
        self.assertEqual((root.get("width"), root.get("height")), ("144pt", "72pt"))
        self.assertTrue(path.read_bytes().endswith(body))

    def test_svg_physical_size_rejects_nonpositive_dimensions_without_write(self):
        original = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'
        path = self.write("bad-size.svg", original)
        with self.assertRaises(ValueError):
            converter.set_svg_physical_size(path, 0, 72)
        self.assertEqual(path.read_bytes(), original)


class NativeEmfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source = self.root / '图 "A".svg'
        self.original = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-5 -10 144 72" width="144pt" height="72pt">'
                         b'<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/></linearGradient></defs>'
                         b'<path id="science" d="M-5,-10 C.123456789 0 144 0 139 62" fill="url(#g)"/></svg>')
        self.source.write_bytes(self.original)

    @staticmethod
    def action_strings(action):
        return [(int(length), bytes.fromhex(hex_data).decode("utf-8"))
                for length, hex_data in re.findall(r'\[\s*(\d+)\s*\n([0-9a-f]+)\s*\n\]', action)]

    def test_action_lengths_are_utf8_byte_lengths_for_unicode_names_and_paths(self):
        path = '/tmp/科研图/实验 "A"（α）.emf'
        set_name = '临时动作_αβ'
        action_name = '导出 EMF'
        values = self.action_strings(native_emf.emf_action(path, set_name, action_name))
        by_value = {value: length for length, value in values}
        for value in (path, set_name, action_name):
            self.assertIn(value, by_value)
            self.assertEqual(by_value[value], len(value.encode("utf-8")))
            self.assertGreater(by_value[value], len(value))

    def fake_run(self, script, app_path, *, fail=False):
        config_line = script.read_text(encoding="utf-8").splitlines()[0]
        config = json.loads(config_line[len("var nativeConfig = "):-1])
        source = Path(config["source"])
        self.assertNotEqual(source.resolve(), self.source.resolve())
        self.assertTrue(source.is_relative_to(self.root))
        root = ET.parse(source).getroot()
        namespace = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual((root.get("width"), root.get("height")), ("576pt", "288pt"))
        path = root.find('.//s:path[@id="science"]', namespace)
        self.assertEqual(path.get("d"), "M-5,-10 C.123456789 0 144 0 139 62")
        self.assertEqual(path.get("fill"), "url(#g)")
        self.assertIsNotNone(root.find('.//s:linearGradient[@id="g"]', namespace))
        group = next(x for x in root if x.tag.endswith("}g"))
        clip_reference = group.get("clip-path")
        clip_id = clip_reference[len("url(#"):-1]
        clip = root.find('.//s:clipPath[@id="' + clip_id + '"]', namespace)
        self.assertEqual(clip.get("clipPathUnits"), "userSpaceOnUse")
        rect = clip.find("s:rect", namespace)
        self.assertEqual([rect.get(k) for k in ("x", "y", "width", "height")], ["-5", "-10", "144", "72"])
        action = Path(config["action"]).read_text(encoding="utf-8")
        values = [value for _, value in self.action_strings(action)]
        self.assertIn(config["setName"], values)
        destination = next(Path(value) for value in values if value.endswith(".emf"))
        self.assertTrue(destination.is_relative_to(script.parent))
        self.seen_sets.append(config["setName"])
        self.seen_sources.append(source)
        if fail:
            return "ERROR: simulated native exporter failure"
        destination.write_bytes(native_emf_bytes())
        return "OK"

    def test_native_export_clips_private_copy_and_uses_distinct_action_sets(self):
        self.seen_sets, self.seen_sources = [], []
        with patch.dict(sys.modules, {"converter": converter}), \
             patch.object(converter, "run_illustrator", side_effect=self.fake_run):
            first = self.root / "first.emf"
            second = self.root / "second.emf"
            info = native_emf.export_svg_emf(self.source, first, "/Applications/Illustrator.app", self.root)
            native_emf.export_svg_emf(self.source, second, "/Applications/Illustrator.app", self.root)
        self.assertTrue(info["artboard_clipped"])
        self.assertEqual(info["raster_supersample_factor"], 4)
        self.assertEqual(info["source_artboard_width_mm"], 50.8)
        self.assertEqual(info["source_artboard_height_mm"], 25.4)
        self.assertEqual(info["emf_frame_width_mm"], 50.8)
        self.assertEqual(info["emf_frame_height_mm"], 25.4)
        self.assertFalse(info["native_physical_size_differs"])
        restored = first.read_bytes()
        self.assertEqual(second.read_bytes(), restored)
        self.assertEqual(struct.unpack_from("<4i", restored, 8), (0, -96, 192, 0))
        self.assertEqual(struct.unpack_from("<4i", restored, 24), (0, -2540, 5080, 0))
        self.assertIn(struct.pack("<IIii", 9, 16, 768, 384), restored)
        self.assertIn(struct.pack("<IIii", 11, 16, 192, 96), restored)
        self.assertIn(b"dense-raster-samples".ljust(88, b"\x00"), restored)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(len(set(self.seen_sets)), 2)
        self.assertEqual(len(set(self.seen_sources)), 2)

    def test_failed_native_export_does_not_publish_partial_target(self):
        self.seen_sets, self.seen_sources = [], []
        target = self.root / "failed.emf"
        with patch.dict(sys.modules, {"converter": converter}), \
             patch.object(converter, "run_illustrator", side_effect=lambda script, app: self.fake_run(script, app, fail=True)):
            with self.assertRaisesRegex(RuntimeError, "simulated native exporter failure"):
                native_emf.export_svg_emf(self.source, target, "/Applications/Illustrator.app", self.root)
        self.assertFalse(target.exists())
        self.assertEqual(self.source.read_bytes(), self.original)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / '图 1 "draft".ai'
        self.original = b"%PDF-1.7\nfixture original bytes\n"
        self.source.write_bytes(self.original)
        self.addCleanup(self.temp.cleanup)
        self.configs = []
        self.probe_patch = patch.object(converter, "probe", return_value={
            "illustrator": {"available": True, "path": "/Applications/Illustrator.app"},
            "libreoffice": {"available": True, "path": "/Applications/LibreOffice.app/Contents/MacOS/soffice"}})
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        lock_patch = patch.object(converter, "illustrator_lock", side_effect=contextlib.nullcontext)
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    def args(self, formats=None, **overrides):
        values = dict(input=str(self.source), formats=formats or ["svg", "png"], dpi=600,
                      output_dir=str(self.root / "exports"), keep_text=False, keep_strokes=False,
                      white_background=False, first_artboard=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def fake_native(self, script, app_path, *, failure=False, boards=1):
        first_line = script.read_text(encoding="utf-8").splitlines()[0]
        config = json.loads(first_line[len("var FC_CONFIG = "):-1])
        self.configs.append(config)
        self.assertNotEqual(Path(config["input"]), self.source)
        self.assertEqual(Path(config["input"]).read_bytes(), self.original)
        folder = Path(config["output"])
        folder.mkdir()
        items = []
        for index in range(1, boards + 1):
            item = {"index": index}
            if config["png"]:
                png = folder / (str(index) + ".png")
                png.write_bytes(png_bytes())
                item["png"] = str(png)
            if config["svg"]:
                svg = folder / (str(index) + ".svg")
                svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L2 1"/></svg>')
                item["svg"] = str(svg)
            items.append(item)
        native = {"success": not failure, "warnings": [], "totalArtboards": boards,
                  "artboards": items, "preprocessing": {"outlined_text_frames": 2}}
        if failure:
            native["error"] = "A later artboard failed after earlier outputs completed"
        Path(config["result"]).write_text(json.dumps(native), encoding="utf-8")
        return ""

    def test_conversion_uses_copy_and_never_overwrites_previous_outputs(self):
        with patch.object(converter, "run_illustrator", side_effect=self.fake_native):
            one = converter.convert(self.args())
            two = converter.convert(self.args())
        self.assertTrue(one["success"])
        self.assertTrue(two["success"])
        self.assertNotEqual(one["output_dir"], two["output_dir"])
        self.assertEqual(self.source.read_bytes(), self.original)
        for item in one["outputs"] + two["outputs"]:
            self.assertTrue(Path(item["path"]).is_file())
        self.assertFalse(Path(self.configs[0]["input"]).exists())
        report = json.loads((Path(one["output_dir"]) / "conversion-report.json").read_text())
        self.assertEqual(report["source"], str(self.source.resolve()))
        self.assertEqual(report["preprocessing"]["outlined_text_frames"], 2)

    def test_multiboard_outputs_have_distinct_names(self):
        with patch.object(converter, "run_illustrator", side_effect=lambda script, app: self.fake_native(script, app, boards=2)):
            result = converter.convert(self.args())
        self.assertTrue(result["success"])
        self.assertEqual(len(result["outputs"]), 4)
        self.assertEqual(len({item["path"] for item in result["outputs"]}), 4)
        self.assertEqual({item["artboard"] for item in result["outputs"]}, {1, 2})

    def test_emf_failure_retains_finished_svg_and_png(self):
        with patch.object(converter, "run_illustrator", side_effect=self.fake_native), \
             patch.object(converter, "export_emf", side_effect=RuntimeError("EMF conversion failed")):
            result = converter.convert(self.args(["svg", "png", "emf"]))
        self.assertFalse(result["success"])
        self.assertIn("EMF conversion failed", result["error"])
        self.assertEqual({x["format"] for x in result["outputs"]}, {"svg", "png"})
        self.assertTrue(all(Path(x["path"]).exists() for x in result["outputs"]))

    def test_native_emf_works_without_libreoffice(self):
        capabilities = {
            "illustrator": {"available": True, "path": "/Applications/Illustrator.app"},
            "libreoffice": {"available": False, "path": None}}

        def fake_emf(svg, target, app_path, scratch):
            self.assertEqual(app_path, capabilities["illustrator"]["path"])
            self.assertTrue(svg.is_file())
            target.write_bytes(emf_bytes())
            return {"records": 2, "bitmap_records": 0, "gradient_records": 0, "engine": "Adobe Illustrator"}

        with patch.object(converter, "probe", return_value=capabilities), \
             patch.object(converter, "run_illustrator", side_effect=self.fake_native), \
             patch.object(converter, "export_emf", side_effect=fake_emf), \
             patch.object(converter, "export_emf_libreoffice", side_effect=AssertionError("LibreOffice must not run")):
            result = converter.convert(self.args(["emf"]))
        self.assertTrue(result["success"], result["error"])
        self.assertEqual([item["format"] for item in result["outputs"]], ["emf"])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_explicit_libreoffice_engine_requires_libreoffice(self):
        capabilities = {
            "illustrator": {"available": True, "path": "/Applications/Illustrator.app"},
            "libreoffice": {"available": False, "path": None}}
        with patch.object(converter, "probe", return_value=capabilities):
            with self.assertRaisesRegex(RuntimeError, "LibreOffice"):
                converter.convert(self.args(["emf"], emf_engine="libreoffice"))
        self.assertFalse((self.root / "exports").exists())

    def test_native_partial_failure_retains_finished_artboards(self):
        with patch.object(converter, "run_illustrator", side_effect=lambda script, app: self.fake_native(script, app, failure=True)):
            result = converter.convert(self.args())
        self.assertFalse(result["success"])
        self.assertIn("later artboard failed", result["error"])
        self.assertEqual({x["format"] for x in result["outputs"]}, {"svg", "png"})

    def test_no_illustrator_stops_before_any_output_write(self):
        with patch.object(converter, "probe", return_value={"illustrator": {"available": False}}):
            with self.assertRaisesRegex(RuntimeError, "Illustrator"):
                converter.convert(self.args())
        self.assertFalse((self.root / "exports").exists())

    def test_native_failure_is_reported_and_source_unchanged(self):
        with patch.object(converter, "run_illustrator", side_effect=RuntimeError("permission denied")):
            result = converter.convert(self.args())
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "permission denied")
        self.assertEqual(self.source.read_bytes(), self.original)
        report = json.loads((Path(result["output_dir"]) / "conversion-report.json").read_text())
        self.assertEqual(report["error"], "permission denied")

    def test_automation_denial_has_actionable_error(self):
        proc = subprocess.CompletedProcess([], 1, "", "Not authorized to send Apple events. (-1743)")
        with patch.object(converter.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "自动化"):
                converter.run_illustrator(Path("/tmp/script.jsx"), "/Applications/Illustrator.app")


if __name__ == "__main__":
    unittest.main()
