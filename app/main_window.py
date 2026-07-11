"""主視窗。

負責：
1. 建立 QTabWidget 分頁容器。
2. 自動掃描 app/tabs/ 底下所有模組，找出 BaseTab 的子類別並掛上分頁。
   → 新增功能時只要在 tabs/ 丟一個新檔案，不必修改這裡。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import traceback

from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)

from app import __app_name__, __version__, tabs as tabs_pkg
from app.tabs.base_tab import BaseTab


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(880, 620)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())

        self._loaded_tabs: list[BaseTab] = []
        self._load_tabs()

        self.tabs.currentChanged.connect(self._on_tab_changed)

    # ------------------------------------------------------------------
    # 分頁自動載入
    # ------------------------------------------------------------------
    def _discover_tab_classes(self) -> list[type[BaseTab]]:
        """掃描 app.tabs 套件，回傳所有 BaseTab 子類別。"""
        found: list[type[BaseTab]] = []
        for module_info in pkgutil.iter_modules(tabs_pkg.__path__):
            name = module_info.name
            if name.startswith("_") or name == "base_tab":
                continue
            try:
                module = importlib.import_module(f"{tabs_pkg.__name__}.{name}")
            except Exception:  # 單一分頁載入失敗不應拖垮整個程式
                traceback.print_exc()
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseTab)
                    and obj is not BaseTab
                    and obj.__module__ == module.__name__
                ):
                    found.append(obj)
        # 依 ORDER 排序，再依標題穩定排序
        found.sort(key=lambda c: (c.ORDER, c.TAB_TITLE))
        return found

    def _load_tabs(self) -> None:
        tab_classes = self._discover_tab_classes()
        for cls in tab_classes:
            if not getattr(cls, "ENABLED", True):
                continue
            try:
                tab = cls()
            except Exception:
                traceback.print_exc()
                QMessageBox.warning(
                    self,
                    "分頁載入失敗",
                    f"分頁「{cls.TAB_TITLE}」載入時發生錯誤，已略過。\n"
                    f"詳見主控台輸出。",
                )
                continue
            self._loaded_tabs.append(tab)
            self.tabs.addTab(tab, cls.TAB_TITLE)

        if not self._loaded_tabs:
            self.statusBar().showMessage("尚未載入任何分頁")
            # 空視窗（一片白）通常代表打包時漏收了 app.tabs.* 子模組，
            # 或分頁在 import 階段就全部失敗。明確跳出訊息，不要靜默白屏。
            QMessageBox.critical(
                self,
                "沒有可用的分頁",
                "找不到任何分頁，視窗會是空的。\n\n"
                "若這是打包後的 .exe，通常是漏收了 app 底下的分頁模組；\n"
                "請確認 spec 有 collect_submodules('app')，或改用 build_local.py 重新編譯。",
            )
        else:
            self.statusBar().showMessage(
                f"已載入 {len(self._loaded_tabs)} 個分頁"
            )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_tab_changed(self, index: int) -> None:
        if 0 <= index < self.tabs.count():
            widget = self.tabs.widget(index)
            if isinstance(widget, BaseTab):
                widget.on_show()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名慣例)
        for tab in self._loaded_tabs:
            try:
                tab.on_close()
            except Exception:
                traceback.print_exc()
        super().closeEvent(event)
