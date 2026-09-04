"""遊戲總控分頁。

這裡放**所有天戀（分身）共用的設定**：打開一個開關，就套用到目前開著的每一台，
而且**之後重開或新開的天戀也會自動幫你設定上**（背景一直核對、補齊，不必手動）。

目前只有一個設定：

* **畫面凍結解除** —— 天戀失焦（點到別的視窗）時畫面本來會凍住、但角色照樣掛機；
  打開後失焦也會繼續更新畫面。改的是遊戲視窗程序 8 個位元組（v2：失焦時不去
  停畫面＋**點回視窗時把輸入重設回來** —— 舊版只做前者，切回來手動放技能會
  失靈），不注入程式碼、不搶前景、不影響邏輯與網路。關掉就還原。
  詳見 app/game/unfreeze.py 與 memory 的 background-unfreeze-investigation、
  manual-cast-broken-investigation。

## 為什麼是「一直掃」

patch 是寫進遊戲行程的記憶體，**不會跟著遊戲重開活過來**。所以背景每隔幾秒
就核對一次每一台：設定要開但還沒套 → 補上；要關但還開著 → 還原。掃得慢沒關係
（純維護，不趕時間），慢反而省 CPU。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app import theme
from app.config import config
from app.core import charname, preload
from app.core.memory import MemoryScanner
from app.game import locate, unfreeze
from app.tabs.base_tab import GROUP_LAUNCH, BaseTab

# 純維護用，掃慢一點（省 CPU）；使用者也說「不用太快」。
REFRESH_MS = 3000

COL_NAME, COL_ACCT, COL_STATE = range(3)
COLS = ("角色", "帳號", "狀態")


def _acct(title: str) -> str:
    """視窗標題 → 帳號；還在登入畫面（標題沒有 " - "）回空字串。"""
    return charname.account_from_title(title) if " - " in title else ""


class MasterTab(BaseTab):
    TAB_TITLE = "遊戲總控"
    GROUP = GROUP_LAUNCH
    ORDER = 13                       # 排在分身總控（12）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._pids: list[int] = []
        self._named: set[int] = set()
        self._loading = False
        self._unfreeze_on = bool(config.get("master.unfreeze", False))

        root = QVBoxLayout(self)

        hint = QLabel(
            "這裡的設定會套用到**所有天戀**。打開後，現在開著的每一台都會設定好，"
            "之後**重開或新開的天戀也會自動幫你設定上**（背景每幾秒核對一次）。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # --- 設定區（之後要加別的全域設定就往這裡塞）-----------------------
        box = QGroupBox("所有天戀共用的設定")
        lay = QVBoxLayout(box)
        self.cb_unfreeze = QCheckBox("畫面凍結解除")
        self.cb_unfreeze.setChecked(self._unfreeze_on)
        self.cb_unfreeze.setToolTip(
            "遊戲點到別的視窗時畫面不凍住，繼續更新。關掉就還原。\n"
            "⚠ 最小化仍然會停。")
        self.cb_unfreeze.toggled.connect(self._on_unfreeze_toggled)
        lay.addWidget(self.cb_unfreeze)
        desc = QLabel(
            "　失焦時畫面不凍住。適合多開時盯著別台、或邊掛機邊做別的事。")
        desc.setStyleSheet(f"color:{theme.MUTED};")
        desc.setWordWrap(True)
        lay.addWidget(desc)
        root.addWidget(box)

        # --- 各台狀態（唯讀，只是讓你看得到有沒有套上）---------------------
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        # ⚠ 不用 ResizeToContents（每拍改儲存格會把整欄重量一遍，見 qt-ui-pitfalls）。
        hh.setSectionResizeMode(QHeaderView.Fixed)
        hh.setSectionResizeMode(COL_STATE, QHeaderView.Stretch)
        fm = self.table.fontMetrics()
        pad = 24
        for col, sample in ((COL_NAME, "十二個字的角色名字"),
                            (COL_ACCT, "fred26016041")):
            self.table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)
        root.addWidget(self.table, 1)

        self.status = QLabel("找分身中…")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(REFRESH_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        self._tick(force=True)

    def on_close(self) -> None:
        self._timer.stop()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()

    # ------------------------------------------------------------------
    def _on_unfreeze_toggled(self, on: bool) -> None:
        self._unfreeze_on = on
        config.set("master.unfreeze", on)
        config.save()                   # ★ 一定要存檔，不然工具箱重開就忘了（見 config.set 只改記憶體）
        self._tick(force=True)          # 立刻套用／還原，不用等下一拍

    def _tick(self, force: bool = False) -> None:
        if not force and not self.isVisible():
            return
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        pids = [w.pid for w in wins]
        if pids != self._pids:
            self._rebuild(wins)
        self._reconcile_and_show(wins)

    def _rebuild(self, wins) -> None:
        alive = {w.pid for w in wins}
        for pid in list(self._scanners):
            if pid not in alive:
                self._scanners.pop(pid).close()
                self._named.discard(pid)
        self._loading = True
        try:
            self.table.setRowCount(len(wins))
            for r, w in enumerate(wins):
                if w.pid not in self._scanners:
                    sc = MemoryScanner()
                    try:
                        sc.open(w.pid)
                        locate.warm(sc)     # 改版位移自動校正（只真的掃一次）
                    except Exception:       # noqa: BLE001
                        sc.close()
                        sc = None
                    if sc is not None:
                        self._scanners[w.pid] = sc
                for c in (COL_NAME, COL_ACCT, COL_STATE):
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.ItemIsEnabled)
                    self.table.setItem(r, c, it)
        finally:
            self._loading = False
        self._pids = [w.pid for w in wins]

    def _reconcile_and_show(self, wins) -> None:
        want = self._unfreeze_on
        n_on = n_off = n_bad = 0
        for r, w in enumerate(wins):
            pid = w.pid
            acct = _acct(w.title)
            sc = self._scanners.get(pid)

            self._set(r, COL_NAME,
                      preload.name_of(pid, account=acct) or "（還沒登入）")
            self._set(r, COL_ACCT, acct or "—")
            if acct and sc is not None and pid not in self._named:
                self._named.add(pid)
                QTimer.singleShot(
                    0, lambda p=pid, a=acct: self._resolve_name(p, a))

            if sc is None:
                self._set(r, COL_STATE, "⚠ 連不上這台", QColor(theme.BAD))
                n_bad += 1
                continue

            st = unfreeze.state(sc)
            # 核對「想要 vs 現況」，補齊差異（apply/remove 都是冪等的）。
            # ★ "mixed"＝套了一半（多半是舊版工具箱只套了第一處）——
            #   照「想要的方向」推一把就會補齊／收乾淨。
            if st != "unknown" and sc.can_write:
                if want and st in ("off", "mixed"):
                    unfreeze.apply(sc)
                    st = unfreeze.state(sc)
                elif not want and st in ("on", "mixed"):
                    unfreeze.remove(sc)
                    st = unfreeze.state(sc)

            if st == "on":
                self._set(r, COL_STATE, "✅ 失焦不凍", QColor(theme.OK))
                n_on += 1
            elif st == "off":
                self._set(r, COL_STATE, "會凍（原始）", QColor(theme.MUTED))
                n_off += 1
            elif st == "mixed":
                # 沒有寫入權限時會停在這裡出不去，照樣算「有問題」提醒。
                self._set(r, COL_STATE, "⚠ 套用到一半（下一拍補齊）",
                          QColor(theme.BAD))
                n_bad += 1
            elif not sc.can_write:
                self._set(r, COL_STATE,
                          "⚠ 沒有寫入權限（請用系統管理員身分執行）",
                          QColor(theme.BAD))
                n_bad += 1
            else:
                self._set(r, COL_STATE,
                          "⚠ 定位不到（可能改版了）", QColor(theme.BAD))
                n_bad += 1

        if not wins:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        elif not want:
            self.status.setText(
                f"找到 {len(wins)} 台。「畫面凍結解除」目前關閉。")
        else:
            msg = f"畫面凍結解除：已套用 {n_on} / {len(wins)} 台"
            if n_off:
                msg += f"（{n_off} 台套用中…）"
            if n_bad:
                msg += f"，有問題 {n_bad} 台"
            self.status.setText(msg)

    # ------------------------------------------------------------------
    def _set(self, row: int, col: int, text: str,
             colour: QColor | None = None) -> None:
        it = self.table.item(row, col)
        if it is None:
            return
        if it.text() != text:
            self._loading = True
            try:
                it.setText(text)
            finally:
                self._loading = False
        if colour is not None and it.foreground().color() != colour:
            it.setForeground(colour)

    def _resolve_name(self, pid: int, account: str) -> None:
        """把某一台的角色名解出來記進 preload 快取（一個 pid 只做一次）。"""
        sc = self._scanners.get(pid)
        if sc is None:
            return
        try:
            preload.name_of(pid, sc, account)
        except Exception:                       # noqa: BLE001
            pass
