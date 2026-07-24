#!/bin/bash
# Build "MLX Audio Separator.app" and install it to /Applications.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="MLX Audio Separator"
BUILD="$HERE/build"
APP="$BUILD/$APP_NAME.app"
VENV_PY="$HOME/.venvs/mlx-audio-separator/bin/python"
BUNDLE_ID="com.rewon.mlxaudioseparator"
# Single source of truth: APP_VERSION in app.py.
VERSION="$(grep -m1 '^APP_VERSION' "$HERE/app.py" | sed -E 's/[^"]*"([^"]+)".*/\1/')"

echo "==> Clean build dir"
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Render icon"
swift "$HERE/make_icon.swift" "$BUILD/icon_1024.png" >/dev/null

echo "==> Build .icns"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz         "$BUILD/icon_1024.png" --out "$ICONSET/icon_${sz}x${sz}.png"    >/dev/null
  sips -z $((sz*2)) $((sz*2)) "$BUILD/icon_1024.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "==> Copy app source"
cp "$HERE/app.py" "$APP/Contents/Resources/app.py"
cp "$HERE/runner.py" "$APP/Contents/Resources/runner.py"
# Vendored tkdnd (drag-and-drop) — app.py finds it next to itself under vendor/.
cp -R "$HERE/vendor" "$APP/Contents/Resources/vendor"

echo "==> Write launcher"
cat > "$APP/Contents/MacOS/launcher" <<LAUNCH
#!/bin/bash
# Force the arm64 slice: the venv Python is universal2 and MLX/numpy are arm64-only,
# so if launched from a Rosetta/x86_64 session it must not run the x86_64 slice.
DIR="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
exec /usr/bin/arch -arm64 "$VENV_PY" "\$DIR/app.py" "\$@"
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"

echo "==> Write Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>15.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
</dict>
</plist>
PLIST

echo "==> Ad-hoc codesign"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "   (codesign warning ignored)"

echo "==> Install to /Applications"
DEST="/Applications/$APP_NAME.app"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
# Refresh Launch Services so icon/name show up immediately.
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true

echo "==> Done: $DEST"
