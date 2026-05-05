# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for telemetry_ui  (--onedir)
#
# Build:
#   pyinstaller telemetry_ui.spec
#   Compress-Archive -Path "dist\telemetry_ui\*" -DestinationPath "dist\telemetry_ui.zip" -Force
#
# Upload:
#   az storage blob upload ... --name telemetry_ui.zip --file dist\telemetry_ui.zip

a = Analysis(
    ["telemetry_ui.py"],
    pathex=[],
    binaries=[],
    datas=[("agent.config.json", ".")],
    hiddenimports=[
        "PIL._tkinter_finder",
        "pystray._win32",
        "pkg_resources.py2_warn",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PIL.ImageQt",
        "PIL.ImageTk",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="telemetry_ui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="telemetry_ui",
)
