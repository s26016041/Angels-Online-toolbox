"""監控技能經驗球分頁（產品主畫面）。

即時監控每個天使之戀分身正在練的技能經驗球：
  1. 按「開始監控」→ 自動找出所有分身。
  2. 背景執行緒「每一輪都重新用 AOB 特徵掃描」每台分身，讀出所有技能經驗球候選，
     比對前後兩輪、挑出「正在增加」的那顆來跟隨，顯示即時值。
     ★ 不快取位址 ★——換地圖 / 重連 / 升級會讓遊戲把技能結構搬到別的位址（舊位址
     的值會「跑掉」或凍住），因為每輪都重掃特徵，新位址下一輪就會出現、自動跟上，
     效果等同「手動按停止再開始」，但是自動且持續的。
  3. 若跟隨的球「持續 5 分鐘沒有再增加」（期間重掃也沒有任何在動的球），或「突然
     掃不到特徵」（很可能是斷線 / 遊戲關了，也照樣併入這 5 分鐘倒數）→ 判定經驗球
     滿了 / 遊戲停止/斷線 → 自動停該台、循環發警報聲、跳警告視窗標明帳號；警報聲
     持續到按「停止警報」為止。一定要先偵測到一次增加，才會開始算這 5 分鐘。

全程只讀取記憶體、不搶焦點、不掛除錯器（安全）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import aob
from app.tabs.base_tab import BaseTab

SIG = aob.SKILL_EXP_BALL
# AOB 特徵較鬆（會命中所有技能結構），上限開大以免漏掉目標；靠「正在增加」選出在練技能。
SCAN_LIMIT = 4096
# 數值持續這麼多秒沒再增加 → 判定經驗球滿了 / 遊戲停止 → 警報。
NO_CHANGE_SECS = 300
# 快速讀值的間隔（毫秒）：決定畫面值多快更新一次。
READ_INTERVAL_MS = 700
# 慢速重掃特徵的間隔（秒）：決定換地圖/搬家後多久補上新位址（越小越快跟上、越吃 CPU）。
RESCAN_SECS = 3


class ScanWorker(QThread):
    """慢速執行緒：定期全記憶體重掃 AOB 特徵，更新每台的候選位址清單。

    「全掃」很慢（要讀整塊記憶體），所以放這條慢執行緒、不擋畫面更新；它只負責讓
    候選位址保持最新——換地圖 / 重連把技能結構搬走後，下一次全掃就會拿到新位址。
    候選清單 self._cands 是與 ReadWorker 共享的 dict（本執行緒寫、對方讀）。
    """

    def __init__(self, insts, cands: dict) -> None:
        super().__init__()
        self._insts = insts  # [(pid, title, sc), ...]
        self._cands = cands
        self._running = True

    def run(self) -> None:
        while self._running:
            for pid, _title, sc in self._insts:
                if not self._running:
                    return
                try:
                    self._cands[pid] = aob.scan(sc, SIG, limit=SCAN_LIMIT)
                except Exception:
                    pass
            # 掃完一輪後歇 RESCAN_SECS 再掃；分段睡以便能盡快響應停止。
            for _ in range(max(1, int(RESCAN_SECS * 10))):
                if not self._running:
                    return
                self.msleep(100)

    def stop(self) -> None:
        self._running = False


class ReadWorker(QThread):
    """快速執行緒：每 ~0.7 秒讀一次候選位址的現值，回傳 {pid: {位址: 值}} 快照。

    只讀「已知候選位址」的值（很快），所以畫面更新回到即時；不做全掃。搭配 ScanWorker
    保持候選位址最新即可。與 ScanWorker 共用 scanner——兩者都只讀取、不改動 scanner
    內部掃描狀態，ReadProcessMemory 本身可並行，安全。
    """

    snapshot = Signal(object)  # {pid: {addr: value}}

    def __init__(self, insts, cands: dict) -> None:
        super().__init__()
        self._insts = insts
        self._cands = cands
        self._running = True

    def run(self) -> None:
        while self._running:
            snap: dict[int, dict[int, int]] = {}
            for pid, _title, sc in self._insts:
                if not self._running:
                    return
                addrs = self._cands.get(pid, [])
                try:
                    snap[pid] = {a: sc.read_value(a, SIG.vt) for a in addrs}
                except Exception:
                    snap[pid] = {}
            if not self._running:
                return
            self.snapshot.emit(snap)
            self.msleep(READ_INTERVAL_MS)

    def stop(self) -> None:
        self._running = False


class AlarmThread(QThread):
    """循環發出警報嗶聲，直到 stop()。"""

    def __init__(self) -> None:
        super().__init__()
        self._on = True

    def run(self) -> None:
        try:
            import winsound
        except Exception:
            return
        while self._on:
            try:
                winsound.Beep(1000, 500)
            except Exception:
                self.msleep(500)
            self.msleep(250)

    def stop(self) -> None:
        self._on = False


class AlarmDialog(QDialog):
    """警報視窗：標明哪些帳號經驗球滿了/停止，含「停止警報」按鈕。"""

    def __init__(self, parent, on_stop) -> None:
        super().__init__(parent)
        self.setWindowTitle("⚠ 技能經驗球警報")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(380, 180)
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.label)
        lay.addStretch(1)
        btn = QPushButton("停止警報")
        btn.setStyleSheet("font-size: 14px; padding: 8px;")
        btn.clicked.connect(on_stop)
        lay.addWidget(btn)

    def set_accounts(self, accounts) -> None:
        dur = f"{NO_CHANGE_SECS // 60} 分鐘" if NO_CHANGE_SECS >= 60 else f"{NO_CHANGE_SECS} 秒"
        body = "\n".join(f"　• {a}" for a in accounts)
        self.label.setText(
            "以下分身的技能經驗球滿了、或遊戲停止/斷線了\n"
            f"（數值持續 {dur} 沒有再增加）：\n\n" + body
        )


class MonitorTab(BaseTab):
    TAB_TITLE = "監控技能經驗球"
    ORDER = 5

    def build_ui(self) -> None:
        self._mons: dict[int, dict] = {}
        self._cands: dict[int, list] = {}   # {pid: [addr,...]}，ScanWorker 寫、ReadWorker 讀
        self._scan_worker: ScanWorker | None = None
        self._read_worker: ReadWorker | None = None
        self._alarm_thread: AlarmThread | None = None
        self._alarm_dialog: AlarmDialog | None = None
        self._alarm_accts: list[str] = []

        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "即時監控每個分身正在練的技能經驗球。每輪都重新掃特徵、自動追最新位址，\n"
                "換地圖 / 重連 / 升級都不會跑掉。某台「持續 5 分鐘沒再增加」（滿了或停止）→ "
                "自動停該台 + 警報聲 + 跳視窗提示。"
            )
        )
        row = QHBoxLayout()
        self.start_btn = QPushButton("開始監控")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.rescan_btn = QPushButton("重新探索分身")
        self.rescan_btn.setEnabled(False)
        self.rescan_btn.clicked.connect(self.rescan)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.rescan_btn)
        row.addStretch(1)
        root.addLayout(row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["帳號", "PID", "技能經驗球", "狀態"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self.table)

        self.status = QLabel("就緒")
        self.status.setStyleSheet("color: gray;")
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    def _find_instances(self):
        insts, seen = [], set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:
                continue
            insts.append((w.pid, w.title, sc))
        return insts

    @staticmethod
    def _acct(title: str) -> str:
        return title.split(" - ", 1)[1] if " - " in title else title

    # ------------------------------------------------------------------
    # 開始 / 停止 / 重新探索
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop_alarm()  # 重新開始前先關掉還在響的警報
        insts = self._find_instances()
        if not insts:
            QMessageBox.information(
                self, "找不到分身",
                "找不到天使之戀分身。請確認遊戲開著且已登入。\n"
                "若遊戲以系統管理員身分執行，請同樣以系統管理員身分執行本工具。",
            )
            return
        self._begin(insts)

    def rescan(self) -> None:
        """重新探索分身（納入新開的視窗 / 移除已關的），並重啟監控。"""
        self._stop_worker()
        self._close_scanners()
        self._stop_alarm()
        insts = self._find_instances()
        if not insts:
            self._reset_ui("找不到分身")
            return
        self._begin(insts)

    def _begin(self, insts) -> None:
        self._mons = {
            pid: {
                "title": title, "sc": sc,
                "prev": {},          # 上一輪快照 {addr: value}
                "tracked": None,     # 目前跟隨（在練）的位址
                "value": None,       # 顯示值
                "last_inc": None,    # 上次偵測到「增加」的時間；None = 還沒動過 → 不倒數
            }
            for pid, title, sc in insts
        }
        self._rebuild_table()
        insts = [(pid, m["title"], m["sc"]) for pid, m in self._mons.items()]
        self._cands = {pid: [] for pid, _t, _s in insts}
        # 慢執行緒定期重掃特徵更新候選位址；快執行緒每 ~0.7s 讀值更新畫面。
        self._scan_worker = ScanWorker(insts, self._cands)
        self._read_worker = ReadWorker(insts, self._cands)
        self._read_worker.snapshot.connect(self._on_snapshot)
        self._scan_worker.start()
        self._read_worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self.status.setText(f"監控中：{len(self._mons)} 台（背景持續重掃特徵，自動追最新位址）")

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self._mons))
        for r, (pid, m) in enumerate(self._mons.items()):
            self.table.setItem(r, 0, QTableWidgetItem(self._acct(m["title"])))
            self.table.setItem(r, 1, QTableWidgetItem(str(pid)))
            self.table.setItem(r, 2, QTableWidgetItem("…"))
            self.table.setItem(r, 3, QTableWidgetItem("定位中…"))

    # ------------------------------------------------------------------
    # 每輪快照處理（在 UI 執行緒，由背景 worker 的訊號觸發）
    # ------------------------------------------------------------------
    def _on_snapshot(self, snap: dict) -> None:
        now = time.monotonic()
        stalled = []
        for r, (pid, m) in enumerate(self._mons.items()):
            if r >= self.table.rowCount():
                break
            cur = snap.get(pid)
            if cur is None:
                continue  # 這輪沒有這台的資料

            prev = m["prev"]
            # 這輪相對上一輪「有增加」的位址們（cur 為空→掃不到特徵→自然沒有任何增加）
            increased = {
                a: v - prev[a]
                for a, v in cur.items()
                if v is not None and prev.get(a) is not None and v > prev[a]
            }
            if increased:
                tracked = m["tracked"]
                if tracked not in increased:
                    # 換一顆在動的（首次鎖定 / 換地圖搬家 / 切技能）→ 改跟增加最多的那顆
                    m["tracked"] = max(increased, key=increased.get)
                m["last_inc"] = now

            # 更新顯示值 = 跟隨那顆的現值
            tr = m["tracked"]
            if tr is not None and cur.get(tr) is not None:
                m["value"] = cur[tr]

            # 「突然掃不到特徵」通常代表斷線／遊戲關了，等同「沒有球在動」，照樣倒數。
            lost = not cur
            if m["last_inc"] is None:
                # 還沒鎖定過 → 不倒數（先有變動才算 5 分鐘）。
                self._set_row(r, m["value"],
                              "定位中…（掃不到特徵）" if lost else "等待變動以鎖定技能…")
            else:
                quiet = now - m["last_inc"]
                if quiet >= NO_CHANGE_SECS:
                    stalled.append(self._acct(m["title"]))
                    self._set_row(r, m["value"],
                                  "⚠ 掃不到特徵（可能斷線）" if lost else "⚠ 已滿/停止")
                elif lost:
                    self._set_row(r, m["value"], f"掃不到特徵…（{int(quiet)}s，可能斷線）")
                else:
                    self._set_row(r, m["value"], f"監控中（{int(quiet)}s 未增加）")
            m["prev"] = cur

        if stalled:
            # 任何一台觸發 → 響警報 + 整個監控停掉（等同按停止），要自己重按「開始監控」。
            self._alarm_and_stop(stalled)

    def _set_row(self, r: int, value, status: str) -> None:
        self.table.setItem(r, 2, QTableWidgetItem(
            "—" if value is None else str(value)))
        self.table.setItem(r, 3, QTableWidgetItem(status))

    # ------------------------------------------------------------------
    # 警報
    # ------------------------------------------------------------------
    def _alarm_and_stop(self, accounts) -> None:
        for a in accounts:
            if a not in self._alarm_accts:
                self._alarm_accts.append(a)
        if self._alarm_thread is None or not self._alarm_thread.isRunning():
            self._alarm_thread = AlarmThread()
            self._alarm_thread.start()
        if self._alarm_dialog is None:
            self._alarm_dialog = AlarmDialog(self, self._stop_alarm)
        self._alarm_dialog.set_accounts(self._alarm_accts)
        self._alarm_dialog.show()
        self._alarm_dialog.raise_()
        self._alarm_dialog.activateWindow()
        # 整個監控停掉（等同按停止），但警報聲繼續響到按「停止警報」
        self._stop_worker()
        self._close_scanners()
        self._reset_ui("已停止（觸發警報）— 請按『開始監控』重新開始")

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------
    def _stop_worker(self) -> None:
        """停掉兩條背景執行緒並等它們結束（一定要在關閉 scanner 前呼叫）。"""
        if self._read_worker:
            try:
                self._read_worker.snapshot.disconnect(self._on_snapshot)
            except Exception:
                pass
            self._read_worker.stop()
        if self._scan_worker:
            self._scan_worker.stop()
        if self._read_worker:
            self._read_worker.wait(5000)
            self._read_worker = None
        if self._scan_worker:
            self._scan_worker.wait(8000)  # 可能正卡在一次全掃，等久一點
            self._scan_worker = None

    def _close_scanners(self) -> None:
        for m in self._mons.values():
            try:
                m["sc"].close()
            except Exception:
                pass

    def _reset_ui(self, status: str) -> None:
        self._mons = {}
        self.table.setRowCount(0)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.status.setText(status)

    def _stop_alarm(self) -> None:
        if self._alarm_thread:
            self._alarm_thread.stop()
            self._alarm_thread.wait(2000)
            self._alarm_thread = None
        if self._alarm_dialog:
            self._alarm_dialog.hide()
        self._alarm_accts = []

    def stop(self) -> None:
        self._stop_alarm()
        self._stop_worker()
        self._close_scanners()
        self._reset_ui("已停止")

    def on_close(self) -> None:
        self._stop_alarm()
        self._stop_worker()
        self._close_scanners()
