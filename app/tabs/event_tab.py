"""活動分頁：**只放活動期間才有意義的東西**。

★★★ 活動結束 ⇒ **整個檔刪掉就好**（主視窗是掃 `app/tabs/` 自動掛分頁的，
  刪檔＝分頁消失，其他分頁一行都不用動）。跟 `app/game/eventmap.py` 的
  `ROUTES` 一樣的設計：活動的東西集中在一個地方，收攤很乾淨。

## 功能①：自動使用活動硬幣（2026-08-27 使用者要求）

掃背包，名字**同時**符合這兩條的才使用：

    · 含關鍵字（預設「啤酒節」，可以自己改）
    · 而且名字裡有「x<數字>」 ← 一疊的禮盒／袋裝

沒有「x<數字>」的是**硬幣本體**（例：`2026-啤酒節銅幣`）—— **絕對不使用**。
每 3 秒用一個，配一顆暫停鈕。

⚠⚠ **為什麼要關鍵字這道閘門**：只認「x<數字>」的話，背包裡任何名字帶
  「x50」的東西都會被吃掉（安靜地做錯事）。關鍵字做成輸入框而不是寫死，
  下次換別的活動不必改程式。

## ⛔ 不准寫死物品編號 —— 這次改版就是活教材

2026-08-27 官方**在活動中途換掉銅幣的編號**：`79912` 那組整批改名成
「2026-啤酒節銅幣**(舊)**」，新的銅幣變成 `87395`（x10/x30/x50/x150 =
87396~87402）。任何寫死 79912 的東西當天就默默失效了。
**認名字不認編號**，改版自動跟上。

## 怎麼「使用」

就是**使用背包物品**（封包代號 `0x2E`），`recall.use_item(mover, 格號)` ——
天使之翼一路都在用、早就實機驗過，這裡完全沒有新封包。

⚠⚠ **格號一定要現讀**（CLAUDE.md 鐵則：交給遊戲的背包格號送出前當場重讀重驗）。
  `bag.Item.slot` 是**陣列索引**，不是物品自己記的格號（`+0x25`）——
  `inventory.locate()` 的表頭實測偏過 6 格，拿索引去打封包就會**用到別的東西**。
  所以送出前一律走 `inventory.find_by_type()`（它讀的是物品自己的 +0x25）。

## 送出去 ≠ 成功

每用一個都要對帳：記下「那個種類在背包裡的總數」，下一輪重數，**變少了才算用掉**。
連著幾輪都沒變少就停下來大聲說 —— ⛔ 不准無限重送（重送一次就可能多用掉一個）。
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import bag, inventory, locate, move, recall
from app.tabs.base_tab import BaseTab, no_elide

# 預設關鍵字：只認名字含這個字串的東西（可在介面上改）。
DEFAULT_KEY = "啤酒節"
# 「一疊」的判準：名字裡有 x 接數字（`2026-啤酒節銅幣 x30 (1)`）。
# ⚠ 大小寫都收；⚠ 沒中的就是硬幣本體，不使用。
STACK_RE = re.compile(r"x\s*(\d+)", re.IGNORECASE)

USE_MS = 3000               # 幾毫秒用一個（使用者指定 3 秒）
REFRESH_MS = 1000           # 「現在符合條件的」清單多久重畫一次（純讀）
# 送出後連續這麼多輪總數都沒變少 → 判定沒生效，停下來說話（不無限重送）。
CONFIRM_TRIES = 3
LOG_COLS = ("時間", "用掉的東西", "這個種類還剩")
LOG_MAX = 300


def stack_size(name: str) -> int | None:
    """名字裡的「x<數字>」；沒有就回 None（＝硬幣本體，不使用）。"""
    m = STACK_RE.search(name)
    return int(m.group(1)) if m else None


def pick_targets(items, key: str) -> list:
    """從背包裡挑出「要使用」的東西。

    規則（使用者 2026-08-27 定）：名字含 `key` **而且**有「x<數字>」。
    ⚠ 順手擋掉裝備：關鍵字打太寬時不會把身上要穿的東西吃掉。
    """
    key = (key or "").strip()
    if not key:
        return []                      # 關鍵字空的就什麼都不做（安全預設）
    return [it for it in items
            if key in it.name and stack_size(it.name) is not None
            and not it.is_gear]


class EventTab(BaseTab):
    TAB_TITLE = "活動"
    ORDER = 48                       # 排在販賣裝備（47）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._running = False
        # 送出去等對帳的那一發：(種類id, 名字, 送出前這個種類的總數, 試了幾輪)
        self._pending = None
        self._used = 0                # 這次開程式以來總共用掉幾個

        root = QVBoxLayout(self)

        hint = QLabel(
            "活動限定的功能都放這裡，活動結束整頁會拿掉。"
            "「自動使用活動硬幣」只會用**成疊的**（名字有 x10、x30 那種），"
            "硬幣本體不會被動到。")
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
        reload_btn.clicked.connect(
            lambda: self.reload_instances(force_names=True))
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        # ── 自動使用活動硬幣 ───────────────────────────────
        box = QGroupBox("自動使用活動硬幣")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("名字要含"))
        self.key = QLineEdit(DEFAULT_KEY)
        self.key.setFixedWidth(120)
        self.key.setToolTip(
            "只使用名字含這幾個字、而且有「x數字」的東西。\n"
            "換別的活動時改這裡就好，不必改程式。")
        self.key.textChanged.connect(self._refresh_targets)
        row.addWidget(self.key)
        rule = QLabel("　＋名字要有「x數字」（x10 / x30…）才用；"
                      "沒有 x 的是硬幣本體，不會動到它")
        rule.setWordWrap(True)
        row.addWidget(rule, 1)
        v.addLayout(row)

        act = QHBoxLayout()
        self.go_btn = QPushButton("▶ 開始自動使用")
        self.go_btn.setToolTip(f"每 {USE_MS // 1000} 秒用掉一個，用完自動停。")
        self.go_btn.clicked.connect(self._start)
        act.addWidget(self.go_btn)
        self.stop_btn = QPushButton("⏸ 暫停")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self._stop("已暫停"))
        act.addWidget(self.stop_btn)
        act.addStretch(1)
        self.count_lbl = QLabel("符合條件：－")
        self.count_lbl.setStyleSheet("font-weight: bold;")
        act.addWidget(self.count_lbl)
        v.addLayout(act)

        self.targets = QListWidget()
        self.targets.setSelectionMode(QAbstractItemView.NoSelection)
        self.targets.setFixedHeight(110)
        self.targets.setToolTip("按下去之後會被用掉的就是這些（由上往下用）。")
        no_elide(self.targets)
        v.addWidget(self.targets)
        root.addWidget(box)

        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log.setSelectionMode(QAbstractItemView.NoSelection)
        self.log.verticalHeader().setVisible(False)
        root.addWidget(self.log, 1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # 兩個計時器：慢的只重畫清單（純讀），快的才真的送封包。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_targets)
        self._timer.start(REFRESH_MS)
        self._use_timer = QTimer(self)
        self._use_timer.timeout.connect(self._use_tick)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        self._stop("換了分身清單，自動使用已停止", quiet=not self._running)
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
        self._refresh_targets()

    def _on_who_changed(self) -> None:
        # ⚠ 換分身一定要停 —— 不然會對「新選的那台」繼續用下去。
        self._stop("換了分身，自動使用已停止", quiet=not self._running)
        self._refresh_targets()

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

    # ------------------------------------------------------------------
    def _scan(self):
        """(符合條件的清單, 整袋讀完整了嗎, 全部物品)。讀不到回 (None, False, [])。

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
        return pick_targets(items, self.key.text()), complete, items

    def _refresh_targets(self) -> None:
        """重畫「現在符合條件的」清單（純讀，切到別頁就不做）。"""
        if not self.isVisible():
            return
        hits, complete, _all = self._scan()
        self.targets.clear()
        if hits is None:
            self.count_lbl.setText("符合條件：讀不到")
            return
        for it in hits:
            self.targets.addItem(f"第 {it.slot} 格　{it.name}　×{it.count}")
        total = sum(it.count for it in hits)
        self.count_lbl.setText(
            f"符合條件：{len(hits)} 格／共 {total} 個"
            + ("" if complete else "（⚠ 背包還沒同步完，數字可能偏少）"))

    # ------------------------------------------------------------------
    def _start(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("⚠ 先選一台分身")
            return
        if not self.key.text().strip():
            self.status.setText("⚠ 關鍵字是空的 —— 那樣會分不出要用哪些，不動作")
            return
        if self._mover(pid) is None:
            return                       # 原因 _mover 已經寫在狀態列上
        self._running = True
        self._pending = None
        self.go_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.key.setEnabled(False)       # 跑起來之後不准改規則（會用到別的東西）
        self.who.setEnabled(False)
        self._use_timer.start(USE_MS)
        self.status.setText(f"▶ 開始：每 {USE_MS // 1000} 秒用一個…")
        self._use_tick()                 # 不要空等第一拍

    def _stop(self, msg: str = "", quiet: bool = False) -> None:
        was = self._running
        self._running = False
        self._pending = None
        self._use_timer.stop()
        self.go_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.key.setEnabled(True)
        self.who.setEnabled(True)
        if msg and (was or not quiet):
            self.status.setText(msg)

    # ------------------------------------------------------------------
    def _use_tick(self) -> None:
        """一輪：先對上一發的帳，再挑一個用掉。"""
        if not self._running:
            return
        pid, sc = self._cur()
        if sc is None:
            self._stop("⚠ 分身不見了，自動使用已停止")
            return
        hits, complete, all_items = self._scan()
        if hits is None:
            self.status.setText("⚠ 這一拍讀不到背包，下一輪再試")
            return

        # ① 對上一發的帳：那個種類的總數有沒有變少
        if self._pending is not None:
            tid, name, before, tries = self._pending
            now = sum(it.count for it in all_items if it.type_id == tid)
            if now < before:
                self._pending = None
                self._used += 1
                self._log(name, now)
            elif not complete:
                return                   # 背包沒同步完，這一拍不算數（別誤判）
            else:
                tries += 1
                if tries >= CONFIRM_TRIES:
                    # ⛔ 不准無限重送：真的有用掉卻誤判的話，每重送一次就多用一個。
                    self._stop(
                        f"⚠ 送了「{name}」{tries} 輪，背包數量都沒變少 → 已暫停。"
                        "（是不是這個東西不能直接使用？或是視窗擋著？）")
                    return
                self._pending = (tid, name, before, tries)
                self.status.setText(
                    f"…等「{name}」的結果（第 {tries}/{CONFIRM_TRIES} 輪）")
                return

        # ② 挑下一個來用
        if not hits:
            if not complete:
                self.status.setText("背包還沒同步完，等一下再看…")
                return
            self._stop(f"✔ 都用完了（這次總共用掉 {self._used} 個）")
            return

        want = hits[0]
        # ★★ 格號**當場重讀**：bag 給的 slot 是陣列索引，不是物品自己記的格號
        #    （+0x25）。拿索引打封包會用到別的東西（見檔頭）。
        h = bag.head(sc)
        got = inventory.find_by_type(sc, h[0], want.type_id) if h else None
        if got is None:
            self.status.setText(f"⚠ 這一拍找不到「{want.name}」的格號，下一輪再試")
            return
        slot, _obj, _cnt = got
        mv = self._mover(pid)
        if mv is None:
            return
        before = sum(it.count for it in all_items if it.type_id == want.type_id)
        if not recall.use_item(mv, slot):
            self.status.setText("⚠ 送不出去（指令槽忙？），下一輪再試")
            return
        self._pending = (want.type_id, want.name, before, 0)
        self.status.setText(f"用掉一個「{want.name}」（第 {slot} 格），確認中…")

    def _log(self, name: str, left: int) -> None:
        # ⚠ 追加式紀錄表一律**插一列**，不要整表重畫（[[qt-ui-pitfalls]]）。
        self.log.insertRow(0)
        for col, text in enumerate((time.strftime("%H:%M:%S"), name, str(left))):
            self.log.setItem(0, col, QTableWidgetItem(text))
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)
        self.status.setText(f"✔ 已用掉 {self._used} 個（最後：{name}）")

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._use_timer.stop()
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
