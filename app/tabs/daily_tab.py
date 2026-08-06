"""領取每日分頁。

一顆按鈕「領取在線獎勵」：**不必選分身**，按下去把目前開著的每一台
都領一輪（6 格全送，跟你在遊戲裡把每格的「領取」都點一遍一模一樣）。

背後是 `app/game/dailygift.py`：呼叫遊戲自己的泛用送包函式
`0x5D3D97(0x48, 獎勵編號)`，加解密與送出都由客戶端處理。

為什麼不用選分身
----------------
使用者指定：「不需要分角色，按下去直接領取全部角色的獎勵」。
領獎沒有互斥問題（每台各領各的），跟能量晶化那種「按一下要指名對誰」不同。

節奏
----
用 QTimer 一拍送一包（不開執行緒 —— 打包 exe 時背景 QThread 沒無頭防護
會原生當機，見 memory 的 packaging-and-release）。一拍一包也讓 GUI
全程不卡：指令槽塞住時 call_sync 最長等 1 秒，全部擠在一次點擊裡做的話
最壞會凍住幾十秒。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
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
from app.game import dailygift, move
from app.tabs.base_tab import BaseTab

TICK_MS = 200             # 一拍送一包：6 格 × 5 台 = 30 包 ≈ 6 秒，不急
LOG_COLS = ("時間", "分身", "結果")
LOG_MAX = 200


class DailyTab(BaseTab):
    TAB_TITLE = "領取每日"
    ORDER = 46                       # 排在能量晶化（45）與販賣裝備（47）之間

    def build_ui(self) -> None:
        self._movers: dict[int, move.Mover] = {}
        self._queue: list[tuple[int, str, int]] = []   # (pid, 顯示名, 獎勵編號)
        self._ok: dict[int, int] = {}                  # pid -> 送成功幾包
        self._fail: dict[int, int] = {}
        self._label: dict[int, str] = {}

        root = QVBoxLayout(self)
        hint = QLabel(
            "把目前開著的每一台分身都領一輪在線獎勵（6 格全送）——"
            "跟你在遊戲的「活動總覽 → 在線獎勵」把每格的「領取」都點一遍"
            "送出的是同一個封包。還沒到時間或已領過的格子，伺服器會自己忽略，"
            "多送沒有影響。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        self.claim_btn = QPushButton("領取在線獎勵")
        self.claim_btn.setToolTip(
            "對每一台開著的分身送出 6 格的領取（在線 0~60 分鐘那六格）。\n"
            "不必選分身、不會動到鍵盤滑鼠，遊戲視窗在背景也照領。")
        self.claim_btn.clicked.connect(self._start)
        bar.addWidget(self.claim_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        # 每台一列的結果（時間／分身／結果），才看得出誰領了、誰沒領到
        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log.setSelectionMode(QTableWidget.NoSelection)
        self.log.verticalHeader().setVisible(False)
        self.log.setAlternatingRowColors(True)
        hh = self.log.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        root.addWidget(self.log)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    def _start(self) -> None:
        clients: list[tuple[int, str]] = []
        seen: set[int] = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            acc = charname.account_from_title(w.title)
            # ⚠ 只用預讀快取拿角色名（不帶 scanner 絕不掃描）；沒有就顯示帳號。
            nm = preload.name_of(w.pid)
            clients.append((w.pid, f"{nm}（{acc}）" if nm else acc))
        if not clients:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            return
        self._queue = [(pid, label, rid)
                       for pid, label in clients
                       for rid in dailygift.REWARD_IDS]
        self._ok = {pid: 0 for pid, _ in clients}
        self._fail = {pid: 0 for pid, _ in clients}
        self._label = dict(clients)
        self.claim_btn.setEnabled(False)
        self.status.setText(f"開始：{len(clients)} 台 × "
                            f"{len(dailygift.REWARD_IDS)} 格")
        self._timer.start(TICK_MS)
        self._tick()                        # 立刻送第一包，不空等一拍

    def _tick(self) -> None:
        if not self._queue:
            self._finish()
            return
        pid, label, rid = self._queue.pop(0)
        mv = self._mover(pid)
        if mv is None:
            # 跳板裝不上（多半是那台剛關掉）—— 這台剩下的格子全跳過，
            # 不然會連跳 6 拍、每拍再失敗一次。
            self._queue = [q for q in self._queue if q[0] != pid]
            self._fail[pid] += 1
            self._done_client(pid, "⚠ 無法安裝跳板（視窗關了？）")
            return
        if dailygift.claim(mv, rid):
            self._ok[pid] += 1
        else:
            self._fail[pid] += 1
        left = sum(1 for q in self._queue if q[0] == pid)
        if left == 0:
            n = len(dailygift.REWARD_IDS)
            self._done_client(
                pid,
                f"已送出 {self._ok[pid]}/{n} 格"
                + ("" if not self._fail[pid]
                   else f"（{self._fail[pid]} 格排不進指令槽，可再按一次）"))
        total = len(self._ok) * len(dailygift.REWARD_IDS)
        sent = total - len(self._queue)
        self.status.setText(f"領取中… {sent}/{total}　目前：{label}")

    def _done_client(self, pid: int, text: str) -> None:
        row = (time.strftime("%H:%M:%S"),
               self._label.get(pid, str(pid)), text)
        self.log.insertRow(0)
        for c, cell in enumerate(row):
            self.log.setItem(0, c, QTableWidgetItem(cell))
        # 只留最近 LOG_MAX 列，放著跑一整晚也不會吃掉記憶體
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)

    def _finish(self) -> None:
        self._timer.stop()
        self.claim_btn.setEnabled(True)
        ok = sum(self._ok.values())
        fail = sum(self._fail.values())
        self.status.setText(
            f"完成：{len(self._ok)} 台，共送出 {ok} 包"
            + (f"，{fail} 包失敗（看上面哪台，再按一次就好）" if fail else ""))

    # ------------------------------------------------------------------
    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，**不要自己 new 一個 Mover** ——
          同一個遊戲行程只能有一份跳板，自己裝會把掛機分頁那份拆掉
          （見 move.acquire 的說明與 energy_tab 同名方法的教訓）。
        ⚠ 失敗不要記進 _movers：記了空殼之後這台就永遠裝不上了。
        """
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception:                               # noqa: BLE001
            self._movers.pop(pid, None)
            return None
        self._movers[pid] = mv
        return mv

    def on_close(self) -> None:
        self._timer.stop()
        # ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
        #   掛機分頁可能還在用（見 move.acquire）。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                           # noqa: BLE001
                pass
        self._movers.clear()
