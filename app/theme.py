"""全域深色主題（深邃夜藍）。

在 QApplication 套一份「QSS 樣式表 + 字體 + 調色盤」，讓主視窗與所有分頁 / 元件
外觀一致。要調整整體外觀只改這裡，各分頁不需要動。

用法（見 main.py）：
    from app.theme import apply_theme
    apply_theme(app)
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# --- 調色盤（深邃夜藍）------------------------------------------------------
# 這些常數與下方 QSS 內的色碼一致；改色時兩邊一起改。
BG         = "#1e2230"   # 視窗底
BASE       = "#1b1f2b"   # 輸入框底
PANEL      = "#262b3b"   # 群組框 / 面板
PANEL_HDR  = "#2b3145"   # 表頭
PANEL_HOV  = "#3a4160"   # 按鈕 hover
TAB_PANE   = "#232838"   # 分頁內容區
TABLE_BG   = "#222736"
TABLE_ALT  = "#262b3b"
BORDER     = "#3a4056"
BORDER_HL  = "#4d557a"
GRID       = "#313650"
ACCENT     = "#6c8cff"   # 主色（藍紫）
ACCENT_HOV = "#7f9bff"
ACCENT_PRS = "#5a78e6"
SEL_BG     = "#33406a"
TEXT       = "#e6e8ef"
TEXT_MUT   = "#9aa2b8"   # 次要 / 狀態文字
TEXT_DIS   = "#6b7288"   # 停用
SUCCESS    = "#33c17f"
DANGER     = "#ff6b6b"

# 供各分頁沿用主題色的語意常數（取代原本寫死的 "gray" / "green"）。
MUTED = TEXT_MUT
OK = SUCCESS
BAD = DANGER

_QSS = """
/* ---- 基底 ---- */
QWidget {
    color: #e6e8ef;
    font-size: 10pt;
}
QMainWindow, QDialog, QMessageBox, QInputDialog, QFileDialog {
    background-color: #1e2230;
}
QLabel { background: transparent; }

/* ---- 分頁 ---- */
QTabWidget::pane {
    border: 1px solid #3a4056;
    border-radius: 8px;
    background-color: #232838;
    top: -1px;
}
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: #9aa2b8;
    padding: 8px 18px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: #e6e8ef; }
QTabBar::tab:selected {
    color: #6c8cff;
    border-bottom: 2px solid #6c8cff;
}

/* ---- 群組框 ---- */
QGroupBox {
    background-color: #262b3b;
    border: 1px solid #3a4056;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 2px 6px;
    color: #b9c0d4;
}

/* ---- 按鈕 ---- */
QPushButton {
    background-color: #2f3548;
    color: #e6e8ef;
    border: 1px solid #3a4056;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
}
QPushButton:hover { background-color: #3a4160; border-color: #4d557a; }
QPushButton:pressed { background-color: #262b3b; }
QPushButton:disabled { color: #6b7288; background-color: #262b3b; border-color: #313650; }
/* 主要動作按鈕：加上 primary 動態屬性即為主色 */
QPushButton[primary="true"] {
    background-color: #6c8cff;
    color: #ffffff;
    border: 1px solid #6c8cff;
    font-weight: 600;
}
QPushButton[primary="true"]:hover { background-color: #7f9bff; border-color: #7f9bff; }
QPushButton[primary="true"]:pressed { background-color: #5a78e6; }
QPushButton[primary="true"]:disabled { background-color: #384168; border-color: #384168; color: #aeb6d6; }

/* ---- 輸入類 ---- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {
    background-color: #1b1f2b;
    color: #e6e8ef;
    border: 1px solid #3a4056;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #6c8cff;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #6c8cff;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    color: #6b7288; background-color: #20242f;
}
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: right;
    width: 22px; border-left: 1px solid #3a4056;
}
QComboBox QAbstractItemView {
    background-color: #262b3b;
    border: 1px solid #3a4056;
    selection-background-color: #6c8cff;
    selection-color: #ffffff;
    outline: none;
}

/* ---- 勾選框 ---- */
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #4d557a; border-radius: 4px;
    background: #1b1f2b;
}
QCheckBox::indicator:hover { border-color: #6c8cff; }
QCheckBox::indicator:checked { background: #6c8cff; border-color: #6c8cff; }

/* ---- 表格 ---- */
QTableView, QTableWidget {
    background-color: #222736;
    alternate-background-color: #262b3b;
    gridline-color: #313650;
    border: 1px solid #3a4056;
    border-radius: 8px;
    selection-background-color: #33406a;
    selection-color: #ffffff;
    outline: none;
}
QTableView::item { padding: 4px 6px; }
QHeaderView::section {
    background-color: #2b3145;
    color: #b9c0d4;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #313650;
    border-bottom: 1px solid #3a4056;
    font-weight: 600;
}
QTableCornerButton::section { background-color: #2b3145; border: none; }

/* ---- 進度條 ---- */
QProgressBar {
    background-color: #1b1f2b;
    border: 1px solid #3a4056;
    border-radius: 6px;
    text-align: center;
    color: #e6e8ef;
    min-height: 16px;
}
QProgressBar::chunk { background-color: #6c8cff; border-radius: 5px; }

/* ---- 捲軸 ---- */
QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4056; min-height: 30px; border-radius: 6px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #4d557a; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a4056; min-width: 30px; border-radius: 6px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #4d557a; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; background: none; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---- 其他 ---- */
QScrollArea { border: none; background: transparent; }
QStatusBar { background-color: #1a1e29; color: #9aa2b8; border-top: 1px solid #313650; }
QStatusBar::item { border: none; }
QToolTip {
    background-color: #2f3548; color: #e6e8ef;
    border: 1px solid #4d557a; padding: 4px 6px; border-radius: 4px;
}
"""


def _palette() -> QPalette:
    """給 Fusion 底層與原生繪製的元件用的深色調色盤（QSS 沒覆蓋到的部分靠它）。"""
    p = QPalette()
    p.setColor(QPalette.Window, QColor(BG))
    p.setColor(QPalette.WindowText, QColor(TEXT))
    p.setColor(QPalette.Base, QColor(BASE))
    p.setColor(QPalette.AlternateBase, QColor(TABLE_ALT))
    p.setColor(QPalette.Text, QColor(TEXT))
    p.setColor(QPalette.Button, QColor(PANEL))
    p.setColor(QPalette.ButtonText, QColor(TEXT))
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.Highlight, QColor(ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, QColor(PANEL))
    p.setColor(QPalette.ToolTipText, QColor(TEXT))
    p.setColor(QPalette.PlaceholderText, QColor(TEXT_MUT))
    p.setColor(QPalette.Link, QColor(ACCENT))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_DIS))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_DIS))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(TEXT_DIS))
    return p


def apply_theme(app: QApplication) -> None:
    """在 QApplication 套用深邃夜藍主題（字體 + 調色盤 + QSS）。"""
    app.setStyle("Fusion")  # 統一底層繪製，QSS 才會在各元件一致生效
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    app.setPalette(_palette())
    app.setStyleSheet(_QSS)
