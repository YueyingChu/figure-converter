"""PDF/EPS validation and orchestration tests; native applications are mocked."""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib


ENGINE = Path(__file__).resolve().parents[1] / "engine" / "converter.py"
spec = importlib.util.spec_from_file_location("document_converter", ENGINE)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def pdf_envelope():
    # The lightweight inspector checks envelopes, not PDF object semantics.
    return b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def eps_envelope(bounds="0 0 144 72", hires=None):
    data = "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: " + bounds + "\n"
    if hires is not None:
        data += "%%HiResBoundingBox: " + hires + "\n"
    return (data + "%%EndComments\nshowpage\n%%EOF\n").encode("ascii")


def dos_eps(payload, offset=30, declared_length=None):
    size = len(payload) if declared_length is None else declared_length
    header = struct.pack("<7IH", 0xC6D3D0C5, offset, size, 0, 0, 0, 0, 0xFFFF)
    return header + payload


def one_pixel_png():
    def chunk(kind, data):
        value = kind + data
        return struct.pack(">I", len(data)) + value + struct.pack(">I", zlib.crc32(value) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff")) + chunk(b"IEND", b""))


class DocumentInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_pdf_requires_header_and_end_marker(self):
        path = self.write("figure.pdf", pdf_envelope())
        self.assertTrue(converter.inspect_document(path, "pdf")["header_valid"])
        for content in (b"", b"not a PDF\n%%EOF\n", b"%PDF-1.7\ntruncated document"):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    converter.inspect_document(path, "pdf")

    def test_eps_prefers_precise_bounds_and_allows_negative_origin(self):
        path = self.write("figure.eps", eps_envelope("-6 -11 139 62", "-5.25 -10.5 139 62"))
        info = converter.inspect_document(path, "eps")
        self.assertTrue(info["header_valid"])
        self.assertEqual(list(info["eps_bounds_pt"]), [-5.25, -10.5, 139.0, 62.0])
        self.assertAlmostEqual(info["width_pt"], 144.25)
        self.assertAlmostEqual(info["height_pt"], 72.5)

    def test_eps_reads_bounds_from_dsc_trailer(self):
        content = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: (atend)\n"
                   b"%%EndComments\nshowpage\n%%Trailer\n%%BoundingBox: 0 0 144 72\n%%EOF\n")
        info = converter.inspect_document(self.write("trailer.eps", content), "eps")
        self.assertEqual((info["width_pt"], info["height_pt"]), (144, 72))

    def test_eps_rejects_missing_nonfinite_or_nonpositive_bounds(self):
        contents = [b"", b"%!PS-Adobe-3.0 EPSF-3.0\n%%EOF\n", b"not an EPS\n%%BoundingBox: 0 0 144 72\n"]
        contents += [eps_envelope(bounds) for bounds in (
            "0 0 0 72", "0 0 -1 72", "0 0 144 0", "nan 0 144 72", "0 0 inf 72", "0 -inf 144 72")]
        for i, content in enumerate(contents):
            with self.subTest(content=content):
                path = self.write("invalid-%d.eps" % i, content)
                with self.assertRaises(ValueError):
                    converter.inspect_document(path, "eps")

    def test_dos_preview_eps_requires_complete_postscript_section(self):
        payload = eps_envelope()
        info = converter.inspect_document(self.write("preview.eps", dos_eps(payload)), "eps")
        self.assertEqual((info["width_pt"], info["height_pt"]), (144, 72))
        for content in (dos_eps(payload, offset=100000), dos_eps(payload, declared_length=len(payload) + 100),
                        dos_eps(b"not PostScript")):
            with self.subTest(content=content[:30]):
                path = self.write("bad-preview.eps", content)
                with self.assertRaises(ValueError):
                    converter.inspect_document(path, "eps")


class DocumentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '科研图 "A".ai'
        self.original = b"%PDF-1.7\noriginal saved Illustrator fixture\n%%EOF\n"
        self.source.write_bytes(self.original)
        self.configs = []
        self.document_calls = []
        self.capabilities = {
            "illustrator": {"available": True, "path": "/Applications/Illustrator.app"},
            "libreoffice": {"available": False, "path": None},
        }
        for name, effect in (("probe", lambda: self.capabilities),
                             ("illustrator_lock", contextlib.nullcontext)):
            patcher = patch.object(converter, name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **overrides):
        values = dict(input=str(self.source), formats=["pdf", "eps"], dpi=600,
                      output_dir=str(self.root / "exports"), keep_text=False, keep_strokes=False,
                      white_background=False, first_artboard=False, emf_engine="illustrator",
                      trim_artboard=True, width_mm=180.0)
        values.update(overrides)
        return argparse.Namespace(**values)

    def fake_native(self, script, app_path):
        first = script.read_text(encoding="ascii").splitlines()[0]
        config = json.loads(first[len("var FC_CONFIG = "):-1])
        self.configs.append(config)
        self.assertFalse(config["svg"], "PDF/EPS-only jobs must not export an intermediate SVG")
        self.assertFalse(config["png"])
        self.assertEqual(app_path, self.capabilities["illustrator"]["path"])
        working = Path(config["input"])
        self.assertNotEqual(working.resolve(), self.source.resolve())
        self.assertEqual(working.read_bytes(), self.original)
        checkpoint = Path(config["documentSource"])
        self.assertNotEqual(checkpoint.resolve(), self.source.resolve())
        self.assertTrue(checkpoint.is_relative_to(script.parent))
        checkpoint.parent.mkdir(exist_ok=True, parents=True)
        checkpoint.write_bytes(b"private preprocessed Illustrator checkpoint")
        width_pt = 180.0 / 25.4 * 72
        boards = [{"index": 1, "name": "First", "width_pt": width_pt, "height_pt": width_pt / 2,
                   "crop_bounds": [10, 90, 154, 18], "source_width_pt": 144, "source_height_pt": 72},
                  {"index": 2, "name": "Second", "width_pt": width_pt, "height_pt": width_pt / 3,
                   "crop_bounds": [200, 90, 344, 42], "source_width_pt": 144, "source_height_pt": 48}]
        if config["firstArtboard"]:
            boards = boards[:1]
        native = {"success": True, "warnings": [], "totalArtboards": 2, "artboards": boards,
                  "preprocessing": {"outlined_text_frames": 3}, "documentSource": str(checkpoint)}
        Path(config["result"]).write_text(json.dumps(native), encoding="utf-8")
        return ""

    def fake_documents(self, checkpoint, artboards, formats, app_path, scratch, *, eps_failure=False, invalid_format=None):
        checkpoint = Path(checkpoint)
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(checkpoint.read_bytes(), b"private preprocessed Illustrator checkpoint")
        self.assertNotEqual(checkpoint.resolve(), self.source.resolve())
        self.assertEqual(app_path, self.capabilities["illustrator"]["path"])
        self.assertEqual(set(formats), {"pdf", "eps"})
        self.document_calls.append((checkpoint, artboards, list(formats)))
        outputs, errors = [], []
        for board in artboards:
            for format_name in formats:
                if format_name == "eps" and eps_failure:
                    errors.append("EPS fixture export failed on artboard %s" % board["index"])
                    continue
                path = Path(scratch) / ("document-%s.%s" % (board["index"], format_name))
                if invalid_format == format_name:
                    data = b""
                elif format_name == "pdf":
                    data = pdf_envelope()
                else:
                    bounds = "0 0 %.9f %.9f" % (board["width_pt"], board["height_pt"])
                    data = eps_envelope(bounds)
                path.write_bytes(data)
                outputs.append({"index": board["index"], "format": format_name, "path": str(path),
                                "width_pt": board["width_pt"], "height_pt": board["height_pt"]})
        return {"outputs": outputs, "errors": errors, "warnings": []}

    def run_conversion(self, document_effect=None, **args):
        with patch.object(converter, "run_illustrator", side_effect=self.fake_native), \
                patch.object(converter, "export_native_documents", side_effect=document_effect or self.fake_documents), \
                patch.object(converter, "export_emf", side_effect=AssertionError("EMF route must not run")), \
                patch.object(converter, "inspect_svg", side_effect=AssertionError("SVG route must not run")):
            return converter.convert(self.args(**args))

    def test_pdf_eps_only_preserves_source_and_outputs_each_artboard_at_requested_width(self):
        result = self.run_conversion()
        self.assertTrue(result["success"], result["error"])
        self.assertEqual(len(result["outputs"]), 4)
        self.assertEqual({item["format"] for item in result["outputs"]}, {"pdf", "eps"})
        self.assertEqual({item["artboard"] for item in result["outputs"]}, {1, 2})
        self.assertEqual(len({item["path"] for item in result["outputs"]}), 4)
        for item in result["outputs"]:
            self.assertAlmostEqual(item["width_mm"], 180.0, places=4)
            self.assertAlmostEqual(item["height_mm"], 90.0 if item["artboard"] == 1 else 60.0, places=4)
            self.assertTrue(Path(item["path"]).is_file())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(Path(self.configs[0]["documentSource"]).exists())

    def test_first_artboard_limits_both_document_formats(self):
        result = self.run_conversion(first_artboard=True)
        self.assertTrue(result["success"], result["error"])
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual({item["artboard"] for item in result["outputs"]}, {1})

    def test_eps_failure_keeps_completed_pdf_outputs(self):
        result = self.run_conversion(document_effect=lambda *args: self.fake_documents(*args, eps_failure=True))
        self.assertFalse(result["success"])
        self.assertIn("EPS fixture export failed", result["error"])
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual({item["format"] for item in result["outputs"]}, {"pdf"})
        self.assertTrue(all(Path(item["path"]).is_file() for item in result["outputs"]))
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_invalid_document_is_not_published(self):
        for format_name in ("pdf", "eps"):
            with self.subTest(format=format_name):
                result = self.run_conversion(document_effect=lambda *args: self.fake_documents(*args, invalid_format=format_name))
                self.assertFalse(result["success"])
                self.assertNotIn(format_name, {item["format"] for item in result["outputs"]})
                self.assertFalse(list(Path(result["output_dir"]).glob("*." + format_name)))
                self.assertEqual({item["format"] for item in result["outputs"]}, {"pdf", "eps"} - {format_name})

    def test_nonfinite_native_pdf_dimensions_are_rejected_without_discarding_eps(self):
        def invalid_dimensions(*args):
            response = self.fake_documents(*args)
            for output in response["outputs"]:
                if output["format"] == "pdf":
                    output["width_pt"] = float("nan")
            return response

        result = self.run_conversion(document_effect=invalid_dimensions)
        self.assertFalse(result["success"])
        self.assertEqual({item["format"] for item in result["outputs"]}, {"eps"})
        self.assertFalse(list(Path(result["output_dir"]).glob("*.pdf")))

    def test_eps_bounds_must_agree_with_reported_physical_dimensions(self):
        def wrong_bounds(*args):
            response = self.fake_documents(*args)
            for output in response["outputs"]:
                if output["format"] == "eps":
                    Path(output["path"]).write_bytes(eps_envelope("0 0 144 72"))
            return response

        result = self.run_conversion(document_effect=wrong_bounds)
        self.assertFalse(result["success"])
        self.assertEqual({item["format"] for item in result["outputs"]}, {"pdf"})
        self.assertFalse(list(Path(result["output_dir"]).glob("*.eps")))

    def test_failed_checkpoint_keeps_png_and_skips_document_exporter(self):
        def png_with_checkpoint_error(script, app_path):
            first = script.read_text(encoding="ascii").splitlines()[0]
            config = json.loads(first[len("var FC_CONFIG = "):-1])
            self.assertTrue(config["png"])
            self.assertFalse(config["svg"])
            self.assertTrue(config["documentSource"])
            folder = Path(config["output"])
            folder.mkdir()
            png = folder / "completed.png"
            png.write_bytes(one_pixel_png())
            native = {"success": True, "warnings": [], "totalArtboards": 1,
                      "preprocessing": {}, "documentError": "checkpoint export failed",
                      "artboards": [{"index": 1, "png": str(png), "crop_bounds": [0, 72, 144, 0],
                                     "width_pt": 144, "height_pt": 72}]}
            Path(config["result"]).write_text(json.dumps(native), encoding="utf-8")
            return ""

        with patch.object(converter, "run_illustrator", side_effect=png_with_checkpoint_error), \
                patch.object(converter, "export_native_documents", side_effect=AssertionError("A failed checkpoint must not be opened")):
            result = converter.convert(self.args(formats=["png", "pdf"]))
        self.assertFalse(result["success"])
        self.assertIn("checkpoint export failed", result["error"])
        self.assertEqual([item["format"] for item in result["outputs"]], ["png"])
        self.assertTrue(Path(result["outputs"][0]["path"]).is_file())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_repeated_document_exports_do_not_overwrite_previous_files(self):
        first = self.run_conversion()
        second = self.run_conversion()
        self.assertTrue(first["success"], first["error"])
        self.assertTrue(second["success"], second["error"])
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertTrue(all(Path(item["path"]).is_file() for item in first["outputs"] + second["outputs"]))


class NativeDocumentHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkpoint = self.root / "prepared.ai"
        self.checkpoint.write_bytes(b"private native document")
        self.artboards = [
            {"index": 1, "crop_bounds": [10, 90, 154, 18], "width_pt": 288, "height_pt": 144},
            {"index": 2, "crop_bounds": [200, 90, 344, 42], "width_pt": 288, "height_pt": 96},
        ]
        self.configs = []

    def native_response(self, script, app_path, mutate=None):
        raw = script.read_bytes()
        self.assertTrue(raw.isascii())
        first = raw.decode("ascii").splitlines()[0]
        config = json.loads(first[len("var documentConfig = "):-1])
        self.configs.append(config)
        self.assertEqual(Path(config["source"]), self.checkpoint)
        self.assertEqual(app_path, "/Applications/Illustrator.app")
        outputs = []
        for requested, entry in zip(self.artboards, config["entries"]):
            self.assertEqual(entry["bounds"], requested["crop_bounds"])
            self.assertEqual((entry["width_pt"], entry["height_pt"]),
                             (requested["width_pt"], requested["height_pt"]))
            self.assertNotIn("svg", entry)
            self.assertNotIn("png", entry)
            for format_name in ("pdf", "eps"):
                path = Path(entry[format_name])
                self.assertTrue(path.is_relative_to(script.parent))
                self.assertNotEqual(path, self.checkpoint)
                outputs.append({"index": entry["index"], "format": format_name, "path": str(path),
                                "width_pt": entry["width_pt"], "height_pt": entry["height_pt"]})
        response = {"outputs": outputs, "errors": [], "warnings": ["native fixture note"]}
        if mutate:
            mutate(response)
        Path(config["result"]).write_text(json.dumps(response), encoding="utf-8")
        return ""

    def invoke(self, mutate=None, name="run"):
        scratch = self.root / name
        scratch.mkdir()
        with patch.object(converter, "run_illustrator", side_effect=lambda script, app: self.native_response(script, app, mutate)):
            return converter.export_native_documents(self.checkpoint, self.artboards,
                                                     ["svg", "pdf", "eps"], "/Applications/Illustrator.app", scratch)

    def test_helper_keeps_native_crop_geometry_and_filters_requested_formats(self):
        result = self.invoke()
        self.assertEqual(len(result["outputs"]), 4)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], ["native fixture note"])
        self.assertEqual({item["format"] for item in result["outputs"]}, {"pdf", "eps"})
        self.assertEqual(self.checkpoint.read_bytes(), b"private native document")

    def test_missing_requested_output_adds_error_and_keeps_completed_outputs(self):
        result = self.invoke(lambda response: response["outputs"].pop())
        self.assertEqual(len(result["outputs"]), 3)
        self.assertTrue(result["errors"])
        self.assertIn("2", result["errors"][0])
        self.assertIn("EPS", result["errors"][0])

    def test_duplicate_wrong_path_and_unrequested_format_are_rejected(self):
        mutations = {
            "duplicate": lambda response: response["outputs"].append(dict(response["outputs"][0])),
            "wrong-path": lambda response: response["outputs"][0].update(path=str(self.root / "unexpected.pdf")),
            "wrong-format": lambda response: response["outputs"][0].update(format="svg"),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name), self.assertRaises(RuntimeError):
                self.invoke(mutate, name=name)
        self.assertEqual(self.checkpoint.read_bytes(), b"private native document")

    def test_missing_checkpoint_stops_before_native_execution(self):
        with patch.object(converter, "run_illustrator") as native:
            with self.assertRaises(RuntimeError):
                converter.export_native_documents(self.root / "missing.ai", self.artboards,
                                                     ["pdf", "eps"], "/Applications/Illustrator.app", self.root)
        native.assert_not_called()
        self.assertFalse((self.root / "documents").exists())


if __name__ == "__main__":
    unittest.main()
