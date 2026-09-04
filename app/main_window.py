"""主視窗。

負責：
1. 建立「左邊分類 → 右邊該分類的分頁」兩層容器。
   ★ 2026-09-05 使用者：「功能頁面太多，不好選要使用的頁面」—— 17 頁塞在一排
     分頁列上會變成左右捲動、名字被切掉。改成左側直排 4 個分類（見
     base_tab.GROUPS），右邊只放該分類的分頁；視窗從 940 放寬到 1040，多出來的
     100 px 剛好給分類欄 —— **每一頁拿到的內容區跟以前一模一樣**，不必跑版。
2. 自動掃描 app/tabs/ 底下所有模組，找出 BaseTab 的子類別並掛上分頁。
   → 新增功能時只要在 tabs/ 丟一個新檔案（設好 GROUP），不必修改這裡。
3. 記住上次停在哪個分類、哪一頁，下次開啟直接回到那裡（多開時每台常常都開同一頁）。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from app import __app_name__, __version__, tabs as tabs_pkg
from app.config import config
from app.tabs.base_tab import GROUPS, BaseTab

# 左側分類欄的寬度。視窗也放寬同樣的量（940 → 1040），內容區維持 940 不變。
SIDEBAR_W = 100
KEY_GROUP = "ui.last_group"     # 上次停在哪個分類（名稱）
KEY_PAGE = "ui.last_page"       # 上次停在哪一頁（TAB_TITLE）


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        # 固定視窗大小：各分頁的內容（收益監控的角色卡、封包分頁的兩張表）都放在
        # 可捲動區裡，視窗本身不需要、也不該跟著內容一起長高。固定尺寸讓多開時
        # 每次打開的位置與大小都一致。
        # ★ 1040 = 原本的 940 + 左側分類欄 SIDEBAR_W（使用者 2026-09-05 同意放大）。
        self.setFixedSize(940 + SIDEBAR_W, 700)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # 左：分類（直排）。右：每個分類一個 QTabWidget，疊在 QStackedWidget 裡。
        self.groups = QListWidget()
        self.groups.setFixedWidth(SIDEBAR_W)
        self.groups.setFocusPolicy(Qt.NoFocus)
        self.groups.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ★ 顏色**全部用系統調色盤**，不自己配色（使用者 2026-09-05：「左邊顏色
        #   看不清，而且要顏色統一」）：背景＝視窗底色、選中＝跟其他清單一樣的
        #   反白色（藍底白字），沒選中＝一般文字色。只調字級與行距。
        #   ⚠ 選中要指名 palette(highlight)：清單設了 NoFocus，不指名的話 Qt 會用
        #     「沒焦點」那組淡灰反白，就是原本看不清的原因之一。
        self.groups.setStyleSheet(
            "QListWidget { border: none; border-right: 1px solid palette(mid);"
            "  background: palette(window); font-size: 13px; }"
            # ⚠ 字級 13 是「長時間自動化」六個字在 100px 寬剛好放得下的上限；
            #   14 會被省略成「長時間自動…」。
            "QListWidget::item { padding: 14px 4px; color: palette(window-text); }"
            "QListWidget::item:selected { background: palette(highlight);"
            "  color: palette(highlighted-text); }")
        self.stack = QStackedWidget()
        lay.addWidget(self.groups)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        # 分類名稱 → 那一組的 QTabWidget（只放有分頁的分類）
        self._tabs_by_group: dict[str, QTabWidget] = {}

        self.setStatusBar(QStatusBar())

        # ★ 先把慢的東西讀完（角色名、位址校正），配進度視窗。
        #   不做的話第一次切到掛機／晶化分頁會凍住三、四秒，而且使用者不知道
        #   在等什麼（他回報「切過去會卡一下」）。沒開遊戲就直接跳過。
        from app.preload_ui import run_with_dialog

        run_with_dialog(self)

        self._loaded_tabs: list[BaseTab] = []
        self._load_tabs()
        self._restore_last()
        # ⚠ 訊號**載完才接**：addTab 會對每一組的第一頁發 currentChanged，
        #   開機時逐頁 on_show() 等於把所有分頁都跑一遍（跟以前一樣只顯示的那頁才跑）。
        self.groups.currentRowChanged.connect(self._on_group_changed)
        for tw in self._tabs_by_group.values():
            tw.currentChanged.connect(
                lambda i, tw=tw: self._on_tab_changed(tw, i))

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

    def _group_tabs(self, group: str) -> QTabWidget:
        """那個分類的 QTabWidget；第一次用到才建（沒分頁的分類不出現在左邊）。"""
        tw = self._tabs_by_group.get(group)
        if tw is None:
            tw = QTabWidget()
            tw.setMovable(True)
            self._tabs_by_group[group] = tw
            self.stack.addWidget(tw)
            self.groups.addItem(QListWidgetItem(group))
        return tw

    def _load_tabs(self) -> None:
        tab_classes = self._discover_tab_classes()
        # ★ 分類照 GROUPS 的順序出現，不是照「哪個分頁先被載到」。
        #   GROUP 沒登記在 GROUPS 裡的分頁排到最後一個分類 —— 寧可放錯格也不能不見。
        order = {g: i for i, g in enumerate(GROUPS)}
        tab_classes.sort(key=lambda c: (order.get(c.GROUP, len(GROUPS)),
                                        c.ORDER, c.TAB_TITLE))
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
            group = cls.GROUP if cls.GROUP in order else GROUPS[-1]
            self._group_tabs(group).addTab(tab, cls.TAB_TITLE)

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
    # 給測試／其他模組用的查詢
    # ------------------------------------------------------------------
    def pages(self) -> list[tuple[str, str, BaseTab]]:
        """所有分頁，照畫面上的順序：(分類, 標題, 分頁物件)。"""
        out = []
        for i in range(self.groups.count()):
            group = self.groups.item(i).text()
            tw = self._tabs_by_group[group]
            for j in range(tw.count()):
                out.append((group, tw.tabText(j), tw.widget(j)))
        return out

    def show_page(self, page: QWidget) -> bool:
        """切到那一頁（左邊分類與右邊分頁一起切）。找不到回 False。"""
        for i in range(self.groups.count()):
            tw = self._tabs_by_group[self.groups.item(i).text()]
            j = tw.indexOf(page)
            if j >= 0:
                self.groups.setCurrentRow(i)
                self.stack.setCurrentIndex(i)
                tw.setCurrentIndex(j)
                return True
        return False

    def current_page(self) -> BaseTab | None:
        tw = self.stack.currentWidget()
        if isinstance(tw, QTabWidget):
            w = tw.currentWidget()
            if isinstance(w, BaseTab):
                return w
        return None

    # ------------------------------------------------------------------
    # 記住上次停在哪
    # ------------------------------------------------------------------
    def _restore_last(self) -> None:
        group = config.get(KEY_GROUP, "")
        title = config.get(KEY_PAGE, "")
        for g, t, page in self.pages():
            if g == group and t == title:
                self.show_page(page)
                page.on_show()
                return
        if self.groups.count():
            self.groups.setCurrentRow(0)
            self.stack.setCurrentIndex(0)
            page = self.current_page()
            if page is not None:
                page.on_show()

    def _remember(self) -> None:
        page = self.current_page()
        row = self.groups.currentRow()
        if page is None or row < 0:
            return
        tw = self.stack.currentWidget()
        config.set(KEY_GROUP, self.groups.item(row).text())
        config.set(KEY_PAGE, tw.tabText(tw.indexOf(page)))
        config.save()          # ⚠ config.set 不寫檔，要接 save()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _on_group_changed(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        page = self.current_page()
        if page is not None:
            page.on_show()
        self._remember()

    def _on_tab_changed(self, tw: QTabWidget, index: int) -> None:
        if tw is not self.stack.currentWidget():
            return                     # 不是畫面上那一組（開機時逐組建立會觸發）
        if 0 <= index < tw.count():
            widget = tw.widget(index)
            if isinstance(widget, BaseTab):
                widget.on_show()
        self._remember()

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
