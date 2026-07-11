"""封包 / 登入攔截分頁。

掛住遊戲的 send，把每個「送出的封包」記錄下來：內容、長度、亂度(判斷加密)、
以及送出時的呼叫鏈（落在 angel.dat 程式碼的返回位址 = 登入函式等高階邏輯候選）。

操作流程：
  ① 在清單選遊戲程序 → 按「選定並開始攔截」。
  ② 回遊戲做一次登入（或任何動作）。
  ③ 下方即時列出送出的封包。找「你登入那一刻才出現」的那筆，看它的呼叫鏈，
     最上層的位址就是登入函式候選；亂度 >7.5 多半是加密。
  ④ 按「停止攔截」還原（關閉分頁也會自動還原）。

注入採延遲載入(pymem/keystone/pefile)；未安裝時本頁會提示安裝，不影響其他分頁。
純讀寫記憶體、不搶焦點、只作用於你選定的程序。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import injector
from app.core import window as win
from app.tabs.base_tab import BaseTab

MAX_ROWS = 500  # 封包表最多保留列數


class PacketTab(BaseTab):
    TAB_TITLE = "封包 / 登入攔截"
    ORDER = 60

    def build_ui(self) -> None:
        self._cap: injector.SendCapture | None = None
        self._packets: list[injector.Packet] = []
        self._windows: list[win.WindowInfo] = []

        root = QVBoxLayout(self)

        ok, msg = injector.available()
        root.addWidget(
            QLabel(
                "掛住遊戲的 send，攔截送出的封包，找出「送登入封包的那段程式」(=登入函式候選)。\n"
                "① 選遊戲程序 → 開始攔截 → ② 回遊戲登入 → ③ 下方看封包的『呼叫鏈』與亂度。"
            )
        )

        root.addWidget(self._build_process_group())
        root.addWidget(self._build_packet_group(), stretch=2)
        root.addWidget(self._build_detail_group(), stretch=1)

        self.status = QLabel("就緒")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

        # 即時輪詢環狀緩衝
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._poll)

        if not ok:
            self._set_enabled_capture(False)
            self.start_btn.setEnabled(False)
            self.status.setText("⚠ 注入功能不可用：" + msg)
        self.refresh_windows()

    # ------------------------------------------------------------------
    def _build_process_group(self) -> QGroupBox:
        box = QGroupBox("① 選定遊戲程序")
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("依視窗標題關鍵字過濾（可留空）")
        self.filter_edit.setText("Angels Online")
        self.filter_edit.returnPressed.connect(self.refresh_windows)
        refresh_btn = QPushButton("重新整理")
        refresh_btn.clicked.connect(self.refresh_windows)
        row.addWidget(self.filter_edit)
        row.addWidget(refresh_btn)
        lay.addLayout(row)

        self.proc_table = QTableWidget(0, 3)
        self.proc_table.setHorizontalHeaderLabels(["PID", "視窗標題", "類別"])
        self.proc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.proc_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.proc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.proc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.proc_table.setMaximumHeight(160)
        lay.addWidget(self.proc_table)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("選定並開始攔截")
        self.start_btn.clicked.connect(self.start_capture)
        self.stop_btn = QPushButton("停止攔截")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_capture)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.clicked.connect(self.clear_packets)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        return box

    def _build_packet_group(self) -> QGroupBox:
        box = QGroupBox("② 送出的封包（即時）")
        lay = QVBoxLayout(box)
        self.pkt_table = QTableWidget(0, 5)
        self.pkt_table.setHorizontalHeaderLabels(
            ["#", "長度", "亂度/8", "呼叫鏈（登入函式候選）", "內容預覽"]
        )
        self.pkt_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pkt_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.pkt_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pkt_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.pkt_table.itemSelectionChanged.connect(self._show_detail)
        lay.addWidget(self.pkt_table)
        return box

    def _build_detail_group(self) -> QGroupBox:
        box = QGroupBox("③ 選取封包的內容（hex）")
        lay = QVBoxLayout(box)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setStyleSheet("font-family: Consolas, monospace;")
        lay.addWidget(self.detail)
        return box

    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        keyword = self.filter_edit.text().strip() or None
        self._windows = win.enumerate_windows(title_contains=keyword)
        self.proc_table.setRowCount(len(self._windows))
        for r, w in enumerate(self._windows):
            self.proc_table.setItem(r, 0, QTableWidgetItem(str(w.pid)))
            self.proc_table.setItem(r, 1, QTableWidgetItem(w.title))
            self.proc_table.setItem(r, 2, QTableWidgetItem(w.class_name))
        if not self._cap:
            self.status.setText(f"找到 {len(self._windows)} 個視窗")

    def _selected_window(self) -> win.WindowInfo | None:
        rows = self.proc_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在清單選一個遊戲視窗。")
            return None
        return self._windows[rows[0].row()]

    def _set_enabled_capture(self, capturing: bool) -> None:
        self.start_btn.setEnabled(not capturing)
        self.stop_btn.setEnabled(capturing)
        self.proc_table.setEnabled(not capturing)
        self.filter_edit.setEnabled(not capturing)

    # ------------------------------------------------------------------
    def start_capture(self) -> None:
        w = self._selected_window()
        if not w:
            return
        exe = injector.process_path(w.pid)
        if not exe:
            QMessageBox.warning(
                self,
                "無法取得執行檔",
                "抓不到該程序的執行檔路徑。若遊戲以系統管理員身分執行，"
                "請同樣以系統管理員身分執行本工具。",
            )
            return
        cap = injector.SendCapture(w.pid, exe)
        try:
            cap.start()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self,
                "開始攔截失敗",
                f"{exc}\n\n（若為權限問題，請以系統管理員身分執行本工具。）",
            )
            return
        self._cap = cap
        self._set_enabled_capture(True)
        self._timer.start()
        self.status.setText(
            f"攔截中：PID {w.pid} — {w.title}\n"
            "現在回遊戲登入；送出的封包會即時出現在下方。"
        )

    def stop_capture(self) -> None:
        self._timer.stop()
        if self._cap:
            try:
                self._cap.stop()
            except Exception:
                pass
            self._cap = None
        self._set_enabled_capture(False)
        self.status.setText(f"已停止攔截（共攔到 {len(self._packets)} 個封包）。")

    def clear_packets(self) -> None:
        self._packets.clear()
        self.pkt_table.setRowCount(0)
        self.detail.clear()

    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if not self._cap or not self._cap.active:
            return
        try:
            new = self._cap.read_new()
        except Exception as exc:  # noqa: BLE001
            self._timer.stop()
            self.status.setText(f"讀取失敗，已停止：{exc}")
            return
        for pkt in new:
            self._append_packet(pkt)

    def _append_packet(self, pkt: injector.Packet) -> None:
        self._packets.append(pkt)
        # 超過上限時裁掉最舊的（同步表格）
        if len(self._packets) > MAX_ROWS:
            self._packets = self._packets[-MAX_ROWS:]
            self._rebuild_table()
            return
        r = self.pkt_table.rowCount()
        self.pkt_table.insertRow(r)
        self._fill_row(r, pkt)
        self.pkt_table.scrollToBottom()

    def _rebuild_table(self) -> None:
        self.pkt_table.setRowCount(len(self._packets))
        for r, pkt in enumerate(self._packets):
            self._fill_row(r, pkt)

    def _fill_row(self, r: int, pkt: injector.Packet) -> None:
        chain = " ← ".join(f"0x{a:X}" for a in pkt.call_chain[:6]) or "—"
        preview = pkt.data[:24].hex(" ")
        ent = f"{pkt.entropy:.2f}"
        seq_item = QTableWidgetItem(str(pkt.seq))
        seq_item.setData(Qt.UserRole, pkt.seq)
        self.pkt_table.setItem(r, 0, seq_item)
        self.pkt_table.setItem(r, 1, QTableWidgetItem(str(pkt.length)))
        ent_item = QTableWidgetItem(ent)
        if pkt.entropy > 7.5:
            ent_item.setForeground(Qt.red)  # 高亂度 → 疑似加密
        self.pkt_table.setItem(r, 2, ent_item)
        self.pkt_table.setItem(r, 3, QTableWidgetItem(chain))
        self.pkt_table.setItem(r, 4, QTableWidgetItem(preview))

    def _show_detail(self) -> None:
        rows = self.pkt_table.selectionModel().selectedRows()
        if not rows:
            return
        seq = self.pkt_table.item(rows[0].row(), 0).data(Qt.UserRole)
        pkt = next((p for p in self._packets if p.seq == seq), None)
        if not pkt:
            return
        chain = "\n".join(f"    0x{a:X}" for a in pkt.call_chain) or "    （無）"
        verdict = "疑似加密/壓縮" if pkt.entropy > 7.5 else "疑似明文/輕度混淆"
        self.detail.setPlainText(
            f"封包 #{pkt.seq}　長度 {pkt.length}　亂度 {pkt.entropy:.2f}/8 → {verdict}\n"
            f"直接呼叫者(send wrapper)：0x{pkt.caller:X}\n"
            f"呼叫鏈（遊戲內位址，登入函式候選在較上層）：\n{chain}\n"
            f"\n內容：\n{pkt.hexdump()}"
        )

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._timer.stop()
        if self._cap:
            try:
                self._cap.stop()   # 還原 IAT，不留下掛鉤
            except Exception:
                pass
            self._cap = None
