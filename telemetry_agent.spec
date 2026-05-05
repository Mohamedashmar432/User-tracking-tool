# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for telemetry_agent  (--onedir)
#
# WHY --onedir:
#   --onefile extracts python311.dll to a NEW _MEI* dir on EVERY startup.
#   Windows Defender scans newly-written DLLs regardless of ExclusionPath rules,
#   and quarantines them.  --onedir writes all DLLs ONCE to the install directory
#   at deploy time (inside a Defender-excluded path) and never touches them again.
#
# Build:
#   pyinstaller telemetry_agent.spec
#   Compress-Archive -Path "dist\telemetry_agent\*" -DestinationPath "dist\telemetry_agent.zip" -Force
#
# Upload:
#   az storage blob upload ... --name telemetry_agent.zip --file dist\telemetry_agent.zip

a = Analysis(
    ["telemetry_agent.py"],
    pathex=[],
    binaries=[],
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
    [],
    exclude_binaries=True,
    name="telemetry_agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="telemetry_agent",
)
