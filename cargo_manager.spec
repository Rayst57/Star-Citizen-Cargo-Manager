# cargo_manager.spec — PyInstaller bundle definition
# Build with:    pyinstaller cargo_manager.spec --clean
#
# Produces:      dist/CargoManager/CargoManager.exe   (one-folder bundle)
# See:           docs/packaging.md

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("data",  "data"),       # schema.sql + seed JSONs + scu_boxes.json + ships.json
        ("theme", "theme"),      # colors.json
    ],
    hiddenimports=[
        "PySide6.QtSvg",
        "sounddevice",
        "pvporcupine",
        "keyring.backends.Windows",
        "pkg_resources.py2_warn",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
    ],
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
    name="CargoManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # set True if UPX is on PATH; saves ~30%
    console=False,               # --windowed: no console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                   # set to "assets/icon.ico" once one exists
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
    name="CargoManager",
)
