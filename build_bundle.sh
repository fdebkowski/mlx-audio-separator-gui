#!/bin/bash
# Build a fully self-contained "MLX Audio Separator.app" with PyInstaller:
# embeds the Python interpreter and the whole MLX separation engine so it runs
# on any Apple Silicon Mac with no venv/Python install. Produces a zip in dist/
# ready to attach to a GitHub release.
#
# Requires the dev venv from setup.sh plus PyInstaller:
#   ~/.venvs/mlx-audio-separator/bin/pip install pyinstaller
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="MLX Audio Separator"
VENV="$HOME/.venvs/mlx-audio-separator"
PY="$VENV/bin/python"
PYI="$VENV/bin/pyinstaller"
BUNDLE_ID="com.rewon.mlxaudioseparator"
# Single source of truth: APP_VERSION in app.py.
VERSION="$(grep -m1 '^APP_VERSION' "$HERE/app.py" | sed -E 's/[^"]*"([^"]+)".*/\1/')"
WORK="$HERE/bundle_work"
DIST="$HERE/dist"

# Static arm64 ffmpeg/ffprobe embedded so the app needs no Homebrew install.
# Pinned to a release of eugeneware/ffmpeg-static and verified by sha256.
FFMPEG_TAG="b6.1.1"
FFMPEG_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/$FFMPEG_TAG"
FFMPEG_SHA256="a90e3db6a3fd35f6074b013f948b1aa45b31c6375489d39e572bea3f18336584"
FFPROBE_SHA256="bb2db6f5d8cef919da12fbf592119a987202a8c060a886f3cab091f9cab90b64"
FF_CACHE="$HERE/.cache/ffmpeg"   # persists across builds (WORK is wiped each run)

if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
  echo "ERROR: The bundle must be built on Apple Silicon (MLX is arm64-only)." >&2
  exit 1
fi
[[ -x "$PYI" ]] || { echo "ERROR: PyInstaller not found. Run: $VENV/bin/pip install pyinstaller" >&2; exit 1; }

echo "==> Render icon"
rm -rf "$WORK"; mkdir -p "$WORK"
swift "$HERE/make_icon.swift" "$WORK/icon_1024.png" >/dev/null
ICONSET="$WORK/AppIcon.iconset"; mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz             "$WORK/icon_1024.png" --out "$ICONSET/icon_${sz}x${sz}.png"    >/dev/null
  sips -z $((sz*2)) $((sz*2)) "$WORK/icon_1024.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK/AppIcon.icns"

echo "==> Run PyInstaller (this bundles the interpreter + engine; takes a minute)"
rm -rf "$DIST/$APP_NAME.app"
arch -arm64 "$PYI" \
  --name "$APP_NAME" \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$WORK/AppIcon.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --target-arch arm64 \
  --workpath "$WORK/pyi" \
  --specpath "$WORK" \
  --distpath "$DIST" \
  --collect-all mlx \
  --collect-all mlx_audio_separator \
  --collect-all mlx_audio_io \
  --collect-all mlx_spectro \
  --collect-all certifi \
  --hidden-import lameenc \
  --hidden-import runner \
  --hidden-import app \
  "$HERE/main_bundle.py"

APP="$DIST/$APP_NAME.app"

echo "==> Fetch static ffmpeg/ffprobe (arm64, cached in .cache/ffmpeg)"
mkdir -p "$FF_CACHE"
fetch_verify() {  # url dest expected_sha
  local url="$1" dest="$2" sha="$3"
  if [[ ! -f "$dest" ]] || [[ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" != "$sha" ]]; then
    echo "   downloading $(basename "$dest")…"
    curl -fL --retry 3 -o "$dest" "$url"
  fi
  local got; got="$(shasum -a 256 "$dest" | cut -d' ' -f1)"
  [[ "$got" == "$sha" ]] || { echo "ERROR: checksum mismatch for $dest ($got != $sha)" >&2; exit 1; }
}
fetch_verify "$FFMPEG_BASE/ffmpeg-darwin-arm64"  "$FF_CACHE/ffmpeg"  "$FFMPEG_SHA256"
fetch_verify "$FFMPEG_BASE/ffprobe-darwin-arm64" "$FF_CACHE/ffprobe" "$FFPROBE_SHA256"
[[ -f "$FF_CACHE/LICENSE" ]] || curl -fL --retry 3 -o "$FF_CACHE/LICENSE" "$FFMPEG_BASE/darwin-arm64.LICENSE"
chmod +x "$FF_CACHE/ffmpeg" "$FF_CACHE/ffprobe"

echo "==> Embed ffmpeg in the app (Contents/Resources/ffmpeg)"
FFDIR="$APP/Contents/Resources/ffmpeg"
mkdir -p "$FFDIR"
cp "$FF_CACHE/ffmpeg" "$FF_CACHE/ffprobe" "$FF_CACHE/LICENSE" "$FFDIR/"
# GPL compliance: ship the license and a written offer for the corresponding source.
cat > "$FFDIR/README.txt" <<EOF
This app bundles static ffmpeg and ffprobe binaries (arm64) used only to decode
and encode audio. They are FFmpeg $FFMPEG_TAG from
  $FFMPEG_BASE
built with --enable-gpl --enable-version3, so they are licensed under the GNU
GPL v3. The full license text is in the LICENSE file next to this note.

The corresponding source for these binaries is FFmpeg, available at
https://ffmpeg.org and https://git.ffmpeg.org/ffmpeg.git . The prebuilt binaries
and their build recipe come from https://github.com/eugeneware/ffmpeg-static .
EOF

echo "==> Patch Info.plist (version + minimum system)"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"
# MLX 0.31.2 (and mlx-audio-io, which pins it) ships macOS-15-only wheels, so
# the bundled engine dylibs are built minos 15.0. Declaring the true minimum
# makes macOS refuse launch on older systems with a clear dialog instead of the
# app opening and crashing the moment it loads a model.
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 15.0" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 15.0" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSApplicationCategoryType string public.app-category.music" "$PLIST" 2>/dev/null || true

echo "==> Ad-hoc codesign (deep: finalizes every nested binary's signature)"
codesign --force --deep --sign - --timestamp=none "$APP" >/dev/null 2>&1 || echo "   (codesign warning ignored)"

echo "==> Repair mlx_audio_io RECORD hash (PyInstaller rewrote the binary)"
"$PY" "$HERE/bundle_fix_record.py" "$APP"

echo "==> Re-seal app bundle (non-deep: keeps the patched .so bytes intact)"
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || echo "   (codesign warning ignored)"

echo "==> Zip for release"
ZIP="$DIST/MLX-Audio-Separator-$VERSION-macos-arm64.zip"
rm -f "$ZIP"
# ditto preserves the bundle's symlinks/signature; -k makes a PKZip archive.
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

SIZE=$(du -sh "$APP" | cut -f1)
echo
echo "==> Done"
echo "    App: $APP  ($SIZE)"
echo "    Zip: $ZIP"
