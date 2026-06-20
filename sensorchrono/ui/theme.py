"""KSU brand theme — gold / black / white palette applied via QSS."""
from __future__ import annotations

GOLD  = "#F0AB00"
BLACK = "#000000"
WHITE = "#FFFFFF"
DARK  = "#1A1A1A"
MUTED = "#666666"

APP_STYLESHEET = f"""
/* ── global ─────────────────────────────────────────────────────────── */
QWidget {{
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    color: {WHITE};
    background-color: {BLACK};
}}
QMainWindow {{ background-color: {BLACK}; }}

/* ── buttons ─────────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {GOLD};
    color: {BLACK};
    border: none;
    padding: 7px 22px;
    border-radius: 4px;
    font-weight: 600;
    min-height: 30px;
}}
QPushButton:hover    {{ background-color: #D49800; }}
QPushButton:pressed  {{ background-color: #B88200; }}
QPushButton:disabled {{ background-color: #333333; color: #666666; }}

QPushButton[role="secondary"] {{
    background-color: #1E1E1E;
    color: {WHITE};
    border: 1px solid #444444;
}}
QPushButton[role="secondary"]:hover {{ background-color: #2A2A2A; }}

QPushButton[role="danger"] {{
    background-color: #C0392B;
    color: {WHITE};
}}
QPushButton[role="danger"]:hover {{ background-color: #A93226; }}

/* ── inputs ──────────────────────────────────────────────────────────── */
QLineEdit, QSpinBox, QComboBox {{
    border: 1px solid #444444;
    border-radius: 4px;
    padding: 5px 8px;
    background: #1A1A1A;
    color: {WHITE};
    min-height: 28px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {GOLD};
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: #1A1A1A;
    color: {WHITE};
    selection-background-color: {GOLD};
    selection-color: {BLACK};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: #2A2A2A;
    border: 1px solid #444;
}}

/* ── group box ───────────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid #333333;
    border-radius: 6px;
    margin-top: 14px;
    padding: 8px;
    font-weight: 600;
    background: #111111;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {GOLD};
    background: #111111;
}}

/* ── progress bar ────────────────────────────────────────────────────── */
QProgressBar {{
    border: 1px solid #333333;
    border-radius: 4px;
    background: #1A1A1A;
    text-align: center;
    color: {WHITE};
    height: 20px;
    font-weight: 600;
}}
QProgressBar::chunk {{
    background-color: {GOLD};
    border-radius: 3px;
}}

/* ── table ───────────────────────────────────────────────────────────── */
QTableWidget {{
    border: 1px solid #333333;
    border-radius: 4px;
    gridline-color: #2A2A2A;
    background: #111111;
    color: {WHITE};
    alternate-background-color: #1A1A1A;
}}
QTableWidget::item:selected {{
    background-color: {GOLD};
    color: {BLACK};
}}
QHeaderView::section {{
    background-color: #000000;
    color: {GOLD};
    border: none;
    border-bottom: 1px solid #333;
    padding: 6px 8px;
    font-weight: 600;
}}

/* ── list ────────────────────────────────────────────────────────────── */
QListWidget {{
    border: 1px solid #333333;
    border-radius: 4px;
    background: #111111;
    color: {WHITE};
}}
QListWidget::item {{
    padding: 5px 4px;
    border-bottom: 1px solid #222222;
}}
QListWidget::item:selected {{
    background-color: {GOLD};
    color: {BLACK};
}}

/* ── checkbox ────────────────────────────────────────────────────────── */
QCheckBox {{ spacing: 8px; color: {WHITE}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 2px solid #555555;
    border-radius: 3px;
    background: #1A1A1A;
}}
QCheckBox::indicator:checked {{
    background-color: {GOLD};
    border-color: {GOLD};
}}

/* ── labels ──────────────────────────────────────────────────────────── */
QLabel {{ color: {WHITE}; background: transparent; }}

/* ── form layout ─────────────────────────────────────────────────────── */
QFormLayout QLabel {{ color: #CCCCCC; }}

/* ── status bar ──────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: #0A0A0A;
    color: #888888;
    font-size: 11px;
}}

/* ── scrollbar ───────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    border: none; background: #1A1A1A; width: 8px; border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: #444444; border-radius: 4px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {GOLD}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""
