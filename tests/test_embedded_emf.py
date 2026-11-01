"""Regression coverage for native raster records and publication dimensions."""

import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from engine.native_emf import (_svg_geometry, export_svg_emf,
                               restore_emf_mapping, UnsupportedEMFMapping)
from engine.compat_emf import _draw_document, export_svg_emf as export_compat_emf


def native_fixture(scale=4, transformed=False):
    width, height = 576 * scale, 384 * scale
    header = bytearray(108)
    struct.pack_into("<II", header, 0, 1, len(header))
    struct.pack_into("<4i", header, 8, 0, -height, width, 0)
    struct.pack_into("<4i", header, 24, 0, -12000 * scale, 18000 * scale, 0)
    header[40:44] = b" EMF"
    struct.pack_into("<4i", header, 72, 1024, 768, 320, 240)
    records = [header, struct.pack("<IIi", 17, 12, 8),
               struct.pack("<IIii", 9, 16, width, height),
               struct.pack("<IIii", 11, 16, width, height)]
    if transformed:
        records.append(struct.pack("<II6f", 35, 32, 1, 0, 0, 1, 2, 3))
    records.extend([struct.pack("<II", 81, 12) + b"PNG!", struct.pack("<II", 14, 8)])
    raw = bytearray(b"".join(records))
    struct.pack_into("<II", raw, 48, len(raw), len(records))
    return bytes(raw)


class EmbeddedEMFTests(unittest.TestCase):
    def test_embedded_image_bounds_native_flattening_work(self):
        root = ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg" width="432pt" '
                             'height="288pt" viewBox="0 0 432 288"><g><image '
                             'href="data:image/png;base64,AA=="/></g></svg>')
        self.assertEqual(_svg_geometry(root)[3], 1)
        root.remove(next(iter(root)))
        self.assertEqual(_svg_geometry(root)[3], 4)

    def test_exact_frame_density_agree_without_changing_bitmap(self):
        source = native_fixture()
        result = restore_emf_mapping(source, 4, 180 / 25.4 * 72,
                                     120 / 25.4 * 72, exact_physical_size=True)
        self.assertEqual(struct.unpack_from("<4i", result, 24), (0, -12000, 18000, 0))
        px, py, mmx, mmy = struct.unpack_from("<4i", result, 72)
        self.assertAlmostEqual(576 / px * mmx, 180)
        self.assertAlmostEqual(384 / py * mmy, 120)
        self.assertEqual(struct.unpack_from("<2i", result, 100), (mmx * 1000, mmy * 1000))
        self.assertEqual(result[108 + 12 + 16 + 16:], source[108 + 12 + 16 + 16:])

    def test_unknown_mapping_falls_back_to_native_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source, output = folder / "source.svg", folder / "output.emf"
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="432pt" '
                              'height="288pt" viewBox="0 0 432 288"><path d="M0 0L432 288"/></svg>')
            factors = []

            def fake_native(script, app_path):
                config = json.loads(script.read_text().splitlines()[0][19:-1])
                root = ET.parse(config["source"]).getroot()
                scale = round(float(root.get("width")[:-2]) / 432)
                factors.append(scale)
                raw = bytearray(native_fixture(scale, True))
                count = struct.unpack_from("<I", raw, 52)[0]
                struct.pack_into("<I", raw, 52, count - 1)
                (script.parent / "figure.emf").write_bytes(raw)
                return "OK"

            with patch("engine.converter.run_illustrator", side_effect=fake_native):
                metadata = export_svg_emf(source, output, "fake", folder)
            self.assertEqual(factors, [4, 1])
            self.assertEqual(output.read_bytes(), native_fixture(1, True))
            self.assertTrue(metadata["coordinate_mapping_fallback"])
            self.assertTrue(metadata["native_physical_size_differs"])

    def test_corrupt_transformed_file_is_not_accepted_as_fallback(self):
        source = bytearray(native_fixture(transformed=True))
        struct.pack_into("<I", source, len(source) - 4, 1000000)
        with self.assertRaises(ValueError) as caught:
            restore_emf_mapping(source, 4, 432, 288)
        self.assertNotIsInstance(caught.exception, UnsupportedEMFMapping)

    def test_native_off_by_one_record_count_is_repaired(self):
        source = bytearray(native_fixture(scale=1))
        actual = struct.unpack_from("<I", source, 52)[0]
        struct.pack_into("<I", source, 52, actual - 1)
        result = restore_emf_mapping(source, 1, 432, 288)
        self.assertEqual(struct.unpack_from("<I", result, 52)[0], actual)
        self.assertEqual(result[:52], source[:52])
        self.assertEqual(result[56:], source[56:])


class CompatibilityEMFTests(unittest.TestCase):
    def test_full_page_frame_keeps_physical_size_and_unicode_uri(self):
        source = Path("/tmp/科研 & 图片/figure.svg")
        root = ET.fromstring(_draw_document(source, 180, 73.068769))
        ns = {"draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
              "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"}
        frame = root.find(".//draw:frame", ns)
        self.assertEqual(frame.get("{" + ns["svg"] + "}width"), "180.000000000mm")
        self.assertEqual(frame.get("{" + ns["svg"] + "}height"), "73.068769000mm")
        image = frame.find("draw:image", ns)
        self.assertEqual(image.get("{http://www.w3.org/1999/xlink}href"), source.resolve().as_uri())

    def test_cropped_output_is_rejected_but_frame_rounding_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source, output = folder / "source.svg", folder / "output.emf"
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="432pt" '
                              'height="288pt" viewBox="10 20 432 288"><image '
                              'href="data:image/png;base64,AA=="/></svg>')
            for width, height, accepted in [(14231, 9151, False), (15239, 10159, True)]:
                def fake_export(command, **kwargs):
                    out = Path(command[command.index("--outdir") + 1]) / "figure.emf"
                    data = bytearray(native_fixture(scale=1))
                    struct.pack_into("<4i", data, 24, 0, 0, width, height)
                    out.write_bytes(data)
                    private_svg = ET.parse(out.parent.parent / "figure.svg").getroot()
                    clip = private_svg.find(".//{http://www.w3.org/2000/svg}clipPath/{http://www.w3.org/2000/svg}rect")
                    self.assertEqual(clip.attrib, {"x": "10", "y": "20", "width": "432", "height": "288"})
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with patch("engine.compat_emf.subprocess.run", side_effect=fake_export):
                    if accepted:
                        metadata = export_compat_emf(source, output, "fake", folder)
                        self.assertAlmostEqual(metadata["emf_frame_width_mm"], 152.39)
                        self.assertTrue(metadata["embedded_image_compatibility"])
                        self.assertTrue(output.is_file())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "画板尺寸"):
                            export_compat_emf(source, output, "fake", folder)
                        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
