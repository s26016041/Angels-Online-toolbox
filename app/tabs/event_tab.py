"""活動分頁：**只放活動期間才有意義的東西**。

★★★ 活動結束 ⇒ **刪掉這個檔就好**（主視窗是掃 `app/tabs/` 自動掛分頁的），
  其他分頁一行都不用動。

⛔ 啤酒節「自動使用硬幣」「自動抽轉盤」2026-09-23 活動結束，使用者要求刪除，已刪
  （`app/game/roulette.py` 一起刪；它借給 talkwnd 的讀映像小工具已搬過去）。
  當時的做法與坑記在 memory `event-roulette-and-coins`。

## 自動烤肉：`0xA`、`0xB` 輪流送 talkaction

2026-09-23 使用者擷取兩次：呼叫鏈 `0x599074`（Lua `game.talkaction`，C 函式
0x59904F）→ `0x5D9504`＝`sell.TALK_FN`（代號 0x0B、內文 3）。**參數看 `0x599074`
那一行**（injector.Packet：那列的參數＝被呼叫那支自己的參數）＝ `0xA`／`0xB`
＝對話第 1／第 2 項（同 `supply.talk_option`）。⛔ 第一版誤讀成 `0x5D952D` 那行的 2，
送了伺服器不理。**零新位址**。

✅ 實機（雪狐，看「生鮮棒棒腿」少 10 才算）：`0xA` 成功；**緊接再送 `0xA` 沒反應**；
`0xB` 自己不扣、但 `0xB` 後接 `0xA` 每次都扣。＝烤完停在結果頁，`0xB`（繼續烤肉）
回選單、`0xA` 才烤。所以每拍一包、`0xA`→`0xB` 輪流，從 `0xA` 開始：停在結果頁時
那包 `0xA` 被忽略、下一包 `0xB` 自己把它帶回來。
⚠ 未知：選單頁第 2 項是什麼（棒棒腿用完、`0xA` 沒烤成時 `0xB` 會按在選單頁上）。

★ 送包在背景執行緒：`call_sync` 每包最多卡 `sell.CALL_TIMEOUT`，間隔又能設到
  幾十毫秒，放在 UI 緒會把整個工具拖慢（[[no-crash-no-lag]]）。
★ 出口只有暫停鈕、換分身、分身消失（跳板失效）；送不出去只記次數不停。
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import bag, locate, move, sell
from app.tabs.base_tab import GROUP_CHORES, BaseTab

BBQ_CODES = (0xA, 0xB)      # 烤 → 繼續烤肉（對話第 1／2 項），輪流送；見檔頭
RAW_NAME = "生鮮棒棒腿"      # 烤一次扣 10 —— 成功與否只認它有沒有少
COUNT_EVERY = 1.0           # 幾秒盤點一次生鮮棒棒腿（背景緒做，不佔 UI）
MS_MIN, MS_MAX = 10, 60000  # 間隔可調範圍（毫秒）
MS_DEFAULT = 500
CFG_MS = "event.bbq_ms"


class BbqWorker:
    """背景送包迴圈。`sent`／`failed` 給 UI 讀（只有這條緒寫）。"""

    def __init__(self, mover, interval_ms: int, send=None,
                 scanner=None) -> None:
        self.mover = mover
        self.scanner = scanner
        self.interval_ms = max(MS_MIN, int(interval_ms))
        self._send = send or (lambda mv, code: sell.talk(mv, code))
        self.raw: int | None = None  # 生鮮棒棒腿剩幾個（None＝還沒讀到／讀不完整）
        self._counted = 0.0
        self._stop = threading.Event()
        self.sent = 0
        self.failed = 0
        self.dead = False            # 跳板失效（分身關了）→ 自己結束
        self._th = threading.Thread(target=self._run, daemon=True,
                                    name="event-bbq")

    def start(self) -> None:
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return self._th.is_alive()

    def _count(self) -> None:
        """⚠ 背包沒讀完整就不更新（讀不到≠沒有，[[bag-false-empty-guards]]）。"""
        if self.scanner is None:
            return
        try:
            items, complete = bag.scan(self.scanner)
        except Exception:                                # noqa: BLE001
            return
        if complete:
            self.raw = sum(it.count for it in items if it.name == RAW_NAME)

    def _run(self) -> None:
        n = 0
        while not self._stop.is_set():
            if time.monotonic() - self._counted >= COUNT_EVERY:
                self._counted = time.monotonic()
                self._count()
            t0 = time.monotonic()
            if not (self.mover and self.mover.active) or not sell.TALK_FN:
                self.dead = True
                return
            try:
                ok = self._send(self.mover, BBQ_CODES[n % len(BBQ_CODES)])
            except Exception:                            # noqa: BLE001
                ok = False
            if ok:
                self.sent += 1
            else:
                self.failed += 1
            n += 1
            left = self.interval_ms / 1000 - (time.monotonic() - t0)
            if left > 0:
                self._stop.wait(left)


class EventTab(BaseTab):
    TAB_TITLE = "活動"
    GROUP = GROUP_CHORES
    ORDER = 48                       # 排在販賣裝備（47）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._bbq: BbqWorker | None = None

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

        box = QGroupBox("自動烤肉")
        h = QHBoxLayout(box)
        h.addWidget(QLabel("每"))
        self.ms = QSpinBox()
        self.ms.setRange(MS_MIN, MS_MAX)
        self.ms.setSingleStep(50)
        self.ms.setSuffix(" ms")
        try:
            self.ms.setValue(int(config.get(CFG_MS, MS_DEFAULT)))
        except Exception:                                # noqa: BLE001
            self.ms.setValue(MS_DEFAULT)
        self.ms.valueChanged.connect(self._on_ms_changed)
        h.addWidget(self.ms)
        h.addWidget(QLabel("送一包（烤／繼續烤肉輪流）"))
        self.bbq_btn = QPushButton("▶ 開始")
        self.bbq_btn.setToolTip("一直送烤肉封包，直到按暫停。")
        self.bbq_btn.clicked.connect(self._start_bbq)
        h.addWidget(self.bbq_btn)
        self.bbq_stop = QPushButton("⏸ 暫停")
        self.bbq_stop.setEnabled(False)
        self.bbq_stop.clicked.connect(lambda: self._stop_bbq("已暫停"))
        h.addWidget(self.bbq_stop)
        h.addStretch(1)
        self.bbq_lbl = QLabel("－")
        h.addWidget(self.bbq_lbl)
        root.addWidget(box)
        root.addStretch(1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_label)
        self._ui_timer.start(300)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        self._stop_bbq(quiet=True)
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

    def _on_who_changed(self) -> None:
        # ⚠ 換分身一定要停 —— 不然會對「新選的那台」繼續做下去。
        self._stop_bbq("換了分身，自動烤肉已停止")

    def _on_ms_changed(self, v: int) -> None:
        if self._bbq is not None:
            self._bbq.interval_ms = max(MS_MIN, int(v))
        config.set(CFG_MS, int(v))
        config.save()

    def _cur_pid(self) -> int | None:
        pid = self.who.currentData()
        return int(pid) if pid is not None else None

    def _mover(self, pid: int) -> move.Mover | None:
        """⚠⚠ 一定要走 `move.acquire()` —— 同一個遊戲行程只能有一份跳板。"""
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

    # -- 烤肉 ----------------------------------------------------------
    def _start_bbq(self) -> None:
        pid = self._cur_pid()
        if pid is None:
            self.status.setText("先選分身")
            return
        if not sell.TALK_FN:
            self.status.setText("⚠ 對話動作函式定位失敗（改版？）—— 功能停用")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        self._stop_bbq(quiet=True)
        self._bbq = BbqWorker(mv, self.ms.value(),
                              scanner=self._scanners.get(pid))
        self._bbq.start()
        self.bbq_btn.setEnabled(False)
        self.bbq_stop.setEnabled(True)
        self.status.setText(f"▶ 自動烤肉中（每 {self.ms.value()} ms 一包）")

    def _stop_bbq(self, why: str = "", quiet: bool = False) -> None:
        if self._bbq is not None:
            self._bbq.stop()
            self._bbq = None
        self.bbq_btn.setEnabled(True)
        self.bbq_stop.setEnabled(False)
        if why and not quiet:
            self.status.setText(why)

    def _refresh_label(self) -> None:
        w = self._bbq
        if w is None:
            return
        raw = "?" if w.raw is None else w.raw
        self.bbq_lbl.setText(f"{RAW_NAME} {raw}｜已送 {w.sent}" +
                             (f"／失敗 {w.failed}" if w.failed else ""))
        if w.dead:
            self._stop_bbq("⚠ 分身不見了（跳板失效），自動烤肉已停止")
