"""
Top-level entry script for the standalone Windows build.

Used by PyInstaller (see cargo_manager.spec). For development you can
also run:  python run.py
"""

from __future__ import annotations

import sys

from src.ui.app import launch


if __name__ == "__main__":
    sys.exit(launch())
