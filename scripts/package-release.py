#!/usr/bin/env python3
"""Make reviewed release archives from an explicit public-file allowlist."""
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ".gitignore", "README.md", "LICENSE",
    "app/FigureConverterApp.swift", "app/Info.plist",
    "engine/converter.py", "engine/illustrator.jsx", "engine/native_emf.py",
    "engine/compat_emf.py", "engine/svg_geometry.py", "engine/native_documents.jsx",
    "scripts/build-app.sh", "scripts/package-release.py",
    "tests/create_fixture.jsx", "tests/create_geometry_fixture.jsx",
    "tests/test_converter.py", "tests/test_embedded_emf.py", "tests/test_geometry.py",
    "tests/test_native_emf.py", "tests/test_svg_geometry.py", "tests/test_documents.py",
    "docs/USAGE.md", "docs/PUBLISHING.md", "docs/RELEASE_NOTES.md",
)
PUBLIC_DOCS = ("LICENSE", "README.md", "docs/USAGE.md", "docs/PUBLISHING.md", "docs/RELEASE_NOTES.md")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def package():
    for name in PUBLIC_FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Public source file missing or symlinked: " + name)
    info = plistlib.loads((ROOT / "app/Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected a numeric major.minor.patch version")
    application = ROOT / "dist/Figure-Converter.zip"
    if not application.is_file():
        raise ValueError("Run bash scripts/build-app.sh first")
    prefix = "Figure Converter.app/Contents/"
    expected = set(PUBLIC_DOCS) | {
        prefix + "Info.plist", prefix + "MacOS/FigureConverter",
        prefix + "_CodeSignature/CodeResources",
        prefix + "Resources/build-manifest.json",
        prefix + "Resources/LICENSE", prefix + "Resources/README.md", prefix + "Resources/USAGE.md",
    } | {prefix + "Resources/" + name for name in PUBLIC_FILES if name.startswith("engine/")}
    with zipfile.ZipFile(application) as archive:
        if archive.testzip():
            raise ValueError("The application ZIP failed its CRC check")
        files = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
        if len(files) != len(set(files)) or set(files) != expected:
            raise ValueError("Unexpected or missing application ZIP entries; rebuild and inspect its contents")
        bundled_info = plistlib.loads(archive.read(prefix + "Info.plist"))
        if bundled_info != info:
            raise ValueError("The application version is stale; rebuild first")
        manifest = json.loads(archive.read(prefix + "Resources/build-manifest.json"))
        if manifest.get("version") != version or manifest.get("architecture") != "arm64":
            raise ValueError("This release description targets an arm64 build; check version and architecture")
        required_inputs = {
            "app/FigureConverterApp.swift", "app/Info.plist", "scripts/build-app.sh",
            *PUBLIC_DOCS,
            *(name for name in PUBLIC_FILES if name.startswith("engine/")),
        }
        if set(manifest.get("files", {})) != required_inputs:
            raise ValueError("Build manifest is incomplete; rebuild first")
        for name, recorded_hash in manifest["files"].items():
            if digest((ROOT / name).read_bytes()) != recorded_hash:
                raise ValueError("Source changed since the app was built: " + name)
        for name in PUBLIC_DOCS:
            if archive.read(name) != (ROOT / name).read_bytes():
                raise ValueError("Bundled documentation is stale: " + name)
        for name in PUBLIC_FILES:
            if name.startswith("engine/") and archive.read(prefix + "Resources/" + name) != (ROOT / name).read_bytes():
                raise ValueError("Bundled conversion engine differs: " + name)
    destination = ROOT / "dist" / ("release-v" + version)
    destination.mkdir(exist_ok=True)
    app_target = destination / "Figure-Converter.zip"
    shutil.copy2(application, app_target)
    source_target = destination / ("Figure-Converter-v" + version + "-source.zip")
    with zipfile.ZipFile(source_target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in PUBLIC_FILES:
            archive.write(ROOT / name, "Figure-Converter-v" + version + "/" + name)
    sums = "".join(digest(path.read_bytes()) + "  " + path.name + "\n" for path in (app_target, source_target))
    (destination / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    shutil.copy2(ROOT / "docs/RELEASE_NOTES.md", destination / "RELEASE_NOTES.md")
    print(destination)
    print("Source files: %d; application version: %s; architecture: arm64" % (len(PUBLIC_FILES), version))


if __name__ == "__main__":
    try:
        package()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print("Release packaging stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
