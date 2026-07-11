"""監控技能經驗球分頁（產品主畫面）。

即時監控每個天使之戀分身正在練的技能經驗球：
  1. 按「開始監控」→ 自動找出所有分身，用 AOB 特徵碼定位技能經驗球。
  2. 鎖定「正在增加」的那顆（在練的技能），顯示即時值與每小時經驗。
  3. 若某台的值「持續 5 分鐘沒有變動」（經驗球滿了、或遊戲停止/斷線）→
     自動停止該台監控、循環發出警報聲、並跳出警告視窗標明是哪個帳號；
     警報聲持續到按「停止警報」為止。

全程只讀取記憶體、不搶焦點、不掛除錯器（安全）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
# 數值持續這麼多秒沒變動 → 判定經驗球滿了 / 遊戲停止 → 警報。
NO_CHANGE_SECS = 300


class LocateWorker(QThread):
    """背景掃描每個分身、用 AOB 特徵定位經驗球候選位址。"""

    done = Signal(object)  # [(pid, title, scanner, [candidate_addrs]), ...]

    def __init__(self, insts) -> None:
        super().__init__()
        self._insts = insts

    def run(self) -> None:
        out = []
        for pid, title, sc in self._insts:
            try:
                cands = aob.scan(sc, SIG, limit=SCAN_LIMIT)
            except Exception:
                cands = []
            out.append((pid, title, sc, cands))
        self.done.emit(out)


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
            f"（數值持續 {dur} 沒有變動）：\n\n" + body
        )


class MonitorTab(BaseTab):
    TAB_TITLE = "監控技能經驗球"
    ORDER = 5

    def build_ui(self) -> None:
        self._mons: dict[int, dict] = {}
        self._worker: LocateWorker | None = None
        self._alarm_thread: AlarmThread | None = None
        self._alarm_dialog: AlarmDialog | None = None
        self._alarm_accts: list[str] = []

        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "即時監控每個分身正在練的技能經驗球（AOB 特徵定位，重開/多開都能自動定位）。\n"
                "某台值「持續 5 分鐘沒變動」（經驗球滿了或遊戲停止）→ 自動停該台 + 警報聲 + 跳視窗提示。"
            )
        )
        row = QHBoxLayout()
        self.start_btn = QPushButton("開始監控")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.rescan_btn = QPushButton("重新定位")
        self.rescan_btn.setEnabled(False)
        self.rescan_btn.clicked.connect(self.rescan)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.rescan_btn)
        row.addStretch(1)
        root.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["帳號", "PID", "技能經驗球", "每小時經驗", "狀態"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self.table)

        self.status = QLabel("就緒")
        self.status.setStyleSheet("color: gray;")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)

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
        self._begin_locate(insts, f"定位中…掃描 {len(insts)} 台的經驗球特徵")

    def rescan(self) -> None:
        if not self._mons:
            return
        self._timer.stop()
        insts = [(pid, m["title"], m["sc"]) for pid, m in self._mons.items()]
        self._begin_locate(insts, "重新定位中…")

    def _begin_locate(self, insts, msg: str) -> None:
        self.start_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.status.setText(msg)
        self._worker = LocateWorker(insts)
        self._worker.done.connect(self._on_located)
        self._worker.start()

    def _on_located(self, results) -> None:
        now = time.monotonic()
        self._mons = {}
        for pid, title, sc, cands in results:
            v0 = {a: sc.read_value(a, SIG.vt) for a in cands}
            self._mons[pid] = {
                "title": title, "sc": sc, "cands": cands, "v0": v0,
                "active": None, "value": None, "last_change": now,
                "v_lock": None, "t_lock": now, "alerted": False,
            }
        self._rebuild_table()
        self._timer.start()
        self.stop_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        located = sum(1 for m in self._mons.values() if m["cands"])
        self.status.setText(f"監控中：{len(self._mons)} 台（成功定位 {located} 台）")

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self._mons))
        for r, (pid, m) in enumerate(self._mons.items()):
            self.table.setItem(r, 0, QTableWidgetItem(self._acct(m["title"])))
            self.table.setItem(r, 1, QTableWidgetItem(str(pid)))
            self.table.setItem(r, 2, QTableWidgetItem("…"))
            self.table.setItem(r, 3, QTableWidgetItem("—"))
            self.table.setItem(r, 4, QTableWidgetItem(
                "定位失敗" if not m["cands"] else "準備中"))
        self._poll()

    # ------------------------------------------------------------------
    def _poll(self) -> None:
        now = time.monotonic()
        stalled = []
        for r, (pid, m) in enumerate(self._mons.items()):
            if r >= self.table.rowCount():
                break
            if not m["cands"]:
                self._set_row(r, None, None, "定位失敗（可按重新定位）")
                continue
            sc = m["sc"]

            if m["active"] is None:
                # 找「正在增加」的那顆來鎖定
                best_a, best_d, curvals = None, 0, {}
                for a in m["cands"]:
                    v = sc.read_value(a, SIG.vt)
                    curvals[a] = v
                    if v is None:
                        continue
                    d = v - (m["v0"].get(a) or v)
                    if d > best_d:
                        best_d, best_a = d, a
                if best_a is not None and best_d > 0:
                    m["active"] = best_a
                    m["value"] = curvals[best_a]
                    m["last_change"] = now
                    m["v_lock"], m["t_lock"] = curvals[best_a], now
                    self._set_row(r, m["value"], 0.0, "監控中（已鎖定在練技能）")
                else:
                    valid = {a: v for a, v in curvals.items() if v is not None}
                    tentative = max(valid, key=valid.get) if valid else None
                    self._set_row(r, valid.get(tentative), None, "等待變動以鎖定技能…")
                continue

            # 已鎖定 → 監控該位址
            v = sc.read_value(m["active"], SIG.vt)
            if v is None:
                v = m["value"]  # 讀不到（遊戲可能關了）→ 視為未變動，讓計時繼續
            if v != m["value"]:
                m["value"] = v
                m["last_change"] = now
            dur = now - m["last_change"]
            if dur >= NO_CHANGE_SECS:
                stalled.append(self._acct(m["title"]))
                self._set_row(r, m["value"], 0.0, "⚠ 已滿/停止")
            else:
                rate = 0.0
                dt = now - m["t_lock"]
                if m["v_lock"] is not None and m["value"] is not None and dt > 3:
                    rate = (m["value"] - m["v_lock"]) / dt * 3600
                self._set_row(r, m["value"], rate, f"監控中（{int(dur)}s 未變）")

        if stalled:
            # 任何一台觸發 → 響警報 + 整個監控停掉（等同按停止），要自己重按「開始監控」。
            self._alarm_and_stop(stalled)

    def _set_row(self, r: int, value, rate, status: str) -> None:
        self.table.setItem(r, 2, QTableWidgetItem(
            "—" if value is None else str(value)))
        self.table.setItem(r, 3, QTableWidgetItem(
            "—" if rate is None else f"+{rate:,.0f}/時"))
        self.table.setItem(r, 4, QTableWidgetItem(status))

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
        self._stop_monitoring("已停止（觸發警報）— 請按『開始監控』重新開始")

    def _stop_monitoring(self, status: str = "已停止") -> None:
        self._timer.stop()
        for m in self._mons.values():
            try:
                m["sc"].close()
            except Exception:
                pass
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

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_alarm()
        self._stop_monitoring("已停止")

    def on_close(self) -> None:
        self._timer.stop()
        self._stop_alarm()
        if self._worker and self._worker.isRunning():
            self._worker.wait(5000)
        for m in self._mons.values():
            try:
                m["sc"].close()
            except Exception:
                pass
