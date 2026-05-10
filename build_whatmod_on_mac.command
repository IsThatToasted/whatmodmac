#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

echo "====================================================="
echo "Building WhatMod.app on macOS"
echo "====================================================="

python3 --version >/dev/null 2>&1 || { echo "ERROR: python3 is required."; exit 1; }

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-mac.txt
python -m playwright install chromium
rm -rf build dist
python -m PyInstaller --clean --noconfirm WhatMod-mac.spec

if [ -d "dist/WhatMod.app" ]; then
  echo "SUCCESS: dist/WhatMod.app created."
else
  echo "ERROR: Build failed. Review output above."
  exit 1
fi
