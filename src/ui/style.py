"""
Loads theme/colors.json and produces the QSS stylesheet for the app.

Mobiglass v2: a soft dark backdrop with translucent charcoal-blue
panels (22 px rounded), pill-shaped "field" containers (14 px rounded)
inside panels for high-contrast readable content, and bright cyan
corner-taper accents (drawn by `MobiglassCornerOverlay`, not QSS,
since stylesheets can't do radial alpha masks).

Style anchors:
    QWidget#mainPanel        — the three big columns (Contracts / Bay / Route)
    QFrame#card              — content blocks inside panels (pill fields)
    QLabel[heading=true]     — section headers with cyan glow
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
    field  = colors.get("field", surf)
    field_strong = colors.get("field_strong", field)
    header_strip = colors.get("header_strip", surf)
    pri    = colors["primary"]
    pri_on = colors["primary_on"]
    accent = colors["accent"]
    accent_bright = colors.get("accent_bright", accent)
    acc_on = colors["accent_on"]
    text   = colors["text"]
    muted  = colors["text_muted"]
    border = colors.get("border", "#264a5c")
    amber  = colors.get("secondary_accent", "#ff8a3c")
    danger = colors.get("danger", "#ff3030")

    return f"""
    /* ── Base ─────────────────────────────────────────────────── */
    /* Soft dark linear gradient backdrop; panels paint with rgba()
       over this so the gradient reads through and gives the
       "translucent over a holographic surface" Mobiglass feel. */
    QMainWindow, QDialog {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:1,
            stop:0 #060a12, stop:1 #02050a);
        color: {text};
        font-family: 'Segoe UI', sans-serif;
        font-size: 13px;
    }}
    QWidget {{
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
        letter-spacing: 1.5px;
    }}

    /* ── Main panels (Contracts / Bay / Route columns) ───────── */
    /* Translucent charcoal-blue cards with 22 px rounded corners.
       Alpha 158/255 ≈ 0.62 — matches the HTML mockup's
       rgba(24, 27, 35, 0.62), so the soft dark backdrop reads
       through between panels. The corner-taper cyan accents are
       drawn by MobiglassCornerOverlay on top — they are NOT part
       of this stylesheet. */
    QWidget#mainPanel {{
        background-color: rgba(24, 27, 35, 158);
        border-radius: 22px;
    }}

    /* ── Inner cards / fields (pill containers) ──────────────── */
    /* The reference mockup calls these "fields" — contract rows,
       stop cards, SCU summary, etc. They sit inside panels and
       hold the actual readable content. */
    QFrame#card {{
        background-color: {field};
        border: 1px solid rgba(91, 228, 255, 0.18);
        border-radius: 14px;
    }}
    QFrame#card[conflict="true"] {{
        background-color: rgba(60, 32, 14, 0.92);
        border: 1px solid {amber};
    }}

    /* ── Buttons ─────────────────────────────────────────────── */
    QPushButton {{
        background-color: {field};
        color: {accent_bright};
        border: 1px solid rgba(91, 228, 255, 0.4);
        border-radius: 999px;
        padding: 6px 14px;
        font-weight: 500;
        letter-spacing: 0.4px;
    }}
    QPushButton:hover {{
        background-color: {field_strong};
        border-color: {accent_bright};
        color: {accent_bright};
    }}
    QPushButton:pressed {{
        background-color: {accent};
        color: {acc_on};
    }}
    QPushButton:disabled {{
        background-color: {field};
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
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
        background-color: {field};
        color: {text};
        border: 1px solid rgba(91, 228, 255, 0.18);
        border-radius: 12px;
        padding: 4px 10px;
        selection-background-color: {pri};
        selection-color: {pri_on};
    }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
        border-color: {accent};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {surf};
        color: {text};
        border: 1px solid {accent};
        selection-background-color: {pri};
        selection-color: {pri_on};
        border-radius: 8px;
    }}

    QCheckBox {{
        color: {text};
        spacing: 6px;
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {border};
        background: {field_strong};
        border-radius: 3px;
    }}
    QCheckBox::indicator:checked {{
        background: {accent};
        border: 1px solid {accent_bright};
    }}

    /* ── Scroll area / scrollbars ────────────────────────────── */
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollArea > QWidget > QWidget {{
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
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
        background: transparent;
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
        border-radius: 14px;
    }}
    QTabBar::tab {{
        background: {field};
        color: {muted};
        border: 1px solid {border};
        border-bottom: none;
        padding: 6px 16px;
        margin-right: 2px;
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
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
        border-radius: 6px;
    }}
    """


def apply_stylesheet(app: QApplication) -> dict[str, str]:
    """Apply the stylesheet to *app*. Returns the colors dict for callers."""
    colors = load_colors()
    app.setStyleSheet(build_qss(colors))
    return colors
