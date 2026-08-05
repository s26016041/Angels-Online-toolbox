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
        # 固定視窗大小：各分頁的內容（收益監控的角色卡、封包分頁的兩張表）都放在
        # 可捲動區裡，視窗本身不需要、也不該跟著內容一起長高。固定尺寸讓多開時
        # 每次打開的位置與大小都一致。寬度要放得下封包分頁的欄位。
        self.setFixedSize(940, 700)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())

        # ★ 先把慢的東西讀完（角色名、位址校正），配進度視窗。
        #   不做的話第一次切到掛機／晶化分頁會凍住三、四秒，而且使用者不知道
        #   在等什麼（他回報「切過去會卡一下」）。沒開遊戲就直接跳過。
        from app.preload_ui import run_with_dialog

        run_with_dialog(self)

        self._loaded_tabs: list[BaseTab] = []
        self._load_tabs()

        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 自動更新：背景查有沒有新版，有才跳提示。開發模式與關掉自動檢查時完全不動作。
        from app.update_ui import UpdateManager

        self._updater = UpdateManager(self)
        self._updater.start()

        # ★ 背景自我監察：遊戲改版讓讀取邏輯失效時，跳通知並關掉程式。
        #   壞掉時讀到的是垃圾數值而不是「沒有數值」，繼續開著會誤導使用者。
        #   判定很保守（要所有分身都失敗），沒開遊戲時完全不檢查。
        from app import health_ui

        self._health = health_ui.start(self)

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
        try:
            self._updater.stop()
        except Exception:
            traceback.print_exc()
        # ⚠⚠ 自我監察那條執行緒**一定要等它結束**。它在掃五台分身的記憶體，
        #   物件被解構時人還在跑的話，Qt 丟「Destroyed while thread is still
        #   running」→ Windows 0xC0000409 原生當機，crash.log 什麼都不會留
        #   （health_ui.start() 的說明就是在講這個坑）。
        try:
            if self._health is not None:
                self._health.stop()
        except Exception:
            traceback.print_exc()
        for tab in self._loaded_tabs:
            try:
                tab.on_close()
            except Exception:
                traceback.print_exc()
        super().closeEvent(event)
