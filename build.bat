@echo off
REM Star Citizen Cargo Manager — Windows build script
REM
REM Prereqs: Python 3.12+, then:    pip install -r requirements.txt
REM
REM Output:                          dist\CargoManager\CargoManager.exe

setlocal

echo === Running test suite ===
python -c "import pytest" 1>nul 2>nul
if errorlevel 1 (
    echo pytest not installed in this Python; skipping tests.
    echo To enable: pip install pytest
) else (
    python -m pytest tests\ -q
    if errorlevel 1 (
        echo Tests failed. Aborting build.
        exit /b 1
    )
)

echo.
echo === Building bundle with PyInstaller ===
python -c "import PyInstaller" 1>nul 2>nul
if errorlevel 1 (
    echo PyInstaller not installed.
    echo Install:  pip install pyinstaller
    exit /b 1
)
python -m PyInstaller cargo_manager.spec --clean --noconfirm
if errorlevel 1 (
    echo PyInstaller build failed.
    exit /b 1
)

echo.
echo === Smoke-testing the bundle ===
if not exist "dist\CargoManager\CargoManager.exe" (
    echo Build did not produce CargoManager.exe.
    exit /b 1
)

echo.
echo Build complete:  dist\CargoManager\
echo To run:          dist\CargoManager\CargoManager.exe
echo To distribute:   zip the contents of dist\CargoManager\
endlocal
