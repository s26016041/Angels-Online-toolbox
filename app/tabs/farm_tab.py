"""自動掛機分頁：列出附近怪物，選一隻，然後持續送攻擊按鍵。

運作原理（見 app/game/entity.py 的說明）
----------------------------------------
遊戲攻擊時會**自己重讀**「客戶端目前選定的目標」那個欄位，所以只要：
    ① 把目標的實體 ID 寫進狀態物件 +0x2D8
    ② 送一個攻擊按鍵（預設 F2）給該視窗
角色就會打那隻怪。不必注入會執行的程式碼、不必自己組封包、不必搶視窗焦點
（背景視窗吃得到鍵盤訊息，吃不到滑鼠點擊 —— 所以選目標非走記憶體不可）。

介面
----
每個分身一個子分頁（標題就是角色名）。各自可以掃描周圍怪物、選一隻，
勾「開始掛機」才會開始迴圈，取消勾選就停。

掃描要跑全記憶體（約 1～3 秒），所以放在背景執行緒，而且只在按下按鈕或
勾選掛機時才掃 —— 不做無謂的定期重掃。
"""
from __future__ import annotations

import ctypes

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import charname
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import entity
from app.tabs.base_tab import BaseTab

VK_F2 = 0x71
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
DEFAULT_INTERVAL = 0.1          # 秒；使用者指定預設每 0.1 秒按一次
TICK_MS = 20                    # 迴圈計時器解析度


def send_key(hwnd: int, vk: int = VK_F2) -> None:
    """對指定視窗送一次按鍵。用 PostMessage 所以不會搶焦點。"""
    u = ctypes.windll.user32
    u.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    u.PostMessageW(hwnd, WM_KEYUP, vk, 0)


class ScanWorker(QThread):
    """背景掃描：定位狀態物件 + 列出附近怪物。

    全記憶體掃描一次約 1～3 秒，不能放在 UI 執行緒。
    用一條常駐執行緒處理所有分身的請求，比每次開新執行緒好收尾。
    """

    done = Signal(int, object, object, str)   # pid, state, monsters, err

    def __init__(self) -> None:
        super().__init__()
        self._queue: list[tuple[int, MemoryScanner]] = []
        self._running = True

    def request(self, pid: int, sc: MemoryScanner) -> None:
        if not any(p == pid for p, _ in self._queue):
            self._queue.append((pid, sc))

    def run(self) -> None:
        while self._running:
            if not self._queue:
                self.msleep(80)
                continue
            pid, sc = self._queue.pop(0)
            state = mons = None
            err = ""
            try:
                state = entity.locate_state(
                    sc, should_stop=lambda: not self._running)
                if state is None:
                    err = "找不到狀態物件（掃到 0 個或多個）"
                mons = entity.monsters(sc, should_stop=lambda: not self._running)
            except Exception as exc:               # noqa: BLE001
                err = f"掃描失敗：{exc}"
            if self._running:
                self.done.emit(pid, state, mons, err)

    def stop(self) -> None:
        self._running = False


class CharFarmPage(QWidget):
    """單一分身的掛機介面。"""

    def __init__(self, pid: int, hwnd: int, title: str,
                 sc: MemoryScanner, on_scan) -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        self.state: int | None = None
        self.mons: list[entity.Entity] = []
        self._on_scan = on_scan
        self._acc = 0.0

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.scan_btn = QPushButton("掃描周圍怪物")
        self.scan_btn.clicked.connect(lambda: self._on_scan(self.pid))
        bar.addWidget(self.scan_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("每隔"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.05, 5.0)
        self.interval.setSingleStep(0.05)
        self.interval.setDecimals(2)
        self.interval.setValue(DEFAULT_INTERVAL)
        self.interval.setSuffix(" 秒按一次 F2")
        self.interval.setFixedWidth(150)
        bar.addWidget(self.interval)
        bar.addSpacing(12)
        self.run_cb = QCheckBox("開始掛機")
        self.run_cb.setToolTip(
            "勾選後開始迴圈：把選中的怪寫進遊戲的『目前目標』，並持續送 F2。\n"
            "取消勾選立刻停止。")
        self.run_cb.toggled.connect(self._on_toggle)
        bar.addWidget(self.run_cb)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名稱", "種類 ID", "實體 ID"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        self.status = QLabel("尚未掃描")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    def apply_scan(self, state, mons, err: str) -> None:
        self.state = state
        self.mons = mons or []
        self.table.setRowCount(len(self.mons))
        for r, m in enumerate(self.mons):
            self.table.setItem(r, 0, QTableWidgetItem(m.name))
            self.table.setItem(r, 1, QTableWidgetItem(str(m.type_id)))
            self.table.setItem(r, 2, QTableWidgetItem(f"{m.eid:#010x}"))
        if err:
            self.status.setText(f"⚠ {err}")
        else:
            cur = entity.read_target(self.sc, state) if state else 0
            hit = next((m.name for m in self.mons if m.eid == cur), None)
            self.status.setText(
                f"找到 {len(self.mons)} 隻　"
                + (f"目前目標：{hit}" if hit
                   else ("目前沒有選定目標" if not cur else
                         f"目前目標 {cur:#x}（不在清單裡）")))
        self.scan_btn.setEnabled(True)

    def selected(self) -> entity.Entity | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        i = rows[0].row()
        return self.mons[i] if 0 <= i < len(self.mons) else None

    def _on_toggle(self, on: bool) -> None:
        if not on:
            self.status.setText("已停止")
            return
        if self.state is None:
            self.run_cb.setChecked(False)
            QMessageBox.information(self, "還不能開始",
                                    "請先按「掃描周圍怪物」。")
            return
        if self.selected() is None:
            self.run_cb.setChecked(False)
            QMessageBox.information(self, "還不能開始",
                                    "請先在清單裡選一隻要打的怪。")
            return
        self._acc = 0.0
        self.status.setText("掛機中…")

    def tick(self, dt: float) -> None:
        """迴圈的一次心跳。由分頁的計時器統一驅動。"""
        if not self.run_cb.isChecked() or self.state is None:
            return
        m = self.selected()
        if m is None:
            return
        self._acc += dt
        if self._acc < self.interval.value():
            return
        self._acc = 0.0
        try:
            # 每次都重寫目標 —— 遊戲自己也可能改動它（例如怪死了）
            entity.set_target(self.sc, self.state, m.eid)
        except Exception as exc:                   # noqa: BLE001
            self.run_cb.setChecked(False)
            self.status.setText(f"⚠ 寫入失敗：{exc}")
            return
        send_key(self.hwnd)
        alive = entity.is_alive(self.sc, m)
        self.status.setText(
            f"掛機中：{m.name} {m.eid:#010x}"
            + ("" if alive else "　⚠ 這隻已經不在了，請重新掃描並選一隻"))


class FarmTab(BaseTab):
    TAB_TITLE = "自動掛機"
    ORDER = 5

    def build_ui(self) -> None:
        self._pages: dict[int, CharFarmPage] = {}
        self._scanners: list[MemoryScanner] = []
        self._worker: ScanWorker | None = None

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.rescan_btn = QPushButton("重新偵測分身")
        self.rescan_btn.clicked.connect(self.reload_instances)
        bar.addWidget(self.rescan_btn)
        self.found = QLabel("尚未偵測")
        self.found.setStyleSheet("color: #9aa2b8;")
        bar.addWidget(self.found)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        hint = QLabel(
            "① 按「掃描周圍怪物」→ ② 在清單選一隻 → ③ 勾「開始掛機」。\n"
            "原理：把選中那隻的實體 ID 寫進遊戲的「目前目標」欄位，再持續送 F2。"
            "不會搶視窗焦點，可以同時掛多個分身。")
        hint.setStyleSheet("color: #9aa2b8;")
        root.addWidget(hint)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._pages:
            self.reload_instances()

    def reload_instances(self) -> None:
        self._teardown()
        insts = []
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:
                continue
            self._scanners.append(sc)
            insts.append((w.pid, w.hwnd, w.title, sc))
        if not insts:
            self.found.setText("找不到分身")
            return
        self._worker = ScanWorker()
        self._worker.done.connect(self._on_scan_done)
        self._worker.start()
        for pid, hwnd, title, sc in insts:
            page = CharFarmPage(pid, hwnd, title, sc, self._request_scan)
            self._pages[pid] = page
            acct = charname.account_from_title(title)
            nm = charname.read_character_name(sc, acct) or acct
            self.tabs.addTab(page, nm)
        self.found.setText(f"偵測到 {len(insts)} 個分身")

    def _request_scan(self, pid: int) -> None:
        page = self._pages.get(pid)
        if page is None or self._worker is None:
            return
        page.scan_btn.setEnabled(False)
        page.status.setText("掃描中…（全記憶體掃描，約 1～3 秒）")
        self._worker.request(pid, page.sc)

    def _on_scan_done(self, pid: int, state, mons, err: str) -> None:
        page = self._pages.get(pid)
        if page is not None:
            page.apply_scan(state, mons, err)

    def _tick(self) -> None:
        dt = TICK_MS / 1000.0
        for page in self._pages.values():
            page.tick(dt)

    # ------------------------------------------------------------------
    def _teardown(self) -> None:
        for page in self._pages.values():
            page.run_cb.setChecked(False)
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(5000)
            self._worker = None
        self.tabs.clear()
        self._pages = {}
        for sc in self._scanners:
            try:
                sc.close()
            except Exception:
                pass
        self._scanners = []

    def on_close(self) -> None:
        self._timer.stop()
        self._teardown()
