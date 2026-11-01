"""Print geometry and script-encoding contracts, without opening Illustrator."""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib


ENGINE = Path(__file__).resolve().parents[1] / "engine" / "converter.py"
spec = importlib.util.spec_from_file_location("geometry_converter", ENGINE)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def png_chunk(kind, data):
    payload = kind + data
    return (struct.pack(">I", len(data)) + payload +
            struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))


def valid_png(width, height):
    """Make real, decodable pixel rows, including the expected native dimensions."""
    compressor = zlib.compressobj()
    row = b"\x00" + b"\x1c\x72\x80\xff" * width
    pixels = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    return (b"\x89PNG\r\n\x1a\n" +
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            png_chunk(b"IDAT", pixels) + png_chunk(b"IEND", b""))


def physical_png_size(path):
    data = path.read_bytes()
    offset = 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        if data[offset + 4:offset + 8] == b"pHYs":
            return struct.unpack_from(">IIB", data, offset + 8)
        offset += length + 12
    raise AssertionError("PNG is missing print-resolution metadata")


class PrintGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "含图片的科研图 α.ai"
        self.original = b"%PDF-1.7\nmock Illustrator source, never modified\n"
        self.source.write_bytes(self.original)
        self.configs = []
        self.capabilities = {
            "illustrator": {"available": True, "path": "/Applications/Illustrator.app"},
            "libreoffice": {"available": False, "path": None},
        }
        # Native execution is mocked; unit tests must not contend with a real
        # conversion or an integration test using the Illustrator lock.
        lock_patch = patch.object(converter, "illustrator_lock", side_effect=contextlib.nullcontext)
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    def args(self, **overrides):
        values = dict(input=str(self.source), formats=["svg", "png", "emf"], dpi=600,
                      output_dir=str(self.root / "exports"), keep_text=False,
                      keep_strokes=False, white_background=False, first_artboard=False,
                      emf_engine="illustrator", trim_artboard=True, width_mm=180.0)
        values.update(overrides)
        return argparse.Namespace(**values)

    def fake_native(self, script, app_path):
        raw_script = script.read_bytes()
        self.assertTrue(raw_script.isascii(), "ExtendScript must not depend on the macOS text encoding")
        line = raw_script.decode("ascii").splitlines()[0]
        config = json.loads(line[len("var FC_CONFIG = "):-1])
        self.configs.append(config)
        self.assertEqual(Path(config["input"]).read_bytes(), self.original)
        self.assertNotEqual(Path(config["input"]).resolve(), self.source.resolve())
        folder = Path(config["output"])
        folder.mkdir()

        # This fixture represents a cropped 2:1 Illustrator artboard. Geometry is
        # native engine output; this test checks the backend consumes it intact.
        width_pt = 180.0 * 72 / 25.4
        height_pt = width_pt / 2
        item = dict(index=1, name="实验结果", width_pt=width_pt, height_pt=height_pt,
                    source_width_pt=144.0, source_height_pt=72.0,
                    original_width_pt=288.0, original_height_pt=216.0,
                    trimmed=True, crop_bounds=[36.0, 180.0, 180.0, 108.0])
        if config["svg"]:
            svg = folder / "1.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="144pt" height="72pt" '
                'viewBox="36 -180 144 72"><path id="detail" '
                'd="M36.0000001 -180L180 -108"/></svg>', encoding="utf-8")
            item["svg"] = str(svg)
        if config["png"]:
            png = folder / "1.png"
            png.write_bytes(valid_png(4252, 2126))
            item["png"] = str(png)
        Path(config["result"]).write_text(json.dumps({
            "success": True, "warnings": [], "totalArtboards": 1,
            "artboards": [item], "preprocessing": {},
        }), encoding="utf-8")
        return ""

    def fake_emf(self, svg, target, app_path, scratch):
        root = ET.parse(svg).getroot()
        self.assertAlmostEqual(float(root.get("width")[:-2]) * 25.4 / 72, 180.0, places=5)
        self.assertAlmostEqual(float(root.get("height")[:-2]) * 25.4 / 72, 90.0, places=5)
        self.assertEqual(root.get("viewBox"), "36 -180 144 72")
        target.write_bytes(b"mock native EMF; inspected by its own exporter")
        return {"records": 2, "bitmap_records": 0, "gradient_records": 0}

    def test_invalid_print_width_stops_before_engine_discovery_or_output_creation(self):
        for width in (0, -1, float("nan"), float("inf"), -float("inf"), 1000.01):
            with self.subTest(width=width), patch.object(converter, "probe") as probe, \
                    patch.object(converter, "run_illustrator") as native:
                with self.assertRaises(ValueError):
                    converter.convert(self.args(width_mm=width))
                probe.assert_not_called()
                native.assert_not_called()
                self.assertFalse((self.root / "exports").exists())

    def test_180mm_width_reaches_all_formats_without_changing_svg_coordinates(self):
        with patch.object(converter, "probe", return_value=self.capabilities), \
                patch.object(converter, "run_illustrator", side_effect=self.fake_native), \
                patch.object(converter, "export_emf", side_effect=self.fake_emf):
            result = converter.convert(self.args())
        self.assertTrue(result["success"], result["error"])
        self.assertEqual(self.configs[0]["widthMm"], 180.0)
        self.assertTrue(self.configs[0]["trimArtboard"])
        outputs = {item["format"]: item for item in result["outputs"]}
        self.assertEqual(set(outputs), {"svg", "png", "emf"})
        for item in outputs.values():
            self.assertAlmostEqual(item["width_mm"], 180.0, places=3)
            self.assertAlmostEqual(item["height_mm"], 90.0, places=3)
        svg = ET.parse(outputs["svg"]["path"]).getroot()
        self.assertEqual(svg.get("viewBox"), "36 -180 144 72")
        self.assertEqual(svg[0].get("d"), "M36.0000001 -180L180 -108")
        self.assertAlmostEqual(float(svg.get("width")[:-2]) / float(svg.get("height")[:-2]), 2)
        png = outputs["png"]
        self.assertEqual((png["width_px"], png["height_px"], png["dpi"]), (4252, 2126, 600))
        ppm_x, ppm_y, unit = physical_png_size(Path(png["path"]))
        self.assertEqual((ppm_x, ppm_y, unit), (23622, 23622, 1))
        self.assertAlmostEqual(png["width_px"] / ppm_x * 1000, 180.0, delta=0.02)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_original_size_and_uncropped_options_are_forwarded_explicitly(self):
        with patch.object(converter, "probe", return_value=self.capabilities), \
                patch.object(converter, "run_illustrator", side_effect=self.fake_native):
            result = converter.convert(self.args(formats=["svg"], trim_artboard=False, width_mm=None))
        self.assertTrue(result["success"], result["error"])
        self.assertFalse(self.configs[0]["trimArtboard"])
        self.assertIsNone(self.configs[0]["widthMm"])

    def test_script_writer_preserves_chinese_and_non_bmp_text_in_ascii_source(self):
        config = {"input": "/tmp/科研图/α𝛼🧪.ai", "warning": "已保留图片；文字转换为轮廓。"}
        message = "渐变、虚线和图片均已保留 🧪\u2028下一行\u2029尾行"
        source = ("var FC_CONFIG = " + json.dumps(config, ensure_ascii=False) + ";\n" +
                  "var message = " + json.dumps(message, ensure_ascii=False) + ";\n// 中文注释\n")
        target = self.root / "warning.jsx"
        converter.write_js_script(target, source)
        encoded = target.read_bytes()
        self.assertTrue(encoded.isascii())
        lines = encoded.decode("ascii").splitlines()
        self.assertEqual(json.loads(lines[0][len("var FC_CONFIG = "):-1]), config)
        self.assertEqual(json.loads(lines[1][len("var message = "):-1]), message)


if __name__ == "__main__":
    unittest.main()
