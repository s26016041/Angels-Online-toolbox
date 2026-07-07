"""天使之戀工具箱 — 程式進入點。

執行方式：
    py main.py
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app import __app_name__
from app.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
