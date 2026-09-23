"""「背包 ↔ 清單」兩張表的小視窗（存公會倉庫、自動丟棄共用；掛機設定裡那兩顆鈕開的）。

兩張表（使用者 2026-09-06 定，2026-09-23 抽成共用）：左邊＝**這台背包**裡符合條件的東西、
右邊＝**清單**。選了按「加入 →」／「← 移除」（或雙擊）在兩邊搬；搜尋框同時過濾兩邊
（子字串，打一個字有那個字的都出現）。清單存 config，**全部分身共用** —— 在哪一台勾都
一樣，每台照這張處理自己背包裡有的。

規則：
  · 不符合條件的（由 Spec.candidates 決定）**不列**，只在底下報個數。
  · 右邊只顯示**名字＋圖示**（使用者 2026-09-06：「大家共用存倉庫表，不需要說不在這台背包，
    以後他拿到這東西一樣會存」）；圖示查資料表（itemflags.icon_of，item.xml 原型介面），
    所以別台加的、這台沒有的東西也畫得出來。
  · 搬一下就存檔（config.set 接 save），不用按確定。
  · 列高＝圖示 32 + 上下各 6（使用者：「每個物品上下要距離大點，現在圖片會被擋到」）。
  · 「🧪 現在就…」＝就地測試，由掛機頁提供 `test_run(say)`；背景執行緒的進度用 QTimer
    撈回來顯示。

各功能只差一個 `Spec`（標題、文案、config 讀寫、候選）；見 guildbank_dialog.py／discard_dialog.py。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout)

from app import theme
from app.game import itemflags, itemicon, itemname

ROLE_TID = Qt.UserRole
ROLE_TEXT = Qt.UserRole + 1          # 過濾用的小寫字串（名字＋編號）
ICON = 32
ROW_H = ICON + 12                    # 圖示上下各留 6px，不然 32px 的圖會被列高裁掉


@dataclass(frozen=True)
class Spec:
    """一個功能長什麼樣：文案＋清單怎麼讀寫＋背包裡哪些算候選。"""

    title: str                       # 視窗標題（後面會接「— 角色名」）
    left_label: str                  # 左表標題
    right_label: str                 # 右表標題
    verb: str                        # 動詞：「存」「丟」——用在摘要／提示
    wanted: Callable[[], set[int]]   # 讀清單（config）
    set_wanted: Callable[[set[int]], None]   # 寫清單（含 save）
    # 候選：scanner → (符合的, 不符合的, 整袋讀完整了嗎)
    candidates: Callable[[object], tuple[list, list, bool]]
    why_not_hint: str = ""           # 不符合那一句的括號說明（空＝不顯示不符合數）
    test_text: str = ""              # 🧪 鈕文字（空＝不放測試鈕）
    test_tip: str = ""


def aggregate(items):
    """把同種類的格子併成一列：{type_id: (數量, icon_id)}，照第一次出現的格號排。"""
    out: dict[int, list[int]] = {}
    for it in items:
        if it.type_id in out:
            out[it.type_id][0] += it.count
        else:
            out[it.type_id] = [it.count, it.icon_id]
    return {tid: (c, ic) for tid, (c, ic) in out.items()}


def matches(query: str, haystack: str) -> bool:
    """子字串過濾：空字串全部符合；不分大小寫。"""
    q = query.strip().lower()
    return (not q) or (q in haystack)


class ItemListDialog(QDialog):
    def __init__(self, parent, scanner, who: str, spec: Spec, test_run=None) -> None:
        super().__init__(parent)
        self._sc = scanner
        self._spec = spec
        self._test_run = test_run
        self._progress = ""
        self._agg: dict[int, tuple[int, int]] = {}
        self.setWindowTitle(f"{spec.title} — {who}")
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋：打一個字，有那個字的都會出現（兩邊一起過濾）")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.reload_btn = QPushButton("重新讀背包")
        self.reload_btn.clicked.connect(self.reload)
        top.addWidget(self.reload_btn)
        v.addLayout(top)

        mid = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel(spec.left_label))
        self.bag_list = self._make_list()
        self.bag_list.itemDoubleClicked.connect(lambda _i: self._add())
        left.addWidget(self.bag_list, 1)
        mid.addLayout(left, 1)

        arrows = QVBoxLayout()
        arrows.addStretch(1)
        self.add_btn = QPushButton("加入 →")
        self.add_btn.setToolTip(f"把左邊選的加進要{spec.verb}的清單（可多選、可雙擊）")
        self.add_btn.clicked.connect(self._add)
        arrows.addWidget(self.add_btn)
        self.remove_btn = QPushButton("← 移除")
        self.remove_btn.setToolTip("把右邊選的從清單拿掉（可多選、可雙擊）")
        self.remove_btn.clicked.connect(self._remove)
        arrows.addWidget(self.remove_btn)
        arrows.addStretch(1)
        mid.addLayout(arrows)

        right = QVBoxLayout()
        right.addWidget(QLabel(spec.right_label))
        self.want_list = self._make_list()
        self.want_list.itemDoubleClicked.connect(lambda _i: self._remove())
        right.addWidget(self.want_list, 1)
        mid.addLayout(right, 1)
        v.addLayout(mid, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {theme.TEXT_MUT};")
        v.addWidget(self.summary)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_MUT};")
        v.addWidget(self.status)

        b = QHBoxLayout()
        if test_run is not None and spec.test_text:
            self.test_btn = QPushButton(spec.test_text)
            self.test_btn.setToolTip(spec.test_tip)
            self.test_btn.clicked.connect(self._start_test)
            b.addWidget(self.test_btn)
        b.addStretch(1)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        b.addWidget(close_btn)
        v.addLayout(b)

        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._poll_progress)
        self.resize(860, 600)
        self.reload()

    @staticmethod
    def _make_list() -> QListWidget:
        lst = QListWidget()
        lst.setIconSize(QSize(ICON, ICON))
        lst.setUniformItemSizes(True)
        lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
        return lst

    # ------------------------------------------------------------------
    def reload(self) -> None:
        """重讀背包、重畫兩張表（清單照 config）。"""
        ok, no, complete = ([], [], False)
        if self._sc is not None:
            try:
                ok, no, complete = self._spec.candidates(self._sc)
            except Exception:                                  # noqa: BLE001
                ok, no, complete = [], [], False
        self._agg = aggregate(ok)
        self._rebuild(self._spec.wanted())
        parts = [f"這台背包可{self._spec.verb} {len(self._agg)} 種"]
        if no and self._spec.why_not_hint:
            parts.append(f"不能{self._spec.verb} {len(no)} 格（{self._spec.why_not_hint}，不列）")
        if not complete:
            parts.append("⚠ 背包沒讀完整（換圖中？）—— 按「重新讀背包」再試")
        self._summary_tail = "　·　".join(parts)
        self._update_summary()

    def _rebuild(self, wanted: set[int]) -> None:
        """照 wanted 重畫兩張表：左邊＝背包裡有、還沒在清單上的；右邊＝清單（含不在背包的）。"""
        self.bag_list.clear()
        self.want_list.clear()
        for tid, (count, icon_id) in self._agg.items():
            if tid not in wanted:
                self._add_row(self.bag_list, tid, f"{itemname.label(tid)} ×{count}", icon_id)
        # 右邊＝清單本身：名字＋圖示（圖示查表，這台沒有的也畫得出來），照名字排
        for tid in sorted(wanted, key=lambda t: (itemname.label(t), t)):
            icon_id = itemflags.icon_of(tid) or self._agg.get(tid, (0, 0))[1]
            self._add_row(self.want_list, tid, itemname.label(tid), icon_id)
        self._apply_filter(self.search.text())

    def _add_row(self, lst: QListWidget, tid: int, text: str, icon_id: int) -> None:
        name = itemname.label(tid)
        row = QListWidgetItem(text)
        row.setData(ROLE_TID, int(tid))
        row.setData(ROLE_TEXT, f"{name} {tid}".lower())
        row.setSizeHint(QSize(0, ROW_H))
        if icon_id:
            pm = itemicon.pixmap(icon_id)
            if pm is not None and not pm.isNull():
                row.setIcon(QIcon(pm))
        lst.addItem(row)

    # ------------------------------------------------------------------
    def _apply_filter(self, text: str) -> None:
        for lst in (self.bag_list, self.want_list):
            for i in range(lst.count()):
                row = lst.item(i)
                row.setHidden(not matches(text, row.data(ROLE_TEXT) or ""))

    def wanted_ids(self) -> set[int]:
        """右邊那張表現在有的種類 ID。"""
        return {int(self.want_list.item(i).data(ROLE_TID))
                for i in range(self.want_list.count())}

    def _selected(self, lst: QListWidget) -> set[int]:
        return {int(r.data(ROLE_TID)) for r in lst.selectedItems()}

    def _add(self) -> None:
        picked = self._selected(self.bag_list)
        if picked:
            self._commit(self.wanted_ids() | picked)

    def _remove(self) -> None:
        picked = self._selected(self.want_list)
        if picked:
            self._commit(self.wanted_ids() - picked)

    def _commit(self, ids: set[int]) -> None:
        self._spec.set_wanted(ids)              # 搬一下就存檔（含 save）
        self._rebuild(ids)
        self._update_summary()

    def _update_summary(self) -> None:
        n = len(self.wanted_ids())
        self.summary.setText(f"清單 {n} 種（全部分身共用）　·　"
                             + getattr(self, "_summary_tail", ""))

    # ------------------------------------------------------------------
    def _say(self, msg: str) -> None:
        """背景執行緒的進度（只存字串，Qt 由 _poll_progress 在主執行緒更新）。"""
        self._progress = str(msg)

    def _poll_progress(self) -> None:
        if self._progress:
            self.status.setText(self._progress)

    def _start_test(self) -> None:
        if self._test_run is None:
            return
        self._progress = ""
        self.status.setText("開始…")
        self._timer.start()
        started = False
        try:
            started = bool(self._test_run(self._say))
        finally:
            if not started:
                self._timer.stop()
                self._poll_progress()
