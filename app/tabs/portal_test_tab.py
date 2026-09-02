r"""傳點測試（開發用）：列出這張圖的傳點／機關，**按一下送一次進入封包**。

## 這一頁在測什麼

2026-09-02 從使用者「進副本」的擷取解出來：進傳點那一包是 **`0x0D`**，平常
**不是我們送的** —— 是遊戲客戶端每一拍檢查「有沒有人踩在這個物件上」然後自己
送（反組譯全文在 `app/game/portal.py` 檔頭）。內容是：

    0x0D(玩家 +0x1D0, 觸發物件 +0x1D0, u8 動作碼)

這一頁就是把那一包**手動送一次**：挑一個物件 → 按鈕 → 叫遊戲自己那支
`portal.SEND_FN` 送出去，位元組跟真的踩上去一模一樣。

⚠⚠ **不走過去、不重試**（使用者 2026-09-02 指定：「單純按鈕按一下發一次進入
  封包」）。要再送就再按一次 —— 按鈕本身就是重試。

## 送出去之後怎麼判斷成不成功

按下去之後盯 `OBSERVE_SECS` 秒（純讀，不做任何動作），三種結果分開講：

* **人被搬走了**（一拍跳 ≥ `JUMP_TILES` 格，或場景編號變了）＝ 成功。
* **去重欄 `+0x208` 變成我**＝ 客戶端側的狀態動了（多半是同一拍它自己也踩到）。
* **什麼都沒發生**＝ 伺服器收了沒反應。最可能的原因是**它會檢查你站在哪**
  （我們是隔空送的），其次才是等級／任務／隊伍條件不到。

⛔ 三種結果不准合併成一句「失敗」—— 那就是 CLAUDE.md 說的「安靜地做錯事」的
  反面教材：使用者要能從訊息分辨「沒送出去」「送了沒反應」「送了成功」。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
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
from app.game import bag, entity, locate, move, portal, scene, terrain
from app.tabs.base_tab import BaseTab

# 掃描列出傳點的半徑（格）。整張圖都列出來太多，附近這一圈才是要試的。
SCAN_RADIUS = 60.0
# 送出去之後盯多久（秒）、多久看一次。
OBSERVE_SECS = 6.0
OBSERVE_MS = 100
# 順移判定（跟 dungeon_tab 同一組數字：跑步一拍最多 0.6 格）。
JUMP_TILES = 3.0
JUMP_MAX_GAP = 0.35

COLS = ("距離", "座標", "名字", "外觀", "封包id", "旗標", "動作碼", "去重欄",
        "走得到")
LOG_COLS = ("時間", "描述")
LOG_MAX = 300


def _d(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class PortalTestTab(BaseTab):
    TAB_TITLE = "傳點測試"
    ORDER = 8                        # 排在自動刷副本（7）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._maps = terrain.Cache()
        self._rows: list[portal.Trigger] = []
        self._watch = None           # 送出去之後的觀察狀態

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        b = QPushButton("重新整理")
        b.setToolTip("重新列出目前開著的遊戲分身。")
        b.clicked.connect(lambda: self.reload_instances(force_names=True))
        bar.addWidget(b)
        bar.addStretch(1)
        self.here_lbl = QLabel("－")
        bar.addWidget(self.here_lbl)
        root.addLayout(bar)

        box = QGroupBox("這張圖上「踩上去會有事發生」的物件")
        v = QVBoxLayout(box)
        h = QHBoxLayout()
        b = QPushButton("🔍 掃描")
        b.setToolTip(f"列出附近 {SCAN_RADIUS:.0f} 格內、遊戲自己認得的觸發物件。\n"
                     "旗標與動作碼是從遊戲的判斷式讀出來的，不是猜的。")
        b.clicked.connect(self._scan)
        h.addWidget(b)
        self.send_btn = QPushButton("📨 送一次進入封包")
        self.send_btn.setToolTip(
            "對選中那個物件送一次 0x0D（＝遊戲踩上去時自己送的那一包）。\n"
            "只送一次、不會自己重試；要再送就再按一次。\n"
            "⚠ 是隔空送的：伺服器若檢查站位，會收了沒反應。")
        self.send_btn.clicked.connect(self._send_once)
        h.addWidget(self.send_btn)
        h.addStretch(1)
        v.addLayout(h)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        for i in range(len(COLS) - 1):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(len(COLS) - 1, QHeaderView.Stretch)
        v.addWidget(self.table, 1)
        root.addWidget(box, 1)

        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log.setSelectionMode(QAbstractItemView.NoSelection)
        self.log.verticalHeader().setVisible(False)
        lh = self.log.horizontalHeader()
        # ⚠ 追加式紀錄表的時間欄不開 ResizeToContents（[[qt-ui-pitfalls]]）。
        lh.setSectionResizeMode(0, QHeaderView.Fixed)
        lh.resizeSection(
            0, self.log.fontMetrics().horizontalAdvance("00:00:00") + 24)
        lh.setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.log, 1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._observe)
        self._idle = QTimer(self)
        self._idle.timeout.connect(self._refresh_here)
        self._idle.start(1000)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        self._timer.stop()
        self._watch = None
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._rows = []
        self.table.setRowCount(0)
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
                locate.warm(sc)
            except Exception:                            # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            self._scanners[w.pid] = sc
            self.who.addItem(
                f"{preload.name_of(w.pid, sc, acc, force=force_names)}"
                f"（{acc}）", w.pid)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        self._refresh_here()

    def _on_who_changed(self) -> None:
        # ⚠ 換分身要把清單清掉：上一台掃到的物件位址在這一台完全不算數。
        self._timer.stop()
        self._watch = None
        self._rows = []
        self.table.setRowCount(0)
        self.status.setText("換了分身 —— 重新掃描一次")
        self._refresh_here()

    def _cur(self):
        pid = self.who.currentData()
        if pid is None:
            return None, None
        return int(pid), self._scanners.get(int(pid))

    def _mover_of(self, pid: int):
        """這台分身的跳板。⚠⚠ 一定要走 `move.acquire()`（一個 PID 一份）。"""
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

    # ------------------------------------------------------------------
    @staticmethod
    def _pf(sc):
        """玩家物件（+0xBC 是 eid、+0x1D0 是封包用的 id）。讀不到回 None。"""
        return bag.player_entity(sc)

    def _me(self, sc):
        """我站的格子。⚠ 實體本體＝玩家物件 +8（[[entity-coordinates]]）。"""
        pf = self._pf(sc)
        return entity.read_pos(sc, pf + 8) if pf else None

    @staticmethod
    def _eid(sc, pf) -> int:
        """我的網路 eid（去重欄 +0x208 記的就是它）。讀不到回 0。"""
        if not pf:
            return 0
        raw = sc._read_bytes(pf + move.MGR.OBJ_ID, 4)
        return int.from_bytes(bytes(raw), "little") if raw else 0

    def _log(self, text: str) -> None:
        # ⚠ 追加式紀錄表一律插一列，不要整表重畫。
        self.log.insertRow(0)
        self.log.setItem(0, 0, QTableWidgetItem(time.strftime("%H:%M:%S")))
        self.log.setItem(0, 1, QTableWidgetItem(text))
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)

    def _refresh_here(self) -> None:
        if not self.isVisible() or self._timer.isActive():
            return
        _pid, sc = self._cur()
        if sc is None:
            self.here_lbl.setText("－")
            return
        me = self._me(sc)
        sid = scene.current_id(sc, allow_scan=False)
        where = scene.scene_name(sid) if sid is not None else "？"
        self.here_lbl.setText(
            f"{where}　({me[0]:.1f}, {me[1]:.1f})" if me
            else f"{where}　讀不到位置")

    # ------------------------------------------------------------------
    def _scan(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        me = self._me(sc)
        if me is None:
            self.status.setText("讀不到自己的位置 —— 還在載圖嗎？")
            return
        trigs = portal.nearby(sc, me, SCAN_RADIUS)
        if trigs is None:
            # ⚠ 讀不到 ≠ 這裡沒有傳點。分開講（[[bag-false-empty-guards]]）。
            self.status.setText(
                "⛔ 認不出傳點：那支「每拍檢查有沒有人踩上來」的函式沒定位到"
                if not portal.TRIGGER_FN else "⛔ 讀不到場上物件表（載圖中？）")
            return
        my_eid = self._eid(sc, self._pf(sc))
        grid = self._maps.get(sc)
        reach = grid.reachable(int(me[0]), int(me[1])) if grid else None
        self._rows = trigs
        self.table.setRowCount(0)
        for t in trigs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            if reach is None:
                walk = "？"          # 讀不到地形圖＝不知道，不要寫「走不到」
            else:
                walk = "✔" if (int(t.x), int(t.y)) in reach else "✘ 另一區"
            if my_eid and t.last == my_eid:
                mark = "★ 已經記著我"
            elif t.last:
                mark = f"{t.last:#010x}"
            else:
                mark = "－（沒人踩過）"
            for i, txt in enumerate((
                    f"{t.dist(me):.1f}",
                    f"({t.x:.1f}, {t.y:.1f})",
                    t.name,
                    str(t.model),
                    f"{t.select_id:#010x}",
                    f"{t.flags:#06x}",
                    str(t.code) if t.code is not None else "—",
                    mark, walk)):
                self.table.setItem(row, i, QTableWidgetItem(txt))
        here = scene.scene_name(scene.current_id(sc, allow_scan=False))
        self.status.setText(
            f"{here}：附近 {SCAN_RADIUS:.0f} 格內有 {len(trigs)} 個觸發物件"
            if trigs else
            f"{here}：附近 {SCAN_RADIUS:.0f} 格內一個都沒有"
            "（傳點可能離得遠，走近一點再掃）")

    # ------------------------------------------------------------------
    def _send_once(self) -> None:
        """★ 按一下 ＝ 送一發。不走過去、不自己重試。"""
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            self.status.setText("先在上面挑一個要送的物件")
            return
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        trig = self._rows[row]
        mv = self._mover_of(pid)
        if mv is None:
            return
        pf = self._pf(sc)
        before = portal.read(sc, trig)
        me = self._me(sc)
        ok, msg = portal.enter(mv, sc, trig, pf)
        self._log(msg + (f"　（我在 ({me[0]:.1f}, {me[1]:.1f})、"
                         f"離它 {trig.dist(me):.1f} 格）" if me else ""))
        self.status.setText(msg)
        if not ok:
            return
        # 送出去了 → 盯著看有沒有反應（純讀，不做任何動作）
        self._watch = {
            "sc": sc, "pf": pf, "trig": trig,
            "t0": time.monotonic(),
            "map": scene.map_key(scene.current_id(sc, allow_scan=False)),
            "last": before.last if before else None,
            "pos": me, "pos_t": time.monotonic(),
        }
        self._timer.start(OBSERVE_MS)

    def _observe(self) -> None:
        w = self._watch
        if not w:
            self._timer.stop()
            return
        sc, now = w["sc"], time.monotonic()
        me = self._me(sc)
        # ① 被搬走了嗎 —— 每一拍都要採樣，錯過那一下就看不到了。
        if me is not None:
            prev, prev_t = w["pos"], w["pos_t"]
            w["pos"], w["pos_t"] = me, now
            if (prev is not None and now - prev_t <= JUMP_MAX_GAP
                    and _d(prev, me) >= JUMP_TILES):
                self._done(f"✔ 成功：人被搬到 ({me[0]:.0f}, {me[1]:.0f})")
                return
        here = scene.map_key(scene.current_id(sc, allow_scan=False))
        if here is not None and w["map"] is not None and here != w["map"]:
            self._done(f"✔ 成功：換到「{scene.scene_name(here)}」")
            return
        # ② 客戶端側的去重欄有沒有動（＝它自己也踩到了）
        cur = portal.read(sc, w["trig"])
        my = self._eid(sc, w["pf"])
        if cur is not None and my and cur.last == my and w["last"] != my:
            w["last"] = my
            self._log(f"去重欄變成我了（{my:#010x}）"
                      "—— 客戶端這一拍自己也踩到了")
        left = OBSERVE_SECS - (now - w["t0"])
        self.status.setText(f"送出去了，盯著看反應…{max(left, 0):.1f} 秒")
        if left <= 0:
            self._done(
                "⚠ 送出去了，但 %.0f 秒內人沒被搬走、場景也沒變。"
                "最可能是伺服器會檢查站位（我們是隔空送的）；"
                "其次才是等級／任務／隊伍條件不到。" % OBSERVE_SECS)

    def _done(self, why: str) -> None:
        self._timer.stop()
        self._watch = None
        self.status.setText(why)
        self._log(why)

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        # ⚠⚠ 分頁的收尾一定要寫 on_close（QTabWidget 不會發 close 事件）。
        self._timer.stop()
        self._idle.stop()
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                            # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
