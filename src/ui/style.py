"""
Loads theme/colors.json and produces the QSS stylesheet for the app.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QApplication


_THEME_PATH = Path(__file__).resolve().parents[2] / "theme" / "colors.json"


def load_colors() -> dict[str, str]:
    data = json.loads(_THEME_PATH.read_text(encoding="utf-8"))
    return data["roles"]


def build_qss(colors: dict[str, str]) -> str:
    bg     = colors["background"]
    surf   = colors.get("surface", bg)
    pri    = colors["primary"]
    pri_on = colors["primary_on"]
    accent = colors["accent"]
    acc_on = colors["accent_on"]
    text   = colors["text"]
    muted  = colors["text_muted"]

    return f"""
    QMainWindow, QDialog, QWidget {{
        background-color: {bg};
        color: {text};
        font-family: 'Segoe UI', sans-serif;
        font-size: 13px;
    }}

    QLabel {{
        background: transparent;
    }}
    QLabel[muted="true"] {{
        color: {muted};
    }}
    QLabel[heading="true"] {{
        font-size: 14px;
        font-weight: bold;
        color: {accent};
    }}

    QPushButton {{
        background-color: {pri};
        color: {pri_on};
        border: none;
        border-radius: 4px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{
        background-color: {accent};
        color: {acc_on};
    }}
    QPushButton:disabled {{
        background-color: #2c3a78;
        color: #6f7da8;
    }}
    QPushButton[flat="true"] {{
        background-color: transparent;
        color: {muted};
        padding: 2px 6px;
    }}
    QPushButton[flat="true"]:hover {{
        color: {accent};
    }}

    QFrame#card {{
        background-color: #2a3672;
        border: 1px solid #3a4894;
        border-radius: 6px;
    }}
    QFrame#card[conflict="true"] {{
        background-color: #4a3024;
        border: 1px solid {accent};
    }}

    QFrame#recompute_banner {{
        background-color: {bg};
        border-top: 2px solid {accent};
    }}
    QFrame#recompute_banner QLabel {{
        color: {accent};
        font-weight: bold;
    }}

    QComboBox, QLineEdit, QSpinBox {{
        background-color: #1a2452;
        color: {text};
        border: 1px solid #3a4894;
        border-radius: 3px;
        padding: 3px 6px;
    }}
    QComboBox::drop-down {{
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background-color: #1a2452;
        color: {text};
        selection-background-color: {pri};
    }}

    QScrollArea {{
        border: none;
    }}
    QScrollBar:vertical {{
        background: #1a2452;
        width: 10px;
    }}
    QScrollBar::handle:vertical {{
        background: {pri};
        border-radius: 4px;
    }}

    QStatusBar {{
        background: #181f3f;
        color: {muted};
    }}
    """


def apply_stylesheet(app: QApplication) -> dict[str, str]:
    """Apply the stylesheet to *app*. Returns the colors dict for callers."""
    colors = load_colors()
    app.setStyleSheet(build_qss(colors))
    return colors
