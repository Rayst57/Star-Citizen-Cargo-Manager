# cargo_manager.spec — PyInstaller bundle definition
# Build with:    pyinstaller cargo_manager.spec --clean
#
# Produces:      dist/CargoManager/CargoManager.exe   (one-folder bundle)
# See:           docs/packaging.md

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Pull EVERYTHING PySide6 (and friends) needs in: binaries (Qt DLLs),
# datas (qt.conf / plugins / translations), and hiddenimports (every
# PySide6.QtFoo submodule). Without this, the frozen .exe can fail at
# startup with:  ModuleNotFoundError: No module named 'PySide6'
pyside6_datas,   pyside6_binaries,   pyside6_hidden   = collect_all("PySide6")
shiboken6_datas, shiboken6_binaries, shiboken6_hidden = collect_all("shiboken6")
openai_datas,    openai_binaries,    openai_hidden    = collect_all("openai")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[
        *pyside6_binaries,
        *shiboken6_binaries,
        *openai_binaries,
    ],
    datas=[
        ("data",  "data"),       # schema.sql + seed JSONs + scu_boxes.json + ships.json
        ("theme", "theme"),      # colors.json
        *pyside6_datas,
        *shiboken6_datas,
        *openai_datas,
    ],
    hiddenimports=[
        *pyside6_hidden,
        *shiboken6_hidden,
        *openai_hidden,
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
