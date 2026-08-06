"""販賣裝備分頁：把背包裡不要的東西賣給 NPC 商人。

怎麼用
------
1. 在遊戲裡自己走到商人身邊、跟他說話，把「賣東西」的畫面開著。
2. 這裡按「重新讀背包」，勾要賣的那幾格，按「賣出勾選的」。

**為什麼要你自己先跟商人說話**：伺服器是靠「點 NPC」那一包記住你在跟誰交易的，
沒有那一包，賣出封包送過去也是被丟掉。替使用者自動去點商人要先認得出哪隻是
商人、還要走過去，那是另一件事，不混在這裡做。

背後
----
* 背包：`app/game/bag.py` —— 走遊戲自己的容器（實體 +0x2FC），
  格號就是陣列索引，不必靠經驗球 AOB 猜表頭。
* 賣出：`app/game/sell.py` —— 照抄 `npcsalesellconfirm` 送的封包（代號 0x28）。

⚠ 只列遊戲認定的背包格（0x14~0xA9）。**身上穿著的裝備不在裡面**，
  所以這個分頁不可能把你正在穿的東西賣掉。
⚠ 售價 <= 0 的東西遊戲自己就不讓賣，這裡也一樣列成「賣不掉」且不能勾。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import bag, locate, move, sell
from app.tabs.base_tab import BaseTab

COLS = ("賣", "格", "名稱", "數量", "耐久", "單價", "小計")
COL_CHECK, COL_SLOT, COL_NAME, COL_COUNT, COL_DURA, COL_PRICE, COL_TOTAL = range(7)


class SellTab(BaseTab):
    TAB_TITLE = "販賣裝備"
    ORDER = 47                       # 排在能量晶化（45）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._rows: list[bag.Item] = []      # 跟表格列一一對應

        root = QVBoxLayout(self)

        hint = QLabel(
            "先在遊戲裡走到商人身邊跟他說話、把「賣東西」的畫面開著，再回來按賣出。"
            "送的是跟你自己按「確定」完全一樣的封包（呼叫遊戲自己的函式，"
            "加解密都由客戶端處理）。只列背包，身上穿的裝備不會出現在這裡。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self.refresh_bag)
        bar.addWidget(self.who)
        reload_btn = QPushButton("重新整理")
        reload_btn.setToolTip("重新列出目前開著的遊戲分身。")
        reload_btn.clicked.connect(self.reload_instances)
        bar.addWidget(reload_btn)
        bar.addSpacing(12)
        read_btn = QPushButton("重新讀背包")
        read_btn.clicked.connect(self.refresh_bag)
        bar.addWidget(read_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        foot = QHBoxLayout()
        gear_btn = QPushButton("勾選所有裝備")
        gear_btn.setToolTip("勾起背包裡所有「有耐久」的東西（武器防具那類）。")
        gear_btn.clicked.connect(lambda: self._check_all(gear_only=True))
        foot.addWidget(gear_btn)
        none_btn = QPushButton("全部取消")
        none_btn.clicked.connect(lambda: self._check_all(clear=True))
        foot.addWidget(none_btn)
        foot.addStretch(1)
        self.sum_lbl = QLabel("已勾 0 件")
        self.sum_lbl.setStyleSheet("font-weight: bold;")
        foot.addWidget(self.sum_lbl)
        foot.addSpacing(12)
        self.sell_btn = QPushButton("賣出勾選的")
        self.sell_btn.setToolTip("送出賣出封包。要先跟商人說話、商店畫面開著。")
        self.sell_btn.clicked.connect(self.do_sell)
        foot.addWidget(self.sell_btn)
        root.addLayout(foot)

        self.status = QLabel("按「重新整理」找分身")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self) -> None:
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                            # noqa: BLE001
                continue
            try:
                locate.warm(sc)                # 改版位移自動校正（只做一次）
            except Exception:                            # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            # ⚠ 用預讀快取，不要現掃角色名（一台 1~2 秒，五台就整個卡住）。
            self._scanners[w.pid] = sc
            self.who.addItem(f"{preload.name_of(w.pid, sc, acc)}（{acc}）", w.pid)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            self._fill([])
            return
        self.status.setText(f"找到 {len(self._scanners)} 個分身")
        self.refresh_bag()

    # ------------------------------------------------------------------
    def _cur(self):
        pid = self.who.currentData()
        if pid is None:
            return None, None
        return int(pid), self._scanners.get(int(pid))

    def refresh_bag(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self._fill([])
            return
        rows = bag.items(sc)
        self._fill(rows)
        if not rows:
            self.status.setText(
                "背包讀不到東西 —— 角色進場了嗎？（換地圖／登入畫面時讀不到）")
        else:
            ok = sum(1 for r in rows if r.sellable)
            self.status.setText(f"背包 {len(rows)} 格有東西，其中 {ok} 格賣得掉")

    def _fill(self, rows: list[bag.Item]) -> None:
        self._rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, it in enumerate(rows):
            chk = QTableWidgetItem()
            if it.sellable:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Unchecked)
            else:
                # 賣不掉的列不給勾（遊戲自己也不列這種東西）
                chk.setFlags(Qt.ItemIsEnabled)
                chk.setToolTip("這個東西賣不掉（遊戲裡的售價是 0）")
            self.table.setItem(r, COL_CHECK, chk)
            cells = {
                COL_SLOT: str(it.slot),
                COL_NAME: it.name,
                COL_COUNT: f"{it.count:,}",
                COL_DURA: str(it.dura) if it.dura else "—",
                COL_PRICE: f"{it.price:,}" if it.sellable else "賣不掉",
                COL_TOTAL: f"{it.price * it.count:,}" if it.sellable else "—",
            }
            for c, text in cells.items():
                cell = QTableWidgetItem(text)
                if c != COL_NAME:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        self._update_sum()

    # ------------------------------------------------------------------
    def _checked(self) -> list[bag.Item]:
        out = []
        for r, it in enumerate(self._rows):
            cell = self.table.item(r, COL_CHECK)
            if cell is not None and cell.checkState() == Qt.Checked:
                out.append(it)
        return out

    def _check_all(self, gear_only: bool = False, clear: bool = False) -> None:
        self.table.blockSignals(True)
        for r, it in enumerate(self._rows):
            cell = self.table.item(r, COL_CHECK)
            if cell is None or not it.sellable:
                continue
            want = (not clear) and (it.is_gear or not gear_only)
            cell.setCheckState(Qt.Checked if want else Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_sum()

    def _on_item_changed(self, _item) -> None:
        self._update_sum()

    def _update_sum(self) -> None:
        picked = self._checked()
        money = sum(it.price * it.count for it in picked)
        self.sum_lbl.setText(f"已勾 {len(picked)} 件　約 {money:,} 金幣")

    # ------------------------------------------------------------------
    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，不要自己 new 一個 —— 同一個遊戲行程
          只能有一份跳板，自己裝會把掛機分頁那份拆掉。
        """
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception as exc:                         # noqa: BLE001
            self._movers.pop(pid, None)
            self.status.setText(f"⚠ 無法安裝跳板：{exc}")
            return None
        self._movers[pid] = mv
        return mv

    def do_sell(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("請先選一個分身")
            return
        picked = self._checked()
        if not picked:
            self.status.setText("還沒勾任何東西")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        # ⚠ 賣之前重讀一次背包：撿到東西／用掉藥水都會讓格子重排，
        #   拿舊的序號去賣等於賣到別的東西。序號對不上就整批停手。
        fresh = {(it.serial, it.stamp): it for it in bag.items(sc)}
        rows, gone = [], []
        for it in picked:
            now = fresh.get((it.serial, it.stamp))
            if now is None:
                gone.append(it.name)
            else:
                rows.append((now.serial, now.stamp, now.count))
        if gone:
            self.status.setText(
                f"⚠ 背包變了（{'、'.join(gone[:3])}… 已經不在原本的位置），"
                "沒有送出任何東西。請按「重新讀背包」再勾一次。")
            self.refresh_bag()
            return

        ok, msg = sell.sell_items(mv, sc, rows)
        self.status.setText(("✔ " if ok else "⚠ ") + msg
                            + ("　賣完可以按「重新讀背包」對一下" if ok else ""))
        if ok:
            self.refresh_bag()

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        # ★ 用 release() 不要 stop()：跳板是同一個 PID 共用的。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                            # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
