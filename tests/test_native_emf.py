import struct
import unittest
import xml.etree.ElementTree as ET

from engine.native_emf import _svg_geometry, restore_emf_mapping


def fixture():
    header = bytearray(88)
    struct.pack_into("<II", header, 0, 1, 88)
    struct.pack_into("<4i", header, 8, 0, -1536, 2304, 0)
    struct.pack_into("<4i", header, 24, 0, -48000, 72000, 0)
    header[40:44] = b" EMF"
    records = [bytes(header), struct.pack("<IIi", 17, 12, 8),
               struct.pack("<IIii", 10, 16, 0, 0), struct.pack("<IIii", 12, 16, 0, 0),
               struct.pack("<IIii", 9, 16, 2304, 1536),
               struct.pack("<IIii", 11, 16, 2304, 1536),
               struct.pack("<II", 81, 12) + b"DATA", struct.pack("<II", 14, 8)]
    raw = bytearray(b"".join(records))
    struct.pack_into("<II", raw, 48, len(raw), len(records))
    return raw


class NativeEMFTests(unittest.TestCase):
    def test_mapping_keeps_window_and_bitmap_samples(self):
        source = fixture()
        result = restore_emf_mapping(source, 4, 432, 288)
        self.assertEqual(struct.unpack_from("<4i", result, 8), (0, -384, 576, 0))
        self.assertEqual(struct.unpack_from("<4i", result, 24), (0, -12000, 18000, 0))
        self.assertIn(struct.pack("<IIii", 9, 16, 2304, 1536), result)
        self.assertIn(struct.pack("<IIii", 11, 16, 576, 384), result)
        self.assertTrue(result.endswith(struct.pack("<II", 81, 12) + b"DATA" + struct.pack("<II", 14, 8)))

    def test_unknown_mapping_and_truncation_fail(self):
        source = fixture()
        struct.pack_into("<i", source, 96, 1)
        with self.assertRaises(ValueError): restore_emf_mapping(source, 4, 432, 288)
        with self.assertRaises(ValueError): restore_emf_mapping(fixture()[:-1], 4, 432, 288)

    def test_svg_requires_valid_finite_viewbox(self):
        for value in ("", "0 0 nan 288", "0 0 -1 288", "0 0 432"):
            root = ET.Element("svg", {"width": "432pt", "height": "288pt", "viewBox": value})
            with self.assertRaises(ValueError): _svg_geometry(root)

    def test_large_artboard_uses_bounded_quality(self):
        root = ET.Element("svg", {"width": "5000pt", "height": "1000pt", "viewBox": "0 0 5000 1000"})
        self.assertEqual(_svg_geometry(root)[3], 1)


if __name__ == "__main__":
    unittest.main()
