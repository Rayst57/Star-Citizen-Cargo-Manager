# cargo_manager.spec — PyInstaller bundle definition
# Build with:    pyinstaller cargo_manager.spec --clean
#
# Produces:      dist/CargoManager/CargoManager.exe   (one-folder bundle)
# See:           docs/packaging.md
#
# Size policy: we use ONLY QtCore + QtGui + QtWidgets (no WebEngine,
# Quick/QML, Multimedia, Charts, 3D, Designer, Help, etc.). The earlier
# spec called collect_all("PySide6") which dragged the lot in and
# ballooned the bundle to ~700 MB; the targeted list below trims it to
# roughly a quarter of that.

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# openai's data files (cacert.pem, tiktoken encodings shipped with the
# package) ARE needed for runtime HTTPS + tokenisation; collect_all
# handles them while the dependency itself stays modest.
openai_datas, openai_binaries, openai_hidden = collect_all("openai")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[
        *openai_binaries,
    ],
    datas=[
        ("data",  "data"),       # schema.sql + seed JSONs + scu_boxes.json + ships.json
        ("theme", "theme"),      # colors.json
        *openai_datas,
    ],
    hiddenimports=[
        # Only the three Qt submodules we actually import. PyInstaller's
        # built-in hooks for these (hook-PySide6.QtWidgets etc.) drag in
        # the platform plugin (qwindows.dll) and the matching DLLs
        # automatically, so we don't need to enumerate Qt binaries here.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "shiboken6",
        *openai_hidden,
        "keyring.backends.Windows",
        "pkg_resources.py2_warn",
        # Screen-capture deps — imported lazily inside the
        # ScreenCaptureDialog so PyInstaller's static analysis misses
        # them. Listing them here forces the bundle to include them.
        "mss",
        "mss.windows",        # Windows backend module mss loads at runtime
        "pygetwindow",
        "pygetwindow._pygetwindow_win",  # Windows backend
        # Global hotkey listener for the Quick Capture workflow —
        # imported lazily in src/ui/quick_capture.py.
        "keyboard",
        "keyboard._winkeyboard",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Standard library bloat we never use.
        "tkinter",
        "test",
        "unittest",
        "pydoc_data",
        # Scientific stack — none of it is on our import path; excluding
        # in case a transitive dep (e.g. openai's optional extras) tries
        # to pull it in. Each of these is tens of MB if it sneaks in.
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        # PySide6 modules we deliberately do NOT ship. Each entry below
        # is in the 5-300 MB range when included, and the earlier
        # collect_all("PySide6") swept them all in. WebEngine alone
        # accounts for hundreds of MB.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets",
        "PySide6.QtSql",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        "PySide6.QtTest",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtNetworkAuth",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSpatialAudio",
        "PySide6.QtStateMachine",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTextToSpeech",
        "PySide6.QtConcurrent",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


# Drop bulky Qt DLLs / plugins / translations that slip past `excludes`
# because PyInstaller's PySide6 hook still adds them when collecting
# the bits we DO use.
def _trim_pyside_bloat(tocs):
    """Walk a TOC list (binaries/datas) and drop entries that belong to
    Qt modules we don't ship."""
    DROP_TOKENS = (
        "qt6webenginecore", "qt6webengine", "qt6webchannel",
        "qt6websockets", "qt6charts", "qt6datavisualization",
        "qt63d", "qt6multimedia", "qt6qml", "qt6quick",
        "qt6pdf", "qt6designer", "qt6help", "qt6uitools",
        "qt6test", "qt6bluetooth", "qt6nfc", "qt6positioning",
        "qt6location", "qt6sensors", "qt6serialport",
        "qt6networkauth", "qt6remoteobjects", "qt6scxml",
        "qt6spatialaudio", "qt6statemachine", "qt6svg",
        "qt6texttospeech", "qt6opengl",
        # Resource directories the Qt deployment hook drops in wholesale.
        "/qml/", "\\qml\\",
        "/translations/qtwebengine_", "\\translations\\qtwebengine_",
        "/resources/qtwebengine", "\\resources\\qtwebengine",
        # Other heavyweights that occasionally tag along.
        "opengl32sw", "d3dcompiler_47", "libegl", "libglesv2",
    )
    trimmed = []
    for entry in tocs:
        # PyInstaller TOC entries are (dest_name, src_path, type).
        dest = entry[0].lower().replace("\\", "/")
        if any(tok in dest for tok in DROP_TOKENS):
            continue
        trimmed.append(entry)
    return trimmed


a.binaries = _trim_pyside_bloat(a.binaries)
a.datas = _trim_pyside_bloat(a.datas)

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
