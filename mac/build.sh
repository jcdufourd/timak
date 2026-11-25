#!/bin/bash
# Builds Timak.app: the SwiftUI window plus a copy of the timak Python package.
#   ./build.sh            build into mac/build/Timak.app
#   ./build.sh --install  also copy it to /Applications
# Signs with your "Apple Development" certificate (Personal Team) when there is
# one, otherwise ad hoc. Needs only the Command Line Tools, not Xcode.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
APP=build/Timak.app

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "compiling…"
swiftc -O -swift-version 5 -parse-as-library -target arm64-apple-macos14 \
    -o "$APP/Contents/MacOS/Timak" Sources/*.swift
cp Info.plist "$APP/Contents/"

# the engine: run from inside the app, so the app keeps working if the project folder moves
cp -R "$ROOT/timak" "$APP/Contents/Resources/"
find "$APP/Contents/Resources/timak" -name __pycache__ -prune -exec rm -rf {} +

if [ ! -f build/Timak.icns ] || [ make-icon.swift -nt build/Timak.icns ]; then  # redraw after a change
    echo "drawing the icon…"
    rm -rf build/Timak.iconset
    swift make-icon.swift build/Timak.iconset
    iconutil -c icns -o build/Timak.icns build/Timak.iconset
fi
cp build/Timak.icns "$APP/Contents/Resources/"

IDENTITY=$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development/ { print $2; exit }')
if [ -n "$IDENTITY" ]; then
    codesign --force --sign "$IDENTITY" "$APP"
    echo "signed with $IDENTITY"
else
    codesign --force --sign - "$APP"
    echo "WARNING: no Apple Development certificate found; signed ad hoc."
    echo "         Full Disk Access granted to this build is lost at the next build."
    echo "         Create the certificate in Xcode > Settings > Accounts > Manage Certificates."
fi
codesign --verify --strict "$APP"

if [ "${1:-}" = "--install" ]; then
    rm -rf /Applications/Timak.app
    cp -R "$APP" /Applications/
    echo "installed /Applications/Timak.app"
else
    echo "built $(pwd)/$APP"
fi
