# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for telemetry_ui.exe
#
# Build (from repo root, with the same venv as the agent):
#   pip install pystray pillow requests
#   pyinstaller telemetry_ui.spec
#
# Output: dist/telemetry_ui.exe  (target < 30 MB, no runtime dependencies)

a = Analysis(
    ["telemetry_ui.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("agent.config.json", "."),   # bundled bootstrap config
    ],
    hiddenimports=[
        "PIL._tkinter_finder",
        "pystray._win32",
        "pkg_resources.py2_warn",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim unused stdlib/PIL extras to keep size down
        "PIL.ImageQt",
        "PIL.ImageTk",   # we don't use ImageTk (canvas only)
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="telemetry_ui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # no console window
    icon=None,       # replace with a .ico path if available
)
