"""
Star Citizen Cargo Manager — entry point.

Run from the repo root:    python -m src.main
After PyInstaller bundle:  CargoManager.exe
"""

from __future__ import annotations

import sys

from .ui.app import launch


if __name__ == "__main__":
    sys.exit(launch())
