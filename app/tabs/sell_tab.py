"""販賣裝備分頁：把背包裡不要的裝備賣給 NPC 商人。

怎麼用
------
1. 在遊戲裡自己走到商人身邊、跟他說話，把「賣東西」的畫面開著。
2. 手動：勾要賣的，按「賣出勾選的」。
   自動：按「自動賣白裝」或「自動賣白＋藍裝」，每 3 秒清一次背包，按「暫停」停。

**為什麼要你自己先跟商人說話**：伺服器是靠「點 NPC」那一包記住你在跟誰交易的，
沒有那一包，賣出封包送過去也是被丟掉。替使用者自動去點商人要先認得出哪隻是
商人、還要走過去，那是另一件事，不混在這裡做。

背後
----
* 背包：`app/game/bag.py` —— 走遊戲自己的容器（實體 +0x2FC），格號就是陣列索引。
* 賣出：`app/game/sell.py` —— 照抄 `npcsalesellconfirm` 送的封包（代號 0x28）。

⚠ 只列遊戲認定的背包格（0x14~0xA9）。**身上穿著的裝備不在裡面**，
  所以這個分頁不可能把你正在穿的東西賣掉。
⚠ 售價 <= 0 的東西遊戲自己就不讓賣，這裡也一樣列成「賣不掉」且不能勾。

自動賣只碰「裝備／武器」
------------------------
判準是 `bag.Item.is_gear` ＝ **範本的耐久上限 > 0**，跟 item.xml 的「耐久」欄
32476 筆 100% 吻合。所以藥水、卡片、經驗球、座騎、扭蛋**一律不會被自動賣掉**，
壞掉的裝備（耐久現值 0）則照樣算裝備、賣得掉。

品質（普通白／優質藍／頂級橘）
------------------------------
**不是看名字認的**，是讀遊戲畫名字顏色時用的那個欄位（範本 +0x130），
拿 `setting/base/item*.xml` 的 `顏色=` 欄整張對帳過 —— 32476 筆 100% 吻合。
細節見 `app/game/bag.py`。

統計怎麼算才誠實
----------------
送出封包不代表賣掉了（伺服器可能不收）。所以每一輪送完之後會**等 0.9 秒再對一次
背包**：真的從背包消失的才記進歷史；金額用**金幣的實際變化量**，不是用價目表估的。
兩個數字都會顯示，對不上時一眼就看得出來。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
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

COLS = ("賣", "格", "名稱", "品質", "數量", "耐久", "單價", "小計")
(COL_CHECK, COL_SLOT, COL_NAME, COL_GRADE, COL_COUNT, COL_DURA,
 COL_PRICE, COL_TOTAL) = range(8)

HIST_COLS = ("時間", "物品", "品質", "數量", "表定金額")
HIST_HEIGHT = 140
HIST_MAX = 800              # 只留最近這麼多列，掛整晚也不會吃掉記憶體

# 名稱與品質欄的字色，**直接用遊戲自己那三個顏色**（見 app/game/bag.py）：
# 普通不上色（跟著佈景走＝白）、優質 #00F7FF 青藍、頂級 #FFCE10 橘金。
# 佈景是深色的（app/theme.py BG = #1e2230），這兩個亮色在上面都很清楚。
GRADE_FG = {bag.GRADE_FINE: QColor("#00F7FF"), bag.GRADE_TOP: QColor("#FFCE10")}

def _refill(table, stretch_col: int, fill) -> None:
    """整批重填表格 —— 填的期間把表頭的自動欄寬關掉。

    ⚠⚠ 表頭是 ResizeToContents 時，**每一次 setItem 都會同步重量整欄**
      （把該欄每一列都量一遍）。整張重畫就是 O(格數 × 列數)：
      歷史表 800 列 × 5 欄 = 4000 次 setItem、每次掃 800 列，
      一次重畫要燒幾十秒 CPU —— 賣到歷史滿 800 列之後，重畫比結算
      間隔還慢，介面就永久凍住（實際發生：賣了 1000 多件之後整支
      程式未回應；py-spy 抓到主執行緒卡在 setItem，記憶體平穩）。
    做法：填之前切成 Fixed（setItem 變 O(1)），填完切回
      ResizeToContents —— 切回去那一下才做**一次**整欄重量，
      結果一樣、工作量少四千倍。
    """
    hh = table.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.Fixed)
    try:
        fill()
    finally:
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(stretch_col, QHeaderView.Stretch)


REFRESH_MS = 1000           # 背包即時更新的間隔（純讀，一次幾毫秒）
AUTO_MS = 3000              # 自動賣出的間隔 —— 使用者指定 3 秒
# 送出到「背包少一件、金幣變多」反映出來要多久。用回程道具實測是 1.5 秒才換地圖，
# 但那包含伺服器換場景；單純扣物品加錢快得多。0.9 秒是保守值，
# 沒結算完也不會漏 —— 下一輪會再看到那件東西，頂多晚一輪記帳。
SETTLE_MS = 900


class SellTab(BaseTab):
    TAB_TITLE = "販賣裝備"
    ORDER = 47                       # 排在能量晶化（45）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._rows: list[bag.Item] = []      # 跟表格列一一對應
        self._sig = None                     # 背包內容的指紋，變了才重畫
        # pid -> [(時間, 名稱, 品質, 數量, 表定金額)]
        self._hist: dict[int, list[tuple]] = {}
        self._earned: dict[int, int] = {}    # pid -> 金幣實際增加的總和
        self._auto: frozenset[int] | None = None   # 自動賣哪些品質；None = 沒在跑
        self._pending = None                 # (pid, {鍵: 物品}, 賣之前的金幣)

        root = QVBoxLayout(self)

        hint = QLabel(
            "先在遊戲裡走到商人身邊跟他說話、把「賣東西」的畫面開著，再按賣出。"
            "送的是跟你自己按「確定」完全一樣的封包。背包每秒自動更新；"
            "自動賣只會動「裝備／武器」，藥水卡片經驗球都不會被賣掉。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        reload_btn = QPushButton("重新整理")
        reload_btn.setToolTip("重新列出目前開著的遊戲分身。")
        reload_btn.clicked.connect(self.reload_instances)
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        self.gold_lbl = QLabel("金幣 —")
        self.gold_lbl.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.gold_lbl)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.table.itemChanged.connect(lambda _i: self._update_sum())
        root.addWidget(self.table, 1)

        # --- 手動：勾選 + 賣出 ------------------------------------------
        foot = QHBoxLayout()
        white_btn = QPushButton("只勾普通白裝")
        white_btn.setToolTip("只勾起品質是普通(白)的裝備／武器；優質(藍)、頂級(橘) 不會被勾到。")
        white_btn.clicked.connect(
            lambda: self._check(lambda it: it.is_gear
                                and it.grade == bag.GRADE_NORMAL))
        foot.addWidget(white_btn)
        gear_btn = QPushButton("勾選所有裝備")
        gear_btn.setToolTip("勾起背包裡所有裝備／武器，不分品質。")
        gear_btn.clicked.connect(lambda: self._check(lambda it: it.is_gear))
        foot.addWidget(gear_btn)
        none_btn = QPushButton("全部取消")
        none_btn.clicked.connect(lambda: self._check(lambda it: False))
        foot.addWidget(none_btn)
        foot.addStretch(1)
        self.sum_lbl = QLabel("已勾 0 件")
        self.sum_lbl.setStyleSheet("font-weight: bold;")
        foot.addWidget(self.sum_lbl)
        foot.addSpacing(10)
        self.sell_btn = QPushButton("賣出勾選的")
        self.sell_btn.setToolTip("送出賣出封包。要先跟商人說話、商店畫面開著。")
        self.sell_btn.clicked.connect(self.do_sell)
        foot.addWidget(self.sell_btn)
        root.addLayout(foot)

        # --- 自動 --------------------------------------------------------
        auto = QHBoxLayout()
        self.auto_white = QPushButton("自動賣白裝")
        self.auto_white.setToolTip("每 3 秒把背包裡「普通(白)」的裝備／武器賣掉。")
        self.auto_white.clicked.connect(
            lambda: self._start_auto(frozenset({bag.GRADE_NORMAL}), "白裝"))
        auto.addWidget(self.auto_white)
        self.auto_blue = QPushButton("自動賣白＋藍裝")
        self.auto_blue.setToolTip(
            "每 3 秒把背包裡「普通(白)」與「優質(藍)」的裝備／武器賣掉。"
            "頂級(橘) 不會被賣。")
        self.auto_blue.clicked.connect(
            lambda: self._start_auto(
                frozenset({bag.GRADE_NORMAL, bag.GRADE_FINE}), "白裝＋優質藍裝"))
        auto.addWidget(self.auto_blue)
        self.pause_btn = QPushButton("暫停")
        self.pause_btn.clicked.connect(lambda: self._stop_auto("已暫停自動賣出"))
        auto.addWidget(self.pause_btn)
        auto.addStretch(1)
        self.auto_lbl = QLabel("自動賣出：停止中")
        auto.addWidget(self.auto_lbl)
        root.addLayout(auto)

        # --- 歷史統計 ----------------------------------------------------
        box = QGroupBox("賣出紀錄")
        bl = QVBoxLayout(box)
        self.hist = QTableWidget(0, len(HIST_COLS))
        self.hist.setHorizontalHeaderLabels(HIST_COLS)
        self.hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hist.setSelectionMode(QAbstractItemView.NoSelection)
        self.hist.verticalHeader().setVisible(False)
        hh2 = self.hist.horizontalHeader()
        hh2.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh2.setSectionResizeMode(1, QHeaderView.Stretch)
        self.hist.setFixedHeight(HIST_HEIGHT)
        bl.addWidget(self.hist)
        hb = QHBoxLayout()
        refresh_btn = QPushButton("刷新統計")
        refresh_btn.setToolTip("重畫紀錄、重讀金幣，並重算總共賣了多少。")
        refresh_btn.clicked.connect(self._refresh_stats)
        hb.addWidget(refresh_btn)
        clear_btn = QPushButton("清空紀錄")
        clear_btn.clicked.connect(self._clear_hist)
        hb.addWidget(clear_btn)
        hb.addStretch(1)
        self.total_lbl = QLabel("這個分身還沒賣過東西")
        self.total_lbl.setStyleSheet("font-weight: bold;")
        hb.addWidget(self.total_lbl)
        bl.addLayout(hb)
        root.addWidget(box)

        self.status = QLabel("按「重新整理」找分身")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # 背包即時更新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_refresh)
        self._timer.start(REFRESH_MS)
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_tick)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self) -> None:
        self._stop_auto()
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
        self._sig = None
        self._tick_refresh()
        self._refresh_stats()

    def _on_who_changed(self) -> None:
        # ⚠ 換分身一定要停掉自動賣 —— 不然會繼續對「新選的那台」賣下去。
        self._stop_auto("換了分身，自動賣出已停止")
        self._sig = None
        self._tick_refresh()
        self._refresh_stats()

    # ------------------------------------------------------------------
    def _cur(self):
        pid = self.who.currentData()
        if pid is None:
            return None, None
        return int(pid), self._scanners.get(int(pid))

    def _tick_refresh(self) -> None:
        """每秒讀一次背包；內容沒變就不重畫（免得一直洗掉勾選）。"""
        # 切到別的分頁就別讀了（自動賣是另一個計時器，不受影響）。
        if not self.isVisible():
            return
        pid, sc = self._cur()
        if sc is None:
            return
        try:
            rows = bag.items(sc)
            money = bag.gold(sc)
        except Exception:                                # noqa: BLE001
            return
        self.gold_lbl.setText(
            f"金幣 {money:,}" if money is not None else "金幣 —")
        sig = tuple((r.slot, r.serial, r.stamp, r.count, r.dura) for r in rows)
        if sig != self._sig:
            self._sig = sig
            self._fill(rows, keep=self._checked_keys())

    def _fill(self, rows: list[bag.Item], keep: set | None = None) -> None:
        keep = keep or set()
        self._rows = rows
        self.table.blockSignals(True)
        # ⚠ 一定要包 _refill：這張表每秒都可能整批重畫（撿東西、賣東西
        #   背包就變），不包的話每格 setItem 都重量欄寬，介面會越用越卡。
        _refill(self.table, COL_NAME, lambda: self._fill_rows(rows, keep))
        self.table.blockSignals(False)
        self._update_sum()

    def _fill_rows(self, rows: list[bag.Item], keep: set) -> None:
        self.table.setRowCount(len(rows))
        for r, it in enumerate(rows):
            chk = QTableWidgetItem()
            if it.sellable:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(
                    Qt.Checked if (it.serial, it.stamp) in keep else Qt.Unchecked)
            else:
                # 賣不掉的列不給勾（遊戲自己也不列這種東西）
                chk.setFlags(Qt.ItemIsEnabled)
                chk.setToolTip("這個東西賣不掉（遊戲裡的售價是 0）")
            self.table.setItem(r, COL_CHECK, chk)
            if it.is_gear:
                dura = f"{it.dura}/{it.dura_max}" + ("　壞了" if it.broken else "")
            else:
                dura = "—"
            cells = {
                COL_SLOT: str(it.slot),
                COL_NAME: it.name,
                COL_GRADE: it.grade_name if it.is_gear else "—",
                COL_COUNT: f"{it.count:,}",
                COL_DURA: dura,
                COL_PRICE: f"{it.price:,}" if it.sellable else "賣不掉",
                COL_TOTAL: f"{it.price * it.count:,}" if it.sellable else "—",
            }
            fg = GRADE_FG.get(it.grade) if it.is_gear else None
            for c, text in cells.items():
                cell = QTableWidgetItem(text)
                if c != COL_NAME:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if fg is not None and c in (COL_NAME, COL_GRADE):
                    cell.setForeground(fg)
                self.table.setItem(r, c, cell)

    # ------------------------------------------------------------------
    def _checked(self) -> list[bag.Item]:
        out = []
        for r, it in enumerate(self._rows):
            cell = self.table.item(r, COL_CHECK)
            if cell is not None and cell.checkState() == Qt.Checked:
                out.append(it)
        return out

    def _checked_keys(self) -> set:
        return {(it.serial, it.stamp) for it in self._checked()}

    def _check(self, want) -> None:
        """照 want(物品) 重新設定每一列的勾選（賣不掉的一律不勾）。"""
        self.table.blockSignals(True)
        for r, it in enumerate(self._rows):
            cell = self.table.item(r, COL_CHECK)
            if cell is None or not it.sellable:
                continue
            cell.setCheckState(Qt.Checked if want(it) else Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_sum()

    def _update_sum(self) -> None:
        picked = self._checked()
        money = sum(it.price * it.count for it in picked)
        # ★ 勾到優質／頂級就明講。賣掉好裝備是不可逆的，數字要主動送到眼前。
        good = [it for it in picked
                if it.is_gear and it.grade != bag.GRADE_NORMAL]
        warn = ""
        if good:
            n_fine = sum(1 for it in good if it.grade == bag.GRADE_FINE)
            n_top = len(good) - n_fine
            parts = ([f"優質(藍) {n_fine} 件"] if n_fine else []) + \
                    ([f"頂級(橘) {n_top} 件"] if n_top else [])
            warn = "　⚠ 含 " + "、".join(parts)
        self.sum_lbl.setText(f"已勾 {len(picked)} 件　約 {money:,} 金幣{warn}")

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
        """手動：賣掉勾起來的那幾件。"""
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("請先選一個分身")
            return
        picked = self._checked()
        if not picked:
            self.status.setText("還沒勾任何東西")
            return
        # ⚠ 賣之前重讀一次背包：撿到東西／用掉藥水都會讓格子重排，
        #   拿舊的序號去賣等於賣到別的東西。序號對不上就整批停手。
        fresh = {(it.serial, it.stamp) for it in bag.items(sc)}
        gone = [it.name for it in picked if (it.serial, it.stamp) not in fresh]
        if gone:
            self.status.setText(
                f"⚠ 背包變了（{'、'.join(gone[:3])} 已經不在原本的位置），"
                "沒有送出任何東西。請重新勾一次。")
            self._sig = None
            self._tick_refresh()
            return
        self._send(pid, sc, picked)

    # ------------------------------------------------------------------
    # 自動賣出
    # ------------------------------------------------------------------
    def _start_auto(self, grades: frozenset[int], what: str) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("請先選一個分身")
            return
        if self._mover(pid) is None:
            return
        self._auto = grades
        self.auto_lbl.setText(f"自動賣出：每 {AUTO_MS // 1000} 秒賣 {what}")
        self.status.setText(
            f"開始自動賣 {what} —— 記得讓商店的「賣東西」畫面一直開著。")
        self._auto_timer.start(AUTO_MS)
        self._auto_tick()                    # 不必等第一個 3 秒

    def _stop_auto(self, msg: str = "") -> None:
        was_running = self._auto is not None
        self._auto_timer.stop()
        self._auto = None
        self.auto_lbl.setText("自動賣出：停止中")
        # 本來就沒在跑就別報「已停止」—— 那只會讓人以為剛剛有東西在動。
        if msg and was_running:
            self.status.setText(msg)

    def _auto_tick(self) -> None:
        if self._auto is None:
            return
        if self._pending is not None:
            return                           # 上一輪還在等結算，跳過這一拍
        pid, sc = self._cur()
        if sc is None:
            self._stop_auto("分身不見了，自動賣出已停止")
            return
        try:
            rows = bag.items(sc)
        except Exception:                                # noqa: BLE001
            return
        want = [it for it in rows
                if it.is_gear and it.sellable and it.grade in self._auto]
        if not want:
            self.status.setText("背包裡沒有符合條件的裝備，繼續等下一輪…")
            return
        self._send(pid, sc, want)

    # ------------------------------------------------------------------
    def _send(self, pid: int, sc, items: list[bag.Item]) -> None:
        """送出賣出封包，並排一次結算。"""
        if self._pending is not None:
            self.status.setText("上一批還在確認中，等一下再按")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        before = bag.gold(sc)
        keys = {(it.serial, it.stamp): it for it in items}
        ok, msg = sell.sell_items(
            mv, sc, [(it.serial, it.stamp, it.count) for it in items])
        if not ok:
            self.status.setText("⚠ " + msg)
            return
        self.status.setText(f"{msg}　確認中…")
        self._pending = (pid, keys, before)
        QTimer.singleShot(SETTLE_MS, self._settle)

    def _settle(self) -> None:
        """對一次背包：真的消失的才算賣掉，金額用金幣的實際變化量。"""
        if self._pending is None:
            return
        pid, keys, before = self._pending
        self._pending = None
        sc = self._scanners.get(pid)
        if sc is None:
            return
        try:
            rows = bag.items(sc)
            after = bag.gold(sc)
        except Exception:                                # noqa: BLE001
            return
        still = {(it.serial, it.stamp) for it in rows}
        sold = [it for k, it in keys.items() if k not in still]
        if not sold:
            self.status.setText(
                "⚠ 封包送出去了，但背包一件都沒少 —— "
                "商店的「賣東西」畫面還開著嗎？（沒開的話伺服器會直接丟掉）")
            return
        gained = (after - before) if (after is not None and before is not None) else 0
        now = time.strftime("%H:%M:%S")
        log = self._hist.setdefault(pid, [])
        for it in sold:
            log.append((now, it.name, it.grade_name, it.count,
                        it.price * it.count, it.grade))
        del log[:-HIST_MAX]
        self._earned[pid] = self._earned.get(pid, 0) + gained
        self.status.setText(
            f"✔ 賣掉 {len(sold)} 件，金幣 +{gained:,}")
        self._sig = None
        self._tick_refresh()
        self._refresh_stats()

    # ------------------------------------------------------------------
    # 統計
    # ------------------------------------------------------------------
    def _refresh_stats(self) -> None:
        pid, sc = self._cur()
        log = self._hist.get(pid, []) if pid is not None else []
        # ⚠ 一定要包 _refill：這張表滿載 800 列、每次結算都整張重畫，
        #   就是「賣了一千多件之後整支程式凍死」的那顆地雷（見 _refill）。
        _refill(self.hist, 1, lambda: self._fill_hist(log))
        if log:
            self.hist.scrollToBottom()
        if sc is not None:
            money = bag.gold(sc)
            self.gold_lbl.setText(
                f"金幣 {money:,}" if money is not None else "金幣 —")
        if not log:
            self.total_lbl.setText("這個分身還沒賣過東西")
            return
        listed = sum(row[4] for row in log)
        earned = self._earned.get(pid, 0)
        extra = "" if earned == listed else f"（價目表算是 {listed:,}）"
        self.total_lbl.setText(
            f"共賣出 {len(log)} 件　實際入帳 {earned:,} 金幣{extra}")

    def _fill_hist(self, log: list[tuple]) -> None:
        self.hist.setRowCount(len(log))
        for r, (t, name, grade_name, count, money, grade) in enumerate(log):
            fg = GRADE_FG.get(grade)
            for c, text, right in ((0, t, False), (1, name, False),
                                   (2, grade_name, True),
                                   (3, f"{count:,}", True),
                                   (4, f"{money:,}", True)):
                cell = QTableWidgetItem(text)
                if right:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if fg is not None and c in (1, 2):
                    cell.setForeground(fg)
                self.hist.setItem(r, c, cell)

    def _clear_hist(self) -> None:
        pid, _sc = self._cur()
        if pid is None:
            return
        self._hist.pop(pid, None)
        self._earned.pop(pid, None)
        self._refresh_stats()

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._auto_timer.stop()
        # ⚠ 更新用的計時器也要停：下面會把 scanner 全部關掉，計時器還在跑的話
        #   會拿已經關閉的控制碼去讀記憶體。
        self._timer.stop()
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
