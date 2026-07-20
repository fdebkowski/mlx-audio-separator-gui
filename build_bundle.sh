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
VERSION="1.1.0"
WORK="$HERE/bundle_work"
DIST="$HERE/dist"

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
  --hidden-import lameenc \
  --hidden-import runner \
  --hidden-import app \
  "$HERE/main_bundle.py"

APP="$DIST/$APP_NAME.app"

echo "==> Patch Info.plist (version + minimum system)"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 12.0" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$PLIST"
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
