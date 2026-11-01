"""Bitmap density checks for native EMF supersampling; no desktop automation."""
import base64
import importlib.util
from pathlib import Path
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib


ENGINE = Path(__file__).resolve().parents[1] / "engine" / "svg_geometry.py"
spec = importlib.util.spec_from_file_location("figure_svg_geometry", ENGINE)
geometry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(geometry)


def png_uri(width=100, height=100):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = zlib.compress((b"\0" + b"\xff\0\0\x80" * width) * height)
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class BitmapSamplingTests(unittest.TestCase):
    def image(self, width=18.75, height=18.75, extra="", pixels=(100, 100)):
        return '<image width="%s" height="%s" href="%s" %s/>' % (
            width, height, png_uri(*pixels), extra)

    def analyze(self, body, attrs="", width=100, height=100):
        root = ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg" '
                             'viewBox="0 0 100 100" %s>%s</svg>' % (attrs, body))
        return geometry.analyze_embedded_images(root, width, height)

    def test_source_pixels_not_svg_declared_size_determine_density(self):
        safe = self.analyze(self.image())
        self.assertEqual(safe["recommended_factor"], 4)
        self.assertAlmostEqual(safe["max_pixel_scale_at_4x"], 1.0)
        enlarged = self.analyze(self.image(pixels=(50, 50)))
        self.assertEqual(enlarged["recommended_factor"], 1)
        self.assertAlmostEqual(enlarged["max_pixel_scale_at_4x"], 2.0)

    def test_synthetic_low_resolution_png_selects_one(self):
        result = self.analyze(self.image(160, 120, pixels=(160, 120)))
        self.assertEqual(result["recommended_factor"], 1)
        self.assertAlmostEqual(result["max_pixel_scale_at_4x"], 16 / 3)

    def test_rotation_and_nested_affine_transforms_preserve_sampling_density(self):
        for body in ('<g transform="rotate(45 30 30)">' + self.image() + '</g>',
                     '<g transform="scale(2)">' + self.image(extra='transform="scale(.5)"') + '</g>',
                     self.image(extra='transform="translate(500 -200) rotate(90) scale(-1 1)"')):
            with self.subTest(body=body[:60]):
                result = self.analyze(body)
                self.assertEqual(result["recommended_factor"], 4)
                self.assertAlmostEqual(result["max_pixel_scale_at_4x"], 1.0)

    def test_shear_checks_every_direction_not_only_two_image_axes(self):
        result = self.analyze(self.image(extra='transform="matrix(.8 0 .5 .8 0 0)"'))
        # Both transformed basis vectors are shorter than one pixel, but a
        # diagonal direction stretches. A column-length check would miss it.
        self.assertEqual(result["recommended_factor"], 1)
        self.assertGreater(result["max_pixel_scale_at_4x"], 1)

    def test_preserve_aspect_ratio_and_print_scaling(self):
        for attribute, factor in (('preserveAspectRatio="none"', 1),
                                  ('preserveAspectRatio="xMidYMid slice"', 1), ("", 4)):
            with self.subTest(attribute=attribute):
                self.assertEqual(self.analyze(self.image(18.75, 37.5, attribute))["recommended_factor"], factor)
        self.assertEqual(self.analyze(self.image(), width=200, height=200)["recommended_factor"], 1)
        self.assertEqual(self.analyze(self.image(), 'preserveAspectRatio="none"', width=100, height=200)["recommended_factor"], 1)

    def test_each_use_instance_gets_its_own_ancestor_transforms(self):
        definitions = '<defs>' + self.image(40, 40, 'id="bitmap"') + '</defs>'
        small = '<g transform="scale(.25)"><use href="#bitmap"/></g>'
        result = self.analyze(definitions + small)
        self.assertEqual(result["recommended_factor"], 4)
        self.assertEqual(len(result["images"]), 1)
        large = '<use href="#bitmap" transform="scale(3)"/>'
        result = self.analyze(definitions + small + large)
        self.assertEqual(result["recommended_factor"], 1)
        self.assertEqual(len(result["images"]), 2)
        self.assertEqual(self.analyze(definitions)["images"], [])

    def test_unknown_transform_css_and_viewports_fall_back(self):
        bodies = [self.image(extra='transform="matrix3d(1,2,3)"'),
                  self.image(extra='style="scale:20"'),
                  '<style>.a{transform:scale(20)}</style>' + self.image(extra='class="a"'),
                  '<svg width="200" height="200">' + self.image() + '</svg>',
                  '<defs><mask id="m">' + self.image() + '</mask></defs>',
                  self.image(extra='preserveAspectRatio="unknown"')]
        for body in bodies:
            with self.subTest(body=body[:60]):
                self.assertEqual(self.analyze(body)["recommended_factor"], 1)
        result = self.analyze(self.image(), 'transform="rotate(90)" preserveAspectRatio="none"', width=200)
        self.assertEqual(result["reason"], "root_transform")

    def test_external_or_unresolved_raster_references_never_get_loaded(self):
        for body in ('<image width="1" height="1" href="file:///does-not-exist.png"/>',
                     '<use href="https://example.invalid/never-fetch.svg#image"/>',
                     '<filter><feImage href="#anything"/></filter>',
                     '<foreignObject><img src="https://example.invalid/a.png"/></foreignObject>',
                     '<image width="1" height="1" href="data:image/png;base64,broken!"/>'):
            with self.subTest(body=body[:60]):
                self.assertEqual(self.analyze(body)["recommended_factor"], 1)

    def test_cyclic_and_deep_references_fail_conservatively(self):
        cycle = '<defs><g id="a"><use href="#a"/>' + self.image() + '</g></defs><use href="#a"/>'
        self.assertEqual(self.analyze(cycle)["recommended_factor"], 1)
        deep = ET.Element("svg", {"viewBox": "0 0 100 100"})
        parent = deep
        for _ in range(1500):
            parent = ET.SubElement(parent, "g")
        parent.append(ET.fromstring(self.image()))
        result = geometry.analyze_embedded_images(deep, 100, 100)
        self.assertEqual(result["recommended_factor"], 1)
        self.assertEqual(result["reason"], "deeply_nested_geometry")

    def test_conflicting_use_hrefs_fall_back_without_raising(self):
        body = '<use xmlns:xlink="http://www.w3.org/1999/xlink" href="#local" xlink:href="https://example.invalid/remote.svg"/>'
        result = self.analyze(body)
        self.assertEqual(result["recommended_factor"], 1)
        self.assertEqual(result["reason"], "ambiguous_image_reference")


if __name__ == "__main__":
    unittest.main()
