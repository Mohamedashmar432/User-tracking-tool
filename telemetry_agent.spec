# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for telemetry_agent.exe
#
# Build command (run from repo root, venv active):
#   pyinstaller telemetry_agent.spec
#
# Output: dist/telemetry_agent.exe  (~15-25 MB single file, no console window)

a = Analysis(
    ["telemetry_agent.py"],
    pathex=[],
    binaries=[],
    # Bundle agent.config.json so the EXE has a bootstrap URL before install
    datas=[("agent.config.json", ".")],
    hiddenimports=[
        "win32api",
        "win32gui",
        "win32process",
        "win32con",
        "psutil",
        "psutil._pswindows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="telemetry_agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no console window in production
    icon=None,
)
