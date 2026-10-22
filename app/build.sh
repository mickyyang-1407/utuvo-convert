#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
TASK_ROOT=$(pwd -P)
export MACOSX_DEPLOYMENT_TARGET=26.0
mkdir -p .build 'build/UTUVO convert.app/Contents/MacOS'
sh prototype/build.sh
swiftc -O -parse-as-library -module-cache-path "$TASK_ROOT/.build/spatial-cache" prototype/vendor/SpatialRenderer.swift backend/spatial_main.swift -o .build/spatial-helper
swiftc -O -parse-as-library -module-cache-path "$TASK_ROOT/.build/ui-cache" app/Localization.swift app/ConverterApp.swift app/IrisArt.swift app/RadialApp.swift -o .build/AudioConverter-new
mv .build/AudioConverter-new 'build/UTUVO convert.app/Contents/MacOS/AudioConverter'
cat > 'build/UTUVO convert.app/Contents/Info.plist' <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>AudioConverter</string><key>CFBundleIdentifier</key><string>local.utuvo.convert.opensource</string><key>CFBundleName</key><string>UTUVO convert</string><key>CFBundleDisplayName</key><string>UTUVO convert</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleLocalizations</key><array><string>zh-Hant</string><string>en</string></array><key>NSHighResolutionCapable</key><true/><key>LSMinimumSystemVersion</key><string>26.0</string></dict></plist>
PLIST
python3 app/bundle_engine.py
python3 app/build_icon.py
