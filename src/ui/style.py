"""
Loads theme/colors.json and produces the QSS stylesheet for the app.

The current palette evokes Star Citizen's mobiglass UI: a deep
near-black background, cyan accents reminiscent of holographic
displays, amber for warnings and call-outs, and thin glowing borders
on interactive surfaces.
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
    accent_bright = colors.get("accent_bright", accent)
    acc_on = colors["accent_on"]
    text   = colors["text"]
    muted  = colors["text_muted"]
    border = colors.get("border", "#264a5c")
    amber  = colors.get("secondary_accent", "#ff8a3c")

    return f"""
    /* ── Base ─────────────────────────────────────────────────── */
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
        color: {accent_bright};
        letter-spacing: 0.5px;
    }}

    /* ── Buttons ─────────────────────────────────────────────── */
    QPushButton {{
        background-color: transparent;
        color: {accent_bright};
        border: 1px solid {accent};
        border-radius: 2px;
        padding: 6px 14px;
        font-weight: 500;
        letter-spacing: 0.4px;
    }}
    QPushButton:hover {{
        background-color: {pri};
        color: {pri_on};
        border-color: {accent_bright};
    }}
    QPushButton:pressed {{
        background-color: {accent};
        color: {acc_on};
    }}
    QPushButton:disabled {{
        background-color: transparent;
        color: {muted};
        border-color: {border};
    }}
    QPushButton[flat="true"] {{
        background-color: transparent;
        color: {muted};
        border: none;
        padding: 2px 6px;
    }}
    QPushButton[flat="true"]:hover {{
        color: {accent_bright};
        background: transparent;
    }}

    /* ── Cards / panels ──────────────────────────────────────── */
    QFrame#card {{
        background-color: {surf};
        border: 1px solid {border};
        border-radius: 2px;
    }}
    QFrame#card[conflict="true"] {{
        background-color: {surf};
        border: 1px solid {amber};
    }}

    /* ── Recompute banner ────────────────────────────────────── */
    QFrame#recompute_banner {{
        background-color: {bg};
        border-top: 1px solid {amber};
    }}
    QFrame#recompute_banner QLabel {{
        color: {amber};
        font-weight: bold;
        letter-spacing: 0.5px;
    }}

    /* ── Inputs ──────────────────────────────────────────────── */
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {bg};
        color: {text};
        border: 1px solid {border};
        border-radius: 2px;
        padding: 4px 8px;
        selection-background-color: {pri};
        selection-color: {pri_on};
    }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {accent};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 16px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {surf};
        color: {text};
        border: 1px solid {accent};
        selection-background-color: {pri};
        selection-color: {pri_on};
    }}

    QCheckBox {{
        color: {text};
        spacing: 6px;
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {border};
        background: {bg};
        border-radius: 1px;
    }}
    QCheckBox::indicator:checked {{
        background: {accent};
        border: 1px solid {accent_bright};
    }}

    /* ── Scroll area / scrollbars ────────────────────────────── */
    QScrollArea {{
        border: none;
    }}
    QScrollBar:vertical {{
        background: {bg};
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {pri};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {accent};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        background: transparent;
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: {bg};
        height: 8px;
    }}
    QScrollBar::handle:horizontal {{
        background: {pri};
        border-radius: 4px;
        min-width: 24px;
    }}

    /* ── Status bar ──────────────────────────────────────────── */
    QStatusBar {{
        background: {bg};
        color: {muted};
        border-top: 1px solid {border};
    }}

    /* ── Tabs (Settings dialog) ──────────────────────────────── */
    QTabWidget::pane {{
        border: 1px solid {border};
        background: {surf};
    }}
    QTabBar::tab {{
        background: {bg};
        color: {muted};
        border: 1px solid {border};
        border-bottom: none;
        padding: 6px 14px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        color: {accent_bright};
        border: 1px solid {accent};
        border-bottom: none;
        background: {surf};
    }}
    QTabBar::tab:hover {{
        color: {accent_bright};
    }}

    /* ── Tooltip ─────────────────────────────────────────────── */
    QToolTip {{
        background: {surf};
        color: {accent_bright};
        border: 1px solid {accent};
        padding: 4px 8px;
    }}
    """


def apply_stylesheet(app: QApplication) -> dict[str, str]:
    """Apply the stylesheet to *app*. Returns the colors dict for callers."""
    colors = load_colors()
    app.setStyleSheet(build_qss(colors))
    return colors
