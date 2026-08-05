"""記憶體掃描分頁（Cheat Engine 風格）。

用途：找出遊戲中某個數值（經驗值 / HP / 金錢…）實際存放的記憶體位址。

操作流程：
  1. 「重新整理」列出視窗，選一個遊戲視窗 → 按「選定此程序」。
  2. 選數值型別（通常是 4 位元組整數），輸入你在遊戲裡看到的數字，
     按「首次搜尋」。
       ─ 不知道確切數字？把條件改成「未知初始值」直接首次搜尋。
  3. 回遊戲讓數值改變（打怪讓經驗值增加、扣血…），回來把條件改成
     「增加 / 減少 / 已改變 / 等於新值」再按「再次搜尋」。
  4. 重複第 3 步，候選會越縮越少，直到剩幾個位址。
  5. 在結果選一列按「加入觀察」，就能在下方持續看它的即時值，也能寫入。

掃描在背景執行緒進行，不會卡住介面；全程只讀取你選定的程序，不搶焦點。

pymem / 掃描核心採延遲載入：核心是純 ctypes，不需額外套件即可運作。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import window as win
from app.core.memory import (
    STRING_ENCODINGS,
    VALUE_REQUIRED,
    VALUE_TYPES,
    MemoryScanner,
    working_set_mb,
)
from app.tabs.base_tab import BaseTab

# 結果表格最多顯示的列數（候選可能很多，全塞會拖垮介面）。
RESULT_DISPLAY_LIMIT = 2000

# 下拉選單裡的搜尋條件順序（含未知初始值）。
SCAN_TYPE_ORDER = [
    ("exact", "等於"),
    ("bigger", "大於"),
    ("smaller", "小於"),
    ("unknown", "未知初始值"),
    ("increased", "增加"),
    ("decreased", "減少"),
    ("changed", "已改變"),
    ("unchanged", "未改變"),
]


class ScanWorker(QThread):
    """在背景執行一次掃描（首次或再次），避免卡住介面。"""

    progress = Signal(int)  # 0-100
    done = Signal(int)  # 剩餘候選數
    failed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            count = self._fn(lambda f: self.progress.emit(int(f * 100)))
            self.done.emit(int(count))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MemoryTab(BaseTab):
    TAB_TITLE = "記憶體掃描"
    ORDER = 50

    def build_ui(self) -> None:
        self._scanner = MemoryScanner()
        self._worker: ScanWorker | None = None
        self._windows: list[win.WindowInfo] = []
        self._base_addr = 0  # 選定程序後的主模組基底位址
        # 觀察清單：每筆 {"addr": int, "vt": ValueType}
        self._watch: list[dict] = []
        # 字串搜尋結果：[(addr, enc_key, byte_len), ...] 與 addr -> (enc, len)
        self._str_hits: list[tuple[int, str, int]] = []
        self._str_result_meta: dict[int, tuple[str, int]] = {}

        # 內容用捲動區包起來，區塊多時仍好操作。
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        root = QVBoxLayout(container)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        # ⚠ 說明文字一定要開自動換行：視窗窄的時候不換行就會被切掉半句
        #   （使用者要求所有文字都要完整顯示）。
        hint = QLabel(
            "找出遊戲數值的記憶體位址：選定遊戲程序 → 搜尋目前的值 → "
            "回遊戲讓值改變 → 再次搜尋篩選，逐步縮小到目標位址。\n"
            "找到後在「③ 搜尋結果」選一列「加入觀察」，即可持續看即時值、也能寫入。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addWidget(self._build_process_group())
        root.addWidget(self._build_scan_group())
        root.addWidget(self._build_string_group())
        root.addWidget(self._build_results_group(), stretch=2)
        root.addWidget(self._build_watch_group(), stretch=1)

        # 觀察清單即時刷新
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(800)
        self._watch_timer.timeout.connect(self._refresh_live)
        self._watch_timer.start()

        self.refresh_windows()
        self._update_enabled()
        self._on_scan_type_changed()

    # ------------------------------------------------------------------
    # 介面組件
    # ------------------------------------------------------------------
    @staticmethod
    def _set_min_rows(table, rows: int = 5, max_rows: int | None = None) -> None:
        """讓表格至少（或最多）顯示指定列數，避免被壓得太小。"""
        header = table.horizontalHeader().sizeHint().height()
        row_h = table.verticalHeader().defaultSectionSize()
        frame = 2 * table.frameWidth()
        table.setMinimumHeight(header + row_h * rows + frame)
        if max_rows is not None:
            table.setMaximumHeight(header + row_h * max_rows + frame)

    def _build_process_group(self) -> QGroupBox:
        box = QGroupBox("① 選定程序")
        lay = QVBoxLayout(box)

        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("依視窗標題關鍵字過濾（可留空）")
        self.filter_edit.returnPressed.connect(self.refresh_windows)
        refresh_btn = QPushButton("重新整理")
        refresh_btn.clicked.connect(self.refresh_windows)
        attach_btn = QPushButton("選定此程序")
        attach_btn.clicked.connect(self.attach_selected)
        filter_row.addWidget(self.filter_edit)
        filter_row.addWidget(refresh_btn)
        filter_row.addWidget(attach_btn)
        lay.addLayout(filter_row)

        self.proc_table = QTableWidget(0, 4)
        self.proc_table.setHorizontalHeaderLabels(
            ["PID", "記憶體 (MB)", "視窗標題", "程序類別"]
        )
        self.proc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.proc_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.proc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.proc_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        self._set_min_rows(self.proc_table, rows=5, max_rows=8)
        self.proc_table.doubleClicked.connect(self.attach_selected)
        lay.addWidget(self.proc_table)

        self.attached_label = QLabel("尚未選定程序")
        self.attached_label.setStyleSheet("color: #9aa2b8;")
        lay.addWidget(self.attached_label)

        base_row = QHBoxLayout()
        self.base_watch_btn = QPushButton("基底位址加入觀察（int32，可讀寫）")
        self.base_watch_btn.setEnabled(False)
        self.base_watch_btn.clicked.connect(self.add_base_to_watch)
        base_row.addWidget(self.base_watch_btn)
        base_row.addStretch(1)
        lay.addLayout(base_row)
        return box

    def _build_scan_group(self) -> QGroupBox:
        box = QGroupBox("② 搜尋")
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("型別："))
        self.type_combo = QComboBox()
        for vt in VALUE_TYPES.values():
            self.type_combo.addItem(vt.label, vt.key)
        self.type_combo.currentIndexChanged.connect(self._on_value_type_changed)
        row.addWidget(self.type_combo)

        row.addWidget(QLabel("條件："))
        self.scan_combo = QComboBox()
        for key, label in SCAN_TYPE_ORDER:
            self.scan_combo.addItem(label, key)
        self.scan_combo.currentIndexChanged.connect(self._on_scan_type_changed)
        row.addWidget(self.scan_combo)

        row.addWidget(QLabel("數值："))
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("你在遊戲裡看到的數字")
        row.addWidget(self.value_edit, stretch=1)
        lay.addLayout(row)

        btn_row = QHBoxLayout()
        self.first_btn = QPushButton("首次搜尋")
        self.first_btn.clicked.connect(self.do_first_scan)
        self.next_btn = QPushButton("再次搜尋")
        self.next_btn.clicked.connect(self.do_next_scan)
        self.reset_btn = QPushButton("重設搜尋")
        self.reset_btn.clicked.connect(self.reset_scan)
        btn_row.addWidget(self.first_btn)
        btn_row.addWidget(self.next_btn)
        btn_row.addWidget(self.reset_btn)
        lay.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        self.scan_status = QLabel("尚未搜尋")
        self.scan_status.setStyleSheet("color: #9aa2b8;")
        lay.addWidget(self.scan_status)
        return box

    def _build_string_group(self) -> QGroupBox:
        box = QGroupBox("②-B 字串搜尋（找帳號 / 名稱等文字在記憶體的位址）")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("文字："))
        self.str_edit = QLineEdit()
        self.str_edit.setPlaceholderText("例如 fred19970311")
        self.str_edit.returnPressed.connect(self.do_string_scan)
        row.addWidget(self.str_edit, stretch=1)
        lay.addLayout(row)

        opt = QHBoxLayout()
        opt.addWidget(QLabel("編碼："))
        self.str_enc_chks: dict[str, QCheckBox] = {}
        for key, label in STRING_ENCODINGS.items():
            chk = QCheckBox(label)
            # 預設勾 UTF-16 與 ASCII/ANSI（遊戲最常見）
            chk.setChecked(key in ("utf-16-le", "mbcs"))
            self.str_enc_chks[key] = chk
            opt.addWidget(chk)
        self.str_btn = QPushButton("搜尋字串")
        self.str_btn.clicked.connect(self.do_string_scan)
        self.str_next_btn = QPushButton("再次搜尋")
        self.str_next_btn.setEnabled(False)
        self.str_next_btn.clicked.connect(self.do_string_next_scan)
        opt.addStretch(1)
        opt.addWidget(self.str_btn)
        opt.addWidget(self.str_next_btn)
        lay.addLayout(opt)
        lay.addWidget(
            QLabel(
                "結果會出現在下方「③ 搜尋結果」，可『加入觀察』。\n"
                "要縮小範圍：在遊戲裡把該文字改成新的 → 輸入新文字按「再次搜尋」，"
                "只會留下真正在改的那個位址。"
            )
        )
        return box

    def _build_results_group(self) -> QGroupBox:
        box = QGroupBox("③ 搜尋結果")
        lay = QVBoxLayout(box)
        self.result_table = QTableWidget(0, 2)
        self.result_table.setHorizontalHeaderLabels(["位址", "值"])
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self._set_min_rows(self.result_table, rows=5)
        self.result_table.doubleClicked.connect(self.add_selected_to_watch)
        lay.addWidget(self.result_table)

        add_btn = QPushButton("加入觀察（可多選；也可雙擊）")
        add_btn.clicked.connect(self.add_selected_to_watch)
        lay.addWidget(add_btn)
        return box

    def _build_watch_group(self) -> QGroupBox:
        box = QGroupBox("④ 我的觀察位址（即時值）")
        lay = QVBoxLayout(box)
        self.watch_table = QTableWidget(0, 3)
        self.watch_table.setHorizontalHeaderLabels(["位址", "型別", "目前值"])
        self.watch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.watch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.watch_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self._set_min_rows(self.watch_table, rows=5)
        lay.addWidget(self.watch_table)

        btn_row = QHBoxLayout()
        manual_btn = QPushButton("手動加入位址…")
        manual_btn.clicked.connect(self.add_manual_address)
        write_btn = QPushButton("寫入數值…")
        write_btn.clicked.connect(self.write_selected)
        remove_btn = QPushButton("移除")
        remove_btn.clicked.connect(self.remove_selected_watch)
        btn_row.addWidget(manual_btn)
        btn_row.addWidget(write_btn)
        btn_row.addWidget(remove_btn)
        lay.addLayout(btn_row)
        return box

    # ------------------------------------------------------------------
    # 程序清單
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        keyword = self.filter_edit.text().strip() or None
        self._windows = win.enumerate_windows(title_contains=keyword)
        self.proc_table.setRowCount(len(self._windows))
        for row, w in enumerate(self._windows):
            mb = working_set_mb(w.pid)
            mb_text = f"{mb:.1f}" if mb is not None else "—"
            self.proc_table.setItem(row, 0, QTableWidgetItem(str(w.pid)))
            self.proc_table.setItem(row, 1, QTableWidgetItem(mb_text))
            self.proc_table.setItem(row, 2, QTableWidgetItem(w.title))
            self.proc_table.setItem(row, 3, QTableWidgetItem(w.class_name))
        self.scan_status.setText(f"找到 {len(self._windows)} 個視窗")

    def attach_selected(self) -> None:
        rows = self.proc_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在清單中選一個視窗。")
            return
        w = self._windows[rows[0].row()]
        try:
            self._scanner.open(w.pid)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "開啟失敗", str(exc))
            self._update_enabled()
            return
        bits = 32 if self._scanner.pointer_size == 4 else 64
        write_note = "" if self._scanner.can_write else "（唯讀，無法寫入）"
        base = self._scanner.main_module_base()
        self._base_addr = base or 0
        if base:
            v = self._scanner.read_value(base, VALUE_TYPES["int32"])
            base_note = f"　｜　基底位址 0x{base:X}（int32 值 = {v}）"
            self.base_watch_btn.setEnabled(True)
        else:
            base_note = ""
            self.base_watch_btn.setEnabled(False)
        self.attached_label.setText(
            f"已選定：PID {w.pid}（{bits} 位元）— {w.title}{write_note}{base_note}"
        )
        self.attached_label.setStyleSheet("color: #33c17f;")
        self._clear_results()
        self.scan_status.setText("已選定程序，可開始首次搜尋。")
        self._update_enabled()

    # ------------------------------------------------------------------
    # 搜尋條件 UI 連動
    # ------------------------------------------------------------------
    def _on_value_type_changed(self) -> None:
        # 換型別代表要重新搜尋（不同大小的候選不相容）。
        if self._scanner.has_results:
            self._scanner.reset()
            self._clear_results()
            self.scan_status.setText("已切換型別，請重新首次搜尋。")
        self._update_enabled()

    def _on_scan_type_changed(self) -> None:
        key = self.scan_combo.currentData()
        self.value_edit.setEnabled(key in VALUE_REQUIRED)

    def _current_vt(self):
        return VALUE_TYPES[self.type_combo.currentData()]

    def _parse_value(self, vt):
        """讀取並解析輸入框的數值；失敗回傳 (None, 錯誤訊息)。"""
        text = self.value_edit.text().strip()
        if not text:
            return None, "請先輸入要搜尋的數值。"
        try:
            if vt.is_float:
                return float(text), None
            return int(text, 0), None  # 支援 0x 十六進位
        except ValueError:
            return None, f"「{text}」不是有效的{'浮點' if vt.is_float else '整數'}數值。"

    # ------------------------------------------------------------------
    # 掃描動作
    # ------------------------------------------------------------------
    def do_first_scan(self) -> None:
        if not self._scanner.attached:
            QMessageBox.information(self, "提示", "請先選定程序。")
            return
        vt = self._current_vt()
        scan_type = self.scan_combo.currentData()
        value = None
        if scan_type in VALUE_REQUIRED:
            value, err = self._parse_value(vt)
            if err:
                QMessageBox.warning(self, "數值錯誤", err)
                return
        elif scan_type != "unknown":
            QMessageBox.warning(
                self,
                "條件不適用",
                "首次搜尋只能用「等於 / 大於 / 小於 / 未知初始值」。\n"
                "「增加 / 減少 / 改變」要先有一輪結果才能比較。",
            )
            return
        self._run_scan(
            lambda progress: self._scanner.first_scan(
                vt, scan_type, value, False, progress  # 一律搜尋全部記憶體（不分可否寫入）
            ),
            f"首次搜尋（{SCAN_TYPE_LABEL.get(scan_type, scan_type)}）…",
        )

    def do_next_scan(self) -> None:
        if not self._scanner.has_results:
            QMessageBox.information(self, "提示", "請先做一次首次搜尋。")
            return
        vt = self._current_vt()
        scan_type = self.scan_combo.currentData()
        if scan_type == "unknown":
            QMessageBox.warning(
                self, "條件不適用", "「未知初始值」只用於首次搜尋。"
            )
            return
        value = None
        if scan_type in VALUE_REQUIRED:
            value, err = self._parse_value(vt)
            if err:
                QMessageBox.warning(self, "數值錯誤", err)
                return
        self._run_scan(
            lambda progress: self._scanner.next_scan(scan_type, value, progress),
            f"再次搜尋（{SCAN_TYPE_LABEL.get(scan_type, scan_type)}）…",
        )

    def _run_scan(self, fn, status_text: str) -> None:
        self._set_scanning(True)
        self.scan_status.setText(status_text)
        self.progress.setValue(0)
        self._worker = ScanWorker(fn)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.done.connect(self._on_scan_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _on_scan_done(self, count: int) -> None:
        self.progress.setValue(100)
        self._set_scanning(False)
        self._populate_results(count)

    def _on_scan_failed(self, msg: str) -> None:
        self._set_scanning(False)
        self.progress.setValue(0)
        self.scan_status.setText("搜尋失敗")
        QMessageBox.critical(self, "搜尋失敗", msg)

    def reset_scan(self) -> None:
        self._scanner.reset()
        self._clear_results()
        self._str_result_meta = {}
        self.scan_status.setText("已重設，可重新首次搜尋。")
        self._update_enabled()

    # ------------------------------------------------------------------
    # 字串搜尋
    # ------------------------------------------------------------------
    def do_string_scan(self) -> None:
        if not self._scanner.attached:
            QMessageBox.information(self, "提示", "請先選定程序。")
            return
        text = self.str_edit.text()
        if not text:
            QMessageBox.warning(self, "缺少文字", "請輸入要搜尋的文字。")
            return
        encs = [k for k, chk in self.str_enc_chks.items() if chk.isChecked()]
        if not encs:
            QMessageBox.warning(self, "缺少編碼", "請至少勾選一種編碼。")
            return
        self._scanner.reset()  # 字串搜尋獨立於數值搜尋，先清掉數值狀態
        self._clear_results()

        def job(progress):
            # 一律搜尋全部記憶體（不分可否寫入）
            self._str_hits = self._scanner.search_string(text, encs, False, progress)
            return len(self._str_hits)

        self._set_scanning(True)
        self.scan_status.setText(f"搜尋字串「{text}」…")
        self.progress.setValue(0)
        self._worker = ScanWorker(job)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.done.connect(self._on_string_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def do_string_next_scan(self) -> None:
        """字串再次搜尋：在既有命中中，只保留現在內容仍等於輸入文字的位址。"""
        if not self._scanner.attached:
            QMessageBox.information(self, "提示", "請先選定程序。")
            return
        if not self._str_hits:
            QMessageBox.information(self, "提示", "請先做一次字串搜尋。")
            return
        text = self.str_edit.text()
        if not text:
            QMessageBox.warning(
                self, "缺少文字",
                "請輸入要比對的新文字（可先在遊戲裡把該欄位改成新文字）。",
            )
            return
        prev = list(self._str_hits)

        def job(progress):
            self._str_hits = self._scanner.filter_string_hits(prev, text, progress)
            return len(self._str_hits)

        self._set_scanning(True)
        self.scan_status.setText(f"再次搜尋字串「{text}」…（在 {len(prev)} 筆中縮小）")
        self.progress.setValue(0)
        self._worker = ScanWorker(job)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.done.connect(self._on_string_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._worker.start()

    def _on_string_done(self, count: int) -> None:
        self.progress.setValue(100)
        self._set_scanning(False)
        self._populate_string_results(count)

    def _populate_string_results(self, count: int) -> None:
        shown = self._str_hits[:RESULT_DISPLAY_LIMIT]
        self._str_result_meta = {}
        self.result_table.setRowCount(len(shown))
        for i, (addr, enc, blen) in enumerate(shown):
            s = self._scanner.read_string(addr, blen, enc) or ""
            addr_item = QTableWidgetItem(f"0x{addr:X}")
            addr_item.setData(Qt.UserRole, addr)
            self.result_table.setItem(i, 0, addr_item)
            self.result_table.setItem(
                i, 1, QTableWidgetItem(f"「{s}」（{STRING_ENCODINGS[enc]}）")
            )
            self._str_result_meta[addr] = (enc, blen)
        note = "" if count <= len(shown) else f"（太多，只顯示前 {len(shown)} 筆）"
        self.scan_status.setText(f"字串搜尋找到 {count} 筆{note}")
        self._update_enabled()

    # ------------------------------------------------------------------
    # 結果顯示
    # ------------------------------------------------------------------
    def _populate_results(self, count: int) -> None:
        self._str_result_meta = {}  # 這是數值結果，非字串
        self._str_hits = []  # 數值結果 → 字串命中作廢，停用字串『再次搜尋』
        rows = self._scanner.results(limit=RESULT_DISPLAY_LIMIT)
        self.result_table.setRowCount(len(rows))
        for i, (addr, value) in enumerate(rows):
            addr_item = QTableWidgetItem(f"0x{addr:X}")
            addr_item.setData(Qt.UserRole, addr)
            self.result_table.setItem(i, 0, addr_item)
            self.result_table.setItem(i, 1, QTableWidgetItem(str(value)))
        if count > len(rows):
            note = f"（候選太多，只顯示前 {len(rows)} 筆）"
        else:
            note = ""
        self.scan_status.setText(f"目前候選：{count} 筆{note}")
        self._update_enabled()

    def _clear_results(self) -> None:
        self.result_table.setRowCount(0)
        self._str_hits = []  # 清結果同時作廢字串命中，避免『再次搜尋』誤用舊資料

    # ------------------------------------------------------------------
    # 觀察清單
    # ------------------------------------------------------------------
    def add_selected_to_watch(self) -> None:
        vt = self._current_vt()
        rows = self.result_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在結果中選取要觀察的列。")
            return
        for idx in rows:
            item = self.result_table.item(idx.row(), 0)
            addr = item.data(Qt.UserRole)
            if addr in self._str_result_meta:
                enc, blen = self._str_result_meta[addr]
                self._add_watch_string(addr, enc, blen)
            else:
                self._add_watch(addr, vt)
        self._rebuild_watch_table()

    def add_manual_address(self) -> None:
        text, ok = QInputDialog.getText(
            self, "手動加入位址", "輸入記憶體位址（可用 0x 十六進位）："
        )
        if not ok or not text.strip():
            return
        try:
            addr = int(text.strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "位址錯誤", f"「{text}」不是有效的位址。")
            return
        self._add_watch(addr, self._current_vt())
        self._rebuild_watch_table()

    def add_base_to_watch(self) -> None:
        if not self._scanner.attached or not self._base_addr:
            QMessageBox.information(self, "提示", "請先選定程序。")
            return
        self._add_watch(self._base_addr, VALUE_TYPES["int32"])
        self._rebuild_watch_table()

    def _add_watch(self, addr: int, vt) -> None:
        for entry in self._watch:
            if entry["addr"] == addr and entry.get("vt") and entry["vt"].key == vt.key:
                return  # 避免重複
        self._watch.append({"addr": addr, "vt": vt})

    def _add_watch_string(self, addr: int, enc: str, blen: int) -> None:
        for entry in self._watch:
            if entry["addr"] == addr and entry.get("str_enc"):
                return
        self._watch.append({"addr": addr, "vt": None, "str_enc": enc, "str_len": blen})

    def remove_selected_watch(self) -> None:
        rows = sorted(
            (idx.row() for idx in self.watch_table.selectionModel().selectedRows()),
            reverse=True,
        )
        for row in rows:
            if 0 <= row < len(self._watch):
                del self._watch[row]
        self._rebuild_watch_table()

    def write_selected(self) -> None:
        if not self._scanner.attached:
            return
        rows = self.watch_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在觀察清單選一列。")
            return
        entry = self._watch[rows[0].row()]
        if entry.get("str_enc"):
            self._write_string_entry(entry)
            return
        vt = entry["vt"]
        text, ok = QInputDialog.getText(
            self,
            "寫入數值",
            f"對位址 0x{entry['addr']:X} 寫入新的 {vt.label}：",
        )
        if not ok or not text.strip():
            return
        try:
            value = float(text) if vt.is_float else int(text, 0)
        except ValueError:
            QMessageBox.warning(self, "數值錯誤", f"「{text}」不是有效數值。")
            return
        try:
            self._scanner.write_value(entry["addr"], vt, value)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "寫入失敗", str(exc))
            return
        self._refresh_watch_values()

    def _write_string_entry(self, entry: dict) -> None:
        enc = entry["str_enc"]
        cur = self._scanner.read_string(entry["addr"], entry["str_len"], enc) or ""
        text, ok = QInputDialog.getText(
            self,
            "寫入字串",
            f"對位址 0x{entry['addr']:X} 寫入新字串（{STRING_ENCODINGS[enc]}）：",
            text=cur,
        )
        if not ok:
            return
        try:
            new_bytes = text.encode(enc)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "編碼錯誤",
                f"這段文字無法用 {STRING_ENCODINGS[enc]} 編碼：{exc}",
            )
            return
        # 比原長度長 → 會覆蓋相鄰記憶體，先確認
        null_terminate = True
        if len(new_bytes) > entry["str_len"]:
            reply = QMessageBox.question(
                self,
                "字串較長",
                f"新字串 {len(new_bytes)} 位元組，比原本 {entry['str_len']} 位元組長，"
                "寫入會覆蓋後面相鄰的記憶體，可能造成遊戲異常。\n確定要寫入嗎？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            null_terminate = False  # 已在冒險，不再多寫 null 免得再多覆蓋一格
        try:
            self._scanner.write_string(entry["addr"], text, enc, null_terminate)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "寫入失敗", str(exc))
            return
        self._refresh_watch_values()

    def _rebuild_watch_table(self) -> None:
        self.watch_table.setRowCount(len(self._watch))
        for row, entry in enumerate(self._watch):
            addr_item = QTableWidgetItem(f"0x{entry['addr']:X}")
            self.watch_table.setItem(row, 0, addr_item)
            if entry.get("str_enc"):
                type_label = f"字串（{STRING_ENCODINGS[entry['str_enc']]}）"
            else:
                type_label = entry["vt"].label
            self.watch_table.setItem(row, 1, QTableWidgetItem(type_label))
            self.watch_table.setItem(row, 2, QTableWidgetItem("…"))
        self._refresh_watch_values()

    def _refresh_watch_values(self) -> None:
        if not self._scanner.attached or not self._watch:
            return
        for row, entry in enumerate(self._watch):
            if row >= self.watch_table.rowCount():
                break
            if entry.get("str_enc"):
                s = self._scanner.read_string(
                    entry["addr"], entry["str_len"], entry["str_enc"]
                )
                text = "讀取失敗" if s is None else s
            else:
                value = self._scanner.read_value(entry["addr"], entry["vt"])
                text = "讀取失敗" if value is None else str(value)
            self.watch_table.setItem(row, 2, QTableWidgetItem(text))

    def _refresh_live(self) -> None:
        self._refresh_watch_values()

    # ==================================================================
    # 狀態
    # ==================================================================
    def _set_scanning(self, scanning: bool) -> None:
        # 掃描期間暫停即時刷新，避免與背景執行緒同時動用控制代碼。
        if scanning:
            self._watch_timer.stop()
        else:
            self._watch_timer.start()
        attached = self._scanner.attached
        self.first_btn.setEnabled(not scanning and attached)
        self.next_btn.setEnabled(not scanning and self._scanner.has_results)
        self.reset_btn.setEnabled(not scanning)
        self.type_combo.setEnabled(not scanning)
        self.str_btn.setEnabled(not scanning and attached)
        self.str_next_btn.setEnabled(not scanning and attached and bool(self._str_hits))

    def _update_enabled(self) -> None:
        attached = self._scanner.attached
        self.first_btn.setEnabled(attached)
        self.next_btn.setEnabled(attached and self._scanner.has_results)
        self.str_btn.setEnabled(attached)
        self.str_next_btn.setEnabled(attached and bool(self._str_hits))

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._watch_timer.stop()
        if self._worker and self._worker.isRunning():
            self._worker.wait(5000)
        self._scanner.close()


# 條件 key -> 顯示名稱（含未知初始值），給狀態列訊息用。
SCAN_TYPE_LABEL = dict(SCAN_TYPE_ORDER)
