"""「存公會倉庫」小視窗（掛機頁那顆鈕開的）。

列**這台**背包裡能存公會倉庫的東西（圖示＋名字＋數量）、打字就過濾（子字串，
打一個字有那個字的都出現）、勾選＝清單。清單存 config（`guildbank.items`），
**全部分身共用** —— 在哪一台勾都一樣，補給時每台照這張存自己背包裡有的。

規則：
  · 不能存的（綁定／不可交易／不可存倉庫，表在 itemflags）**不列**，只在底下報個數。
  · 清單上有、但這台背包沒有的，另外列在最後（灰字「不在這台背包」）—— 不然在別台勾的
    東西在這台就看不到、也取消不了。
  · 勾一下就存檔（config.set 接 save），不用按確定。
  · 「🧪 現在就存」＝就地測試（走去這城的銀行開社團倉庫存清單上的東西），
    由掛機頁提供 `test_run(say)`；背景執行緒的進度用 QTimer 撈回來顯示。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout)

from app import theme
from app.game import guildbank, itemicon, itemname

ROLE_TID = Qt.UserRole
ROLE_TEXT = Qt.UserRole + 1          # 過濾用的小寫字串（名字＋編號）
ICON = 32


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


class GuildBankDialog(QDialog):
    def __init__(self, parent, scanner, who: str, test_run=None) -> None:
        super().__init__(parent)
        self._sc = scanner
        self._test_run = test_run
        self._progress = ""
        self.setWindowTitle(f"存公會倉庫 — {who}")
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋：打一個字，有那個字的都會出現")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.reload_btn = QPushButton("重新讀背包")
        self.reload_btn.clicked.connect(self.reload)
        top.addWidget(self.reload_btn)
        v.addLayout(top)

        self.list = QListWidget()
        self.list.setIconSize(QSize(ICON, ICON))
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.list, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {theme.TEXT_MUT};")
        v.addWidget(self.summary)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_MUT};")
        v.addWidget(self.status)

        b = QHBoxLayout()
        if test_run is not None:
            self.test_btn = QPushButton("🧪 現在就存（就地測試）")
            self.test_btn.setToolTip(
                "走去這座城的銀行 → 開「社團的倉庫」→ 把清單上、這台背包裡有的東西存進去。\n"
                "不回城、不修裝、不買東西；人要已經在有銀行的城裡。\n"
                "⚠ 會真的把東西存進公會倉庫。")
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
        self.resize(520, 560)
        self._populating = False
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        """重讀背包、重畫清單（勾選狀態照 config）。"""
        wanted = guildbank.wanted()
        ok, no, complete = ([], [], False)
        if self._sc is not None:
            try:
                ok, no, complete = guildbank.candidates(self._sc)
            except Exception:                                  # noqa: BLE001
                ok, no, complete = [], [], False
        agg = aggregate(ok)
        self._populating = True
        self.list.blockSignals(True)
        self.list.clear()
        for tid, (count, icon_id) in agg.items():
            self._add_row(tid, itemname.label(tid), count, icon_id, tid in wanted, in_bag=True)
        for tid in sorted(wanted - set(agg)):
            self._add_row(tid, itemname.label(tid), 0, 0, True, in_bag=False)
        self.list.blockSignals(False)
        self._populating = False
        n_no = sum(it.count for it in no)
        parts = [f"已勾 {len(wanted)} 種（全部分身共用）",
                 f"這台背包可存 {len(agg)} 種"]
        if n_no:
            parts.append(f"不能存 {len(no)} 格（綁定／不可交易／不可存倉庫，不列）")
        if not complete:
            parts.append("⚠ 背包沒讀完整（換圖中？）—— 按「重新讀背包」再試")
        self.summary.setText("　·　".join(parts))
        self._apply_filter(self.search.text())

    def _add_row(self, tid: int, name: str, count: int, icon_id: int,
                 checked: bool, in_bag: bool) -> None:
        text = f"{name} ×{count}" if in_bag else f"{name}（不在這台背包）"
        row = QListWidgetItem(text)
        row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
        row.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        row.setData(ROLE_TID, int(tid))
        row.setData(ROLE_TEXT, f"{name} {tid}".lower())
        if in_bag and icon_id:
            pm = itemicon.pixmap(icon_id)
            if pm is not None and not pm.isNull():
                row.setIcon(QIcon(pm))
        if not in_bag:
            row.setForeground(QColor(theme.TEXT_DIS))
        self.list.addItem(row)

    # ------------------------------------------------------------------
    def _apply_filter(self, text: str) -> None:
        for i in range(self.list.count()):
            row = self.list.item(i)
            row.setHidden(not matches(text, row.data(ROLE_TEXT) or ""))

    def checked_ids(self) -> set[int]:
        out: set[int] = set()
        for i in range(self.list.count()):
            row = self.list.item(i)
            if row.checkState() == Qt.Checked:
                out.add(int(row.data(ROLE_TID)))
        return out

    def _on_item_changed(self, _row) -> None:
        if self._populating:
            return
        ids = self.checked_ids()
        guildbank.set_wanted(ids)               # 勾一下就存檔（含 save）
        # 只更新第一段字，不重讀背包（重畫會把捲動位置弄掉）
        parts = self.summary.text().split("　·　")
        if parts:
            parts[0] = f"已勾 {len(ids)} 種（全部分身共用）"
            self.summary.setText("　·　".join(parts))

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
