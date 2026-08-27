"""活動分頁：**只放活動期間才有意義的東西**。

★★★ 活動結束 ⇒ **刪掉這個檔 ＋ `app/game/roulette.py` 就好**（主視窗是掃
  `app/tabs/` 自動掛分頁的），其他分頁一行都不用動。

## 功能①：自動使用活動硬幣

掃背包，名字**同時**含 `KEY`（「啤酒節」）**而且**有「x<數字>」的才使用；
沒有「x<數字>」的是**硬幣本體**（例：`2026-啤酒節銅幣`）—— **絕對不使用**。
每 `USE_MS` 用一個。

⚠⚠ **關鍵字那道閘門不能省**：只認「x<數字>」的話，背包裡任何名字帶「x50」
  的東西都會被吃掉（安靜地做錯事）。使用者 2026-08-27 確認過這個規則。

## 功能②：自動抽轉盤（要先在遊戲裡把轉盤視窗開起來）

兩條路二選一（`app/game/roulette.py`），**不能混用**：

* **勾「不等動畫」（預設）**＝ `roulette.draw()` 自己送 `0x164`。
  參數兩個都從轉盤物件**現讀**（`[[物件]]` 與 `[物件+0x29]`，實測跟擷取一字不差）。
* 不勾 ＝ `roulette.spin()` 請遊戲自己按下去，跟手點一模一樣，但要等動畫轉完。

⚠⚠ 混用會**同一轉送兩包＝多扣一次**：`spin()` 會讓客戶端起一個到期計時器，
  時間到它自己也送一包。`draw()` 因此在「客戶端正在轉」時一律讓開。

## ⛔ 不准寫死物品編號 —— 這次改版就是活教材

2026-08-27 官方**在活動中途**把銅幣從 `79912` 換成 `87395`、舊的改名
「2026-啤酒節銅幣**(舊)**」。認名字不認編號，新舊兩組自動都吃得到。

## 兩件送出前一定要做的事

* **格號當場重讀**：`bag.Item.slot` 是**陣列索引**，`recall.use_item` 要的是
  物品自己記的格號（`+0x25`）—— 表頭實測偏過 6 格，拿索引打封包會**用到別的
  東西**。所以一律走 `inventory.find_by_type()`。
* **送出去 ≠ 成功**：用硬幣看「那個種類的總數有沒有變少」，抽轉盤看「背包多了
  什麼」。連著幾輪沒動靜就停下來 —— ⛔ 不准無限重送（每重送一次都可能多花一次）。
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
from app.game import bag, inventory, locate, move, recall, roulette
from app.tabs.base_tab import BaseTab

# ★ 這次活動的關鍵字。換別的活動就改這一行（整個分頁本來就是活動限定的）。
KEY = "啤酒節"
# 「一疊」的判準：名字裡有 x 接數字（`2026-啤酒節銅幣 x30 (1)`）。
# ⚠ 沒中的就是硬幣本體，不使用。
STACK_RE = re.compile(r"x\s*(\d+)", re.IGNORECASE)

USE_MS = 3000               # 幾毫秒用一個（使用者指定 3 秒）
SPIN_MS = 500              # 抽轉盤的心跳（要盯「轉完了沒 / 背包變了沒」）
SPIN_SETTLE = 2.0          # 轉完之後等獎品進背包的時間（秒）
SPIN_MAX_WAIT = 30.0       # 一轉最多等這麼久（卡住就停，不無限等）
# 快速抽：送出去之後最多等這麼久等背包有動靜；等不到就當這一發沒生效。
FAST_WAIT = 5.0
CONFIRM_TRIES = 3          # 連續這麼多輪沒動靜 → 停下來說話（不無限重送）
LOG_COLS = ("時間", "描述")
LOG_MAX = 500


def stack_size(name: str) -> int | None:
    """名字裡的「x<數字>」；沒有就回 None（＝硬幣本體，不使用）。"""
    m = STACK_RE.search(name)
    return int(m.group(1)) if m else None


def pick_targets(items, key: str = KEY) -> list:
    """從背包裡挑出「要使用」的東西：名字含 key **而且**有「x<數字>」。

    ⚠ 順手擋掉裝備：關鍵字萬一打太寬，也不會把身上要穿的東西吃掉。
    """
    key = (key or "").strip()
    if not key:
        return []                      # 關鍵字空的就什麼都不做（安全預設）
    return [it for it in items
            if key in it.name and stack_size(it.name) is not None
            and not it.is_gear]


def totals(items) -> dict[int, int]:
    """{種類id: 總數} —— 對帳用（可疊物品會散在好幾格）。"""
    out: dict[int, int] = {}
    for it in items:
        out[it.type_id] = out.get(it.type_id, 0) + it.count
    return out


def gained(before: dict[int, int], after: dict[int, int]) -> list[tuple[int, int]]:
    """背包多了什麼：[(種類id, 多了幾個)]。"""
    return [(tid, n - before.get(tid, 0)) for tid, n in after.items()
            if n > before.get(tid, 0)]


def spent(before: dict[int, int], after: dict[int, int]) -> bool:
    """背包有東西**變少**了嗎 —— 抽一次一定會扣硬幣，這是最硬的「真的抽了」訊號。

    ⚠ 不能只看「多了什麼」：銅轉盤有可能抽到銀幣（同一批幣），也可能槓龜，
      那時只有「少了銅幣」看得出來確實抽了一次。
    """
    return any(after.get(tid, 0) < n for tid, n in before.items())


class EventTab(BaseTab):
    TAB_TITLE = "活動"
    ORDER = 48                       # 排在販賣裝備（47）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        # 用硬幣：送出去等對帳的那一發 (種類id, 名字, 送出前總數, 試了幾輪)
        self._pending = None
        self._used = 0
        # 抽轉盤：(階段, 起始時刻, 抽之前的背包盤點)
        self._spin = None
        self._spins = 0

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        reload_btn = QPushButton("重新整理")
        reload_btn.setToolTip("重新列出目前開著的遊戲分身。")
        reload_btn.clicked.connect(
            lambda: self.reload_instances(force_names=True))
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        # ── 硬幣 ──────────────────────────────────────────
        box = QGroupBox("活動硬幣")
        h = QHBoxLayout(box)
        self.use_btn = QPushButton("▶ 自動使用")
        self.use_btn.setToolTip(
            f"每 {USE_MS // 1000} 秒用掉一個名字有「{KEY}」又有「x數字」的東西。\n"
            "硬幣本體（沒有 x 的那個）不會被動到。")
        self.use_btn.clicked.connect(self._start_use)
        h.addWidget(self.use_btn)
        self.use_stop = QPushButton("⏸ 暫停")
        self.use_stop.setEnabled(False)
        self.use_stop.clicked.connect(lambda: self._stop_use("已暫停"))
        h.addWidget(self.use_stop)
        h.addStretch(1)
        self.use_lbl = QLabel("－")
        h.addWidget(self.use_lbl)
        root.addWidget(box)

        # ── 轉盤 ──────────────────────────────────────────
        box2 = QGroupBox("轉盤")
        h2 = QHBoxLayout(box2)
        self.spin_btn = QPushButton("▶ 自動抽")
        self.spin_btn.setToolTip(
            "一直抽到抽不動（硬幣用完／視窗關掉）為止。\n"
            "⚠ 要先在遊戲裡跟啤酒節使者把要抽的轉盤打開。")
        self.spin_btn.clicked.connect(self._start_spin)
        h2.addWidget(self.spin_btn)
        self.spin_stop = QPushButton("⏸ 暫停")
        self.spin_stop.setEnabled(False)
        self.spin_stop.clicked.connect(lambda: self._stop_spin("已暫停"))
        h2.addWidget(self.spin_stop)
        h2.addSpacing(10)
        # ★ 使用者 2026-08-27：「不想等動畫」。勾著＝自己送 0x164（參數兩個都
        #   從轉盤物件現讀）；不勾＝請遊戲自己按下去、動畫轉完它才送。
        # ⚠ 兩條路**不能混用**（見 roulette.draw 的說明），所以是二選一。
        self.fast_cb = QCheckBox("不等動畫")
        self.fast_cb.setChecked(True)
        self.fast_cb.setToolTip(
            "直接送抽獎封包，不等轉盤動畫轉完（快很多）。\n"
            "取消勾選＝請遊戲自己按下去，跟手點一模一樣但要等動畫。")
        h2.addWidget(self.fast_cb)
        h2.addStretch(1)
        self.spin_lbl = QLabel("－")
        h2.addWidget(self.spin_lbl)
        root.addWidget(box2)

        # ── 歷史紀錄（兩欄：時間、描述）─────────────────────
        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log.setSelectionMode(QAbstractItemView.NoSelection)
        self.log.verticalHeader().setVisible(False)
        hh = self.log.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.log, 1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._use_timer = QTimer(self)
        self._use_timer.timeout.connect(self._use_tick)
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._spin_tick)
        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self._refresh_labels)
        self._state_timer.start(1000)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        self._stop_use(quiet=True)
        self._stop_spin(quiet=True)
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
                locate.warm(sc)              # 改版位移自動校正（只做一次）
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
        self._refresh_labels()

    def _on_who_changed(self) -> None:
        # ⚠ 換分身一定要停 —— 不然會對「新選的那台」繼續做下去。
        self._stop_use("換了分身，自動使用已停止", quiet=True)
        self._stop_spin("換了分身，自動抽已停止", quiet=True)
        self._refresh_labels()

    def _cur(self):
        pid = self.who.currentData()
        if pid is None:
            return None, None
        return int(pid), self._scanners.get(int(pid))

    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()` —— 同一個遊戲行程只能有一份跳板，
          自己 new 一個會把掛機分頁那份拆掉。
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

    def _scan(self):
        """(符合條件的, 整袋讀完整了嗎, 全部物品)；讀不到回 (None, False, [])。

        ⚠ 一定要看 `complete`：`bag.items()` 讀不到容器時回的空清單，跟「真的
          一件都沒有」長得一模一樣 —— 當成「用完了」就是安靜地做錯事
          （[[bag-false-empty-guards]] 的同一個坑）。
        """
        _pid, sc = self._cur()
        if sc is None:
            return None, False, []
        try:
            items, complete = bag.scan(sc)
        except Exception:                                # noqa: BLE001
            return None, False, []
        return pick_targets(items), complete, items

    def _log(self, text: str) -> None:
        # ⚠ 追加式紀錄表一律**插一列**，不要整表重畫（[[qt-ui-pitfalls]]）。
        self.log.insertRow(0)
        self.log.setItem(0, 0, QTableWidgetItem(time.strftime("%H:%M:%S")))
        self.log.setItem(0, 1, QTableWidgetItem(text))
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)

    def _refresh_labels(self) -> None:
        """兩個功能的現況（純讀）。切到別頁就不做。"""
        if not self.isVisible():
            return
        hits, complete, _all = self._scan()
        if hits is None:
            self.use_lbl.setText("讀不到背包")
        else:
            n = sum(it.count for it in hits)
            self.use_lbl.setText(
                f"可用 {n} 個" + ("" if complete else "（背包還在同步）"))
        _pid, sc = self._cur()
        st = roulette.state(sc) if sc is not None else None
        if st is None:
            self.spin_lbl.setText("讀不到轉盤")
        elif not st.open:
            self.spin_lbl.setText("轉盤沒開")
        else:
            self.spin_lbl.setText(
                f"轉盤 {st.kind}" + ("（轉動中）" if st.spinning else "（就緒）"))

    # -- 硬幣 ----------------------------------------------------------
    def _start_use(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("⚠ 先選一台分身")
            return
        if self._mover(pid) is None:
            return                       # 原因 _mover 已經寫在狀態列上
        self._pending = None
        self.use_btn.setEnabled(False)
        self.use_stop.setEnabled(True)
        self.who.setEnabled(False)
        self._use_timer.start(USE_MS)
        self.status.setText(f"▶ 開始使用活動硬幣（每 {USE_MS // 1000} 秒一個）…")
        self._use_tick()                 # 不要空等第一拍

    def _stop_use(self, msg: str = "", quiet: bool = False) -> None:
        was = self._use_timer.isActive()
        self._use_timer.stop()
        self._pending = None
        self.use_btn.setEnabled(True)
        self.use_stop.setEnabled(False)
        self.who.setEnabled(not self._spin_timer.isActive())
        if msg and (was or not quiet):
            self.status.setText(msg)

    def _use_tick(self) -> None:
        """一輪：先對上一發的帳，再挑一個用掉。

        ⚠ 第一件事就是問「還在跑嗎」：**暫停就是暫停**，不管是誰叫到這裡都
          不准再送出任何東西（計時器停掉之後仍在佇列裡的事件、或之後有人
          手動呼叫）。少了這道護欄，停下來之後還會再多用掉一個。
        """
        if not self._use_timer.isActive():
            return
        pid, sc = self._cur()
        if sc is None:
            self._stop_use("⚠ 分身不見了，自動使用已停止")
            return
        hits, complete, all_items = self._scan()
        if hits is None:
            self.status.setText("⚠ 這一拍讀不到背包，下一輪再試")
            return

        if self._pending is not None:            # ① 對上一發的帳
            tid, name, before, tries = self._pending
            now = totals(all_items).get(tid, 0)
            if now < before:
                self._pending = None
                self._used += 1
                self._log(f"用了「{name}」（還剩 {now} 個）")
            elif not complete:
                return                           # 沒同步完，這一拍不算數
            else:
                tries += 1
                if tries >= CONFIRM_TRIES:
                    # ⛔ 不准無限重送：真的用掉卻誤判的話，每重送一次就多用一個。
                    self._stop_use(
                        f"⚠ 送了「{name}」{tries} 輪，背包數量都沒變少 → 已暫停"
                        "（這個東西不能直接使用？還是有視窗擋著？）")
                    return
                self._pending = (tid, name, before, tries)
                return

        if not hits:                             # ② 挑下一個
            if not complete:
                return                           # 沒同步完，別急著說用完了
            self._stop_use(f"✔ 都用完了（這次總共 {self._used} 個）")
            return
        want = hits[0]
        # ★★ 格號**當場重讀**：bag 給的 slot 是陣列索引，不是物品自己記的
        #    格號（+0x25）。拿索引打封包會用到別的東西（見檔頭）。
        h = bag.head(sc)
        got = inventory.find_by_type(sc, h[0], want.type_id) if h else None
        if got is None:
            self.status.setText(f"⚠ 這一拍找不到「{want.name}」的格號，下一輪再試")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        before = totals(all_items).get(want.type_id, 0)
        if not recall.use_item(mv, got[0]):
            self.status.setText("⚠ 送不出去（指令槽忙？），下一輪再試")
            return
        self._pending = (want.type_id, want.name, before, 0)

    # -- 轉盤 ----------------------------------------------------------
    def _start_spin(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("⚠ 先選一台分身")
            return
        st = roulette.state(sc)
        if st is None:
            self.status.setText("⚠ 讀不到轉盤（官方改寫了？）—— 不動作")
            return
        if not st.open:
            self.status.setText(
                "⚠ 轉盤視窗沒開 —— 先在遊戲裡跟啤酒節使者選好要抽哪個轉盤")
            return
        if self._mover(pid) is None:
            return
        self._spin = None
        self.spin_btn.setEnabled(False)
        self.spin_stop.setEnabled(True)
        self.who.setEnabled(False)
        self._spin_timer.start(SPIN_MS)
        self.status.setText("▶ 開始抽轉盤…")
        self._spin_tick()

    def _stop_spin(self, msg: str = "", quiet: bool = False) -> None:
        was = self._spin_timer.isActive()
        self._spin_timer.stop()
        self._spin = None
        self.spin_btn.setEnabled(True)
        self.spin_stop.setEnabled(False)
        self.who.setEnabled(not self._use_timer.isActive())
        if msg and (was or not quiet):
            self.status.setText(msg)

    def _spin_tick(self) -> None:
        """一轉的狀態機：叫下去 → 等它開始轉 → 等轉完 → 對背包的帳。

        ⚠ 同 `_use_tick`：暫停之後一律不再送（抽一次就是花一次錢）。
        """
        if not self._spin_timer.isActive():
            return
        pid, sc = self._cur()
        if sc is None:
            self._stop_spin("⚠ 分身不見了，自動抽已停止")
            return
        st = roulette.state(sc)
        if st is None:
            self._stop_spin("⚠ 讀不到轉盤狀態 → 已暫停")
            return
        if not st.open:
            self._stop_spin(f"✔ 轉盤關掉了，收工（這次抽了 {self._spins} 次）")
            return

        fast = self.fast_cb.isChecked()
        if self._spin is None:                   # ① 叫下去
            if st.spinning:
                return                           # 遊戲自己還在轉，等它
            items, complete = bag.scan(sc)
            if not complete:
                return                           # 背包沒同步完 → 對不了帳，等
            mv = self._mover(pid)
            if mv is None:
                return
            ok, why = (roulette.draw if fast else roulette.spin)(mv, sc)
            if not ok:
                self._stop_spin(f"⚠ {why} → 已暫停")
                return
            self._spin = ("fast" if fast else "start",
                          time.monotonic(), totals(items))
            return

        stage, t0, before = self._spin
        waited = time.monotonic() - t0
        if stage == "fast":                      # 快速抽：直接等背包有動靜
            items, complete = bag.scan(sc)
            if complete:
                after = totals(items)
                if spent(before, after) or gained(before, after):
                    self._finish_spin(before, after)
                    return
            if waited > FAST_WAIT:
                # ⛔ 不無限重送：等不到就停手，不然可能一直丟包給伺服器。
                self._stop_spin(
                    "⚠ 送出去之後背包沒有任何變化 → 已暫停"
                    "（伺服器沒收這一包？硬幣不夠？還是轉盤視窗被關了？）")
            return
        if stage == "start":                     # ② 等它真的開始轉
            if st.spinning:
                self._spin = ("run", time.monotonic(), before)
            elif waited > SPIN_MAX_WAIT:
                self._stop_spin("⚠ 叫下去了但轉盤沒動 → 已暫停"
                                "（硬幣不夠？還是視窗被關掉了？）")
            return
        if stage == "run":                       # ③ 等轉完
            if not st.spinning:
                self._spin = ("settle", time.monotonic(), before)
            elif waited > SPIN_MAX_WAIT:
                self._stop_spin("⚠ 轉太久沒停 → 已暫停")
            return
        # ④ 轉完了，等獎品進背包再對帳
        if waited < SPIN_SETTLE:
            return
        items, complete = bag.scan(sc)
        if not complete:
            if waited > SPIN_MAX_WAIT:
                self._stop_spin("⚠ 轉完了但背包一直讀不完整 → 已暫停")
            return
        self._finish_spin(before, totals(items))

    def _finish_spin(self, before: dict[int, int], after: dict[int, int]) -> None:
        """一轉收尾：把「抽到什麼」記進歷史。"""
        from app.game import itemname
        self._spins += 1
        self._spin = None
        got = gained(before, after)
        if got:
            self._log("轉盤抽到：" + "、".join(
                f"{itemname.of(t) or f'物品 {t}'}×{n}" for t, n in got))
        else:
            # 抽了卻什麼都沒多（槓龜，或獎品晚到）—— 照實記，不編故事。
            self._log("轉盤抽了一次（沒看到背包變多）")

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._use_timer.stop()
        self._spin_timer.stop()
        self._state_timer.stop()
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
