#!/bin/bash
# Builds TrendGame.app for macOS using PyInstaller.
# Output: dist/TrendGame.app
set -e
cd "$(dirname "$0")"

echo "Installing build dependencies..."
pip3 install -q -r requirements.txt
pip3 install -q pyinstaller

echo "Building TrendGame.app..."
pyinstaller \
  --name TrendGame \
  --windowed \
  --noconfirm \
  --clean \
  --add-data "templates:templates" \
  --add-data "terms:terms" \
  --hidden-import "requests" \
  launcher.py

echo ""
echo "  Done → dist/TrendGame.app"
echo "  Drag it to /Applications or double-click to run."
echo ""
