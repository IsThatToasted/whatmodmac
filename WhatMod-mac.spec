# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_dir = Path.cwd()

datas = []

# Include optional branding/splash assets when present beside this spec.
for asset in ["cover.png", "splash.png", "splash(2).png", "whatmod_license_secret.key"]:
    p = project_dir / asset
    if p.exists():
        datas.append((str(p), "."))

# If build_whatmod.command installed Playwright browsers locally, bundle them.
ms_playwright = project_dir / "ms-playwright"
if ms_playwright.exists():
    datas.append((str(ms_playwright), "ms-playwright"))

# Keep Playwright's Python driver/resources available inside the app.
datas += collect_data_files("playwright")
hiddenimports = collect_submodules("playwright")

block_cipher = None

a = Analysis(
    ["whatmod.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WhatMod",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WhatMod",
)

app = BUNDLE(
    coll,
    name="WhatMod.app",
    icon=None,
    bundle_identifier="com.whatmod.whatmod",
    info_plist={
        "NSHighResolutionCapable": "True",
        "NSRequiresAquaSystemAppearance": "False",
        "LSMinimumSystemVersion": "11.0",
        "CFBundleShortVersionString": "1.6.3",
        "CFBundleVersion": "1.6.3",
    },
)
