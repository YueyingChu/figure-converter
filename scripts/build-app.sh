#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
destination_app="$repo_root/dist/Figure Converter.app"
destination_zip="$repo_root/dist/Figure-Converter.zip"
build_cache="$repo_root/.build/swift-module-cache"
staging_root="$(mktemp -d /private/tmp/figure-converter-build.XXXXXX)"
app_path="$staging_root/Figure Converter.app"
trap 'rm -rf "$staging_root"' EXIT

mkdir -p "$app_path/Contents/MacOS" "$app_path/Contents/Resources/engine" "$build_cache"
export CLANG_MODULE_CACHE_PATH="$build_cache"
export SWIFT_MODULECACHE_PATH="$build_cache"

/usr/bin/swiftc -swift-version 5 -O -parse-as-library \
  -target "$(uname -m)-apple-macosx13.0" \
  -module-cache-path "$build_cache" \
  -framework SwiftUI -framework AppKit -framework UniformTypeIdentifiers \
  "$repo_root/app/FigureConverterApp.swift" \
  -o "$app_path/Contents/MacOS/FigureConverter"

cp "$repo_root/app/Info.plist" "$app_path/Contents/Info.plist"
cp "$repo_root/LICENSE" "$app_path/Contents/Resources/LICENSE"
cp "$repo_root/README.md" "$app_path/Contents/Resources/README.md"
cp "$repo_root/docs/USAGE.md" "$app_path/Contents/Resources/USAGE.md"
if [[ -d "$repo_root/engine" ]]; then
  /usr/bin/rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "$repo_root/engine/" "$app_path/Contents/Resources/engine/"
else
  printf '%s\n' 'Note: engine/ is not present yet. Rebuild after adding the converter.' >&2
fi

# Record relative source names and hashes, never developer-machine paths.
/usr/bin/python3 - "$repo_root" "$app_path" <<'PY'
import hashlib, json, platform, plistlib, sys
from pathlib import Path
root, app = map(Path, sys.argv[1:])
files = [root / name for name in (
    'app/FigureConverterApp.swift', 'app/Info.plist', 'scripts/build-app.sh',
    'LICENSE', 'README.md', 'docs/USAGE.md', 'docs/PUBLISHING.md', 'docs/RELEASE_NOTES.md')]
files += sorted(path for path in (root / 'engine').iterdir() if path.suffix in ('.py', '.jsx'))
manifest = {'version': plistlib.loads((root / 'app/Info.plist').read_bytes())['CFBundleShortVersionString'],
            'architecture': platform.machine(),
            'files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
(app / 'Contents/Resources/build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY

# Sign outside the file-provider-backed workspace: it can add FinderInfo after
# signing an .app. The ZIP is made here too, without resource forks or attributes.
/usr/bin/xattr -dr com.apple.FinderInfo "$app_path" 2>/dev/null || true
/usr/bin/xattr -dr com.apple.ResourceFork "$app_path" 2>/dev/null || true
/usr/bin/codesign --force --deep --sign - "$app_path"
/usr/bin/codesign --verify --deep --strict "$app_path"
/usr/bin/ditto -c -k --keepParent --norsrc --noextattr \
  "$app_path" "$staging_root/Figure-Converter.zip"

# Include readable installation instructions and the license next to the app.
/usr/bin/python3 - "$repo_root" "$staging_root/Figure-Converter.zip" <<'PY'
import sys, zipfile
from pathlib import Path
root, archive = map(Path, sys.argv[1:])
with zipfile.ZipFile(archive, 'a', compression=zipfile.ZIP_DEFLATED) as output:
    for name in ('LICENSE', 'README.md', 'docs/USAGE.md', 'docs/PUBLISHING.md', 'docs/RELEASE_NOTES.md'):
        output.write(root / name, name)
PY

mkdir -p "$destination_app"
/usr/bin/rsync -a --delete "$app_path/" "$destination_app/"
cp "$staging_root/Figure-Converter.zip" "$destination_zip"
# This is best effort; a file provider may attach FinderInfo again. The ZIP
# remains the clean, verified distribution artifact regardless of that metadata.
/usr/bin/xattr -dr com.apple.FinderInfo "$destination_app" 2>/dev/null || true
/usr/bin/xattr -dr com.apple.ResourceFork "$destination_app" 2>/dev/null || true
printf '%s\n' "$destination_app" "$destination_zip"
