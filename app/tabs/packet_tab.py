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

import datetime
import pathlib

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app import theme
from app.core import injector
from app.core import window as win
from app.tabs.base_tab import GROUP_DEV, BaseTab

MAX_ROWS = 500  # 封包表最多保留列數


class PacketTab(BaseTab):
    TAB_TITLE = "封包 / 登入攔截"
    GROUP = GROUP_DEV
    ORDER = 60

    def build_ui(self) -> None:
        self._cap: injector.SendCapture | None = None
        self._packets: list[injector.Packet] = []
        self._windows: list[win.WindowInfo] = []
        self._group_mode = False
        self._baseline_sigs: set[tuple[int, ...]] = set()

        root = QVBoxLayout(self)

        root.addWidget(
            QLabel(
                "掛住遊戲的 send，攔截送出的封包，找出「送登入封包的那段程式」(=登入函式候選)。\n"
                "① 選遊戲程序 → 開始攔截 → ② 回遊戲登入 → ③ 下方看封包的『呼叫鏈』與亂度。"
            )
        )

        # ⚠ 三個區塊原本是直接疊在 QVBoxLayout 裡，各自又有「最小高度」，
        #   三段最小高度加起來超過視窗高度時，Qt 不會縮 —— 而是把最下面那段
        #   擠到看不見（使用者回報「送出的封包看不到最下面」）。
        #   改用垂直分割器：空間不夠時三段一起縮，而且可以自己拖拉分配。
        split = QSplitter(Qt.Vertical)
        split.addWidget(self._build_process_group())
        split.addWidget(self._build_packet_group())
        split.addWidget(self._build_detail_group())
        split.setStretchFactor(0, 0)      # 程序清單：固定感，不搶空間
        split.setStretchFactor(1, 3)      # 封包表：多出來的空間都給它
        split.setStretchFactor(2, 1)
        split.setChildrenCollapsible(False)
        root.addWidget(split, stretch=1)

        self.status = QLabel("就緒")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_MUT};")
        root.addWidget(self.status)

        # 即時輪詢環狀緩衝
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._poll)

        self._checked_avail = False
        self.refresh_windows()

    def on_show(self) -> None:
        """第一次真的切到這個分頁時才檢查注入套件在不在。

        ⚠⚠ `injector.available()` 會 import pymem / keystone / pefile
          （實測合計約 430ms，keystone 還要載原生 DLL）。分頁是**開程式時
          全部一起建**的，放在 build_ui() 等於每次啟動都多等這段 ——
          而這個分頁是開發用的，多數時候根本不會打開。
        """
        if self._checked_avail:
            return
        self._checked_avail = True
        ok, msg = injector.available()
        if not ok:
            self._set_enabled_capture(False)
            self.start_btn.setEnabled(False)
            self.status.setText("⚠ 注入功能不可用：" + msg)

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
        # 保底高度：至少看得到約 5~7 個程序。原本只設 maximum，視窗一變矮就會被
        # 下方（封包表、內容）擠到只剩一列。列高約 30px。
        # ⚠ 最小值別開太大：視窗一矮，三段的最小高度加起來就超過可用高度，
        #   最下面那段會被擠到看不見。分割器可以拖，所以這裡只要保底就好。
        self.proc_table.setMinimumHeight(90)
        self.proc_table.setMaximumHeight(280)
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

        # 分組控制列：內容是加密的分不出來，能區分「攻擊/移動/放技能」的是呼叫鏈
        # （從哪段程式送出）。把相同呼叫鏈的封包摺成一列，幾百包就變幾種來源。
        ctrl = QHBoxLayout()
        self.group_chk = QCheckBox("依呼叫鏈分組（找動作函式用）")
        self.group_chk.setToolTip(
            "內容加密、分不出來；能區分動作的是『從哪段程式送出』= 呼叫鏈。\n"
            "勾選後把相同呼叫鏈的封包併成一列，幾百包就變成幾種來源。"
        )
        self.group_chk.toggled.connect(self._on_group_toggled)
        ctrl.addWidget(self.group_chk)
        ctrl.addWidget(QLabel("比對深度"))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 12)
        self.depth_spin.setValue(4)
        self.depth_spin.setEnabled(False)
        self.depth_spin.setToolTip(
            "拿呼叫鏈前幾層當分組依據。太小會全部併成一組，太大會被堆疊雜訊拆碎。"
        )
        self.depth_spin.valueChanged.connect(self._on_depth_changed)
        ctrl.addWidget(self.depth_spin)
        self.baseline_btn = QPushButton("設為基線")
        self.baseline_btn.setEnabled(False)
        self.baseline_btn.setToolTip(
            "把目前所有來源當成背景雜訊；之後新冒出來的來源會標紅置頂。\n"
            "用法：先站著不動→按這顆→回遊戲只做一個動作（如攻擊）→看標紅那列。"
        )
        self.baseline_btn.clicked.connect(self.set_baseline)
        self.clearbase_btn = QPushButton("清除基線")
        self.clearbase_btn.setEnabled(False)
        self.clearbase_btn.clicked.connect(self.clear_baseline)
        ctrl.addWidget(self.baseline_btn)
        ctrl.addWidget(self.clearbase_btn)
        ctrl.addStretch(1)
        lay.addLayout(ctrl)

        # ★ 第二列：把「一直重複、看不出順序、沒辦法整份複製」這三件事解決掉。
        #   狂按同一個鍵時會攔到幾百包幾乎一樣的東西，逐包列出來根本讀不了。
        ctrl2 = QHBoxLayout()
        self.fold_chk = QCheckBox("摺疊連續重複")
        self.fold_chk.setChecked(True)
        self.fold_chk.setToolTip(
            "連續且相同的封包併成一列標「×N」，順序保留。")
        self.fold_chk.toggled.connect(self._rebuild_table)
        ctrl2.addWidget(self.fold_chk)
        self.copy_btn = QPushButton("複製全部")
        self.copy_btn.setToolTip("把整份攔截結果（含每層參數）複製到剪貼簿。")
        self.copy_btn.clicked.connect(self.copy_report)
        self.save_btn = QPushButton("匯出成檔案")
        self.save_btn.setToolTip(
            "存成純文字檔，路徑會顯示在下方狀態列 —— 檔案比剪貼簿好給人看。")
        self.save_btn.clicked.connect(self.save_report)
        ctrl2.addWidget(self.copy_btn)
        ctrl2.addWidget(self.save_btn)
        ctrl2.addStretch(1)
        lay.addLayout(ctrl2)

        self.pkt_table = QTableWidget(0, 5)
        self.pkt_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pkt_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.pkt_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pkt_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        # 保底高度：即使視窗偏矮，也至少看得到約 4 個封包（分割器可再拖大）。
        self.pkt_table.setMinimumHeight(120)
        self.pkt_table.itemSelectionChanged.connect(self._show_detail)
        self._apply_headers()
        lay.addWidget(self.pkt_table)
        return box

    def _build_detail_group(self) -> QGroupBox:
        box = QGroupBox("③ 選取封包的內容（hex）")
        lay = QVBoxLayout(box)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setStyleSheet("font-family: Consolas, monospace;")
        self.detail.setMinimumHeight(70)
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
        # 保險：萬一按到這裡時還沒檢查過套件（on_show 沒被呼叫到），現在補檢查
        self.on_show()
        ok, msg = injector.available()
        if not ok:
            QMessageBox.warning(self, "注入功能不可用", msg)
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
        if not new:
            return
        self._packets.extend(new)
        trimmed = False
        if len(self._packets) > MAX_ROWS:  # 超過上限裁掉最舊的
            self._packets = self._packets[-MAX_ROWS:]
            trimmed = True
        if self._group_mode or self.fold_chk.isChecked() or trimmed:
            # 摺疊要看「跟前一包是不是同一種」，沒辦法只補最後一列，整表重畫。
            # 500 列的重畫實測遠比每秒攔到的封包量便宜，不會拖慢畫面。
            self._rebuild_table()
        else:
            for pkt in new:
                r = self.pkt_table.rowCount()
                self.pkt_table.insertRow(r)
                self._fill_row(r, pkt)
            self.pkt_table.scrollToBottom()

    # ---- 分組 / 基線（把幾百包摺成幾種來源，標出新來源 = 動作函式）--------
    def _apply_headers(self) -> None:
        """依目前模式設定表頭；逐包與分組共用同一張 5 欄表。"""
        if self._group_mode:
            self.pkt_table.setHorizontalHeaderLabels(
                ["狀態", "封包數", "長度", "呼叫鏈（動作函式候選）", "內容範例"]
            )
        else:
            self.pkt_table.setHorizontalHeaderLabels(
                ["#", "重複", "長度", "呼叫鏈（動作函式候選）", "內容預覽"]
            )

    def _on_group_toggled(self, checked: bool) -> None:
        self._group_mode = checked
        self.depth_spin.setEnabled(checked)
        self.baseline_btn.setEnabled(checked)
        self.clearbase_btn.setEnabled(checked)
        self._apply_headers()
        # 分組模式會把整份打散重排，跟「摺疊」是兩種看法，不要同時開
        self.fold_chk.setEnabled(not checked)
        self._rebuild_table()
        if checked:
            self.status.setText(
                "分組模式：相同呼叫鏈併成一列（**不保順序**）。做法→先站著不動幾秒，"
                "按「設為基線」，再回遊戲只做一個動作，標紅置頂那列就是它的來源。"
                "　想看先後順序請取消勾選，改用「摺疊連續重複」。"
            )

    def _on_depth_changed(self, _value: int) -> None:
        if self._baseline_sigs:  # 深度變了，舊基線的簽章對不上，得清掉
            self._baseline_sigs.clear()
            self.status.setText("比對深度已變更，基線已清除，請重新按「設為基線」。")
        if self._group_mode:
            self._rebuild_grouped()

    def set_baseline(self) -> None:
        """把目前所有來源記為基線；之後不在基線內的來源會標紅置頂。"""
        self._baseline_sigs = {self._signature(p) for p in self._packets}
        self._rebuild_grouped()
        self.status.setText(
            f"已把目前 {len(self._baseline_sigs)} 種來源設為基線。"
            "現在回遊戲只做一個動作（例如攻擊同一隻怪），新來源會標紅置頂。"
        )

    def clear_baseline(self) -> None:
        self._baseline_sigs.clear()
        if self._group_mode:
            self._rebuild_grouped()
        self.status.setText("已清除基線。")

    def _signature(self, pkt: injector.Packet) -> tuple[int, ...]:
        """分組依據：呼叫鏈的前 N 層（N = 比對深度）。"""
        return tuple(pkt.call_chain[: self.depth_spin.value()])

    def _rebuild_grouped(self) -> None:
        groups: dict[tuple[int, ...], dict] = {}
        for pkt in self._packets:
            sig = self._signature(pkt)
            g = groups.get(sig)
            if g is None:
                groups[sig] = g = {"count": 0, "lengths": set(), "rep": pkt}
            g["count"] += 1
            g["lengths"].add(pkt.length)
            g["rep"] = pkt  # 以最新一包當代表
        has_base = bool(self._baseline_sigs)
        rows = [
            (has_base and sig not in self._baseline_sigs, g["count"], sig, g)
            for sig, g in groups.items()
        ]
        rows.sort(key=lambda x: (not x[0], -x[1]))  # 有基線時新來源置頂，其餘依數量

        self.pkt_table.setRowCount(len(rows))
        for r, (is_new, count, sig, g) in enumerate(rows):
            rep = g["rep"]
            status = "● 新" if is_new else ("基線" if has_base else "—")
            st_item = QTableWidgetItem(status)
            st_item.setData(Qt.UserRole, rep.seq)
            self.pkt_table.setItem(r, 0, st_item)
            self.pkt_table.setItem(r, 1, QTableWidgetItem(str(count)))
            lengths = ",".join(str(x) for x in sorted(g["lengths"]))
            self.pkt_table.setItem(r, 2, QTableWidgetItem(lengths))
            chain = " ← ".join(f"0x{a:X}" for a in sig) or "—"
            self.pkt_table.setItem(r, 3, QTableWidgetItem(chain))
            self.pkt_table.setItem(r, 4, QTableWidgetItem(rep.data[:24].hex(" ")))
            if is_new:
                for c in range(5):
                    it = self.pkt_table.item(r, c)
                    if it:
                        it.setForeground(Qt.red)

    # ---- 摺疊連續重複（保留順序）-----------------------------------------
    @staticmethod
    def _fold_key(pkt: injector.Packet) -> tuple:
        """兩包算不算「同一種」：來源相同、長度相同就算。

        ⚠ 不能拿內容比 —— 封包有混淆，同一個動作每次的 bytes 都不一樣，
          用內容比等於完全摺不起來。
        """
        return (tuple(pkt.call_chain[:6]), pkt.length)

    def _folded(self) -> list[list[injector.Packet]]:
        """把**連續**的同種封包併成一段。順序保留，這是跟分組模式的差別。"""
        out: list[list[injector.Packet]] = []
        for p in self._packets:
            if out and self._fold_key(out[-1][0]) == self._fold_key(p):
                out[-1].append(p)
            else:
                out.append([p])
        return out

    def _rebuild_table(self) -> None:
        if self._group_mode:
            self._rebuild_grouped()
            return
        runs = (self._folded() if self.fold_chk.isChecked()
                else [[p] for p in self._packets])
        self.pkt_table.setRowCount(len(runs))
        for r, run in enumerate(runs):
            self._fill_row(r, run[0], len(run))
        self.pkt_table.scrollToBottom()

    def _fill_row(self, r: int, pkt: injector.Packet, count: int = 1) -> None:
        chain = " ← ".join(f"0x{a:X}" for a in pkt.call_chain[:6]) or "—"
        preview = pkt.data[:24].hex(" ")
        seq_item = QTableWidgetItem(str(pkt.seq))
        seq_item.setData(Qt.UserRole, pkt.seq)
        self.pkt_table.setItem(r, 0, seq_item)
        self.pkt_table.setItem(r, 1, QTableWidgetItem(
            f"×{count}" if count > 1 else ""))
        self.pkt_table.setItem(r, 2, QTableWidgetItem(str(pkt.length)))
        self.pkt_table.setItem(r, 3, QTableWidgetItem(chain))
        self.pkt_table.setItem(r, 4, QTableWidgetItem(preview))

    # ---- 匯出（整份給人看）------------------------------------------------
    def _report(self) -> str:
        """整份攔截結果的純文字版：**照順序**、連續重複摺起來、附各層參數。

        ★ 重點是「各層收到的參數」而不是 hex —— 封包內容混淆過看不出東西，
          參數才看得懂（例如施放那層直接就是 技能ID、目標實體ID）。
        """
        runs = self._folded()
        head = [
            f"封包攔截記錄　{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            f"共 {len(self._packets)} 包，摺疊後 {len(runs)} 個步驟"
            "（連續且來源+長度相同的算同一步）",
            "",
        ]
        body = []
        for i, run in enumerate(runs, 1):
            p = run[0]
            rng = (f"#{run[0].seq}" if len(run) == 1
                   else f"#{run[0].seq}~{run[-1].seq}")
            body.append(f"── 步驟 {i}　{rng}　×{len(run)}　長度 {p.length}"
                        f"　亂度 {p.entropy:.2f}/8")
            for addr, args in zip(p.frames, p.args):
                if not (injector.CODE_LO <= addr < injector.CODE_HI):
                    continue
                shown = [f"0x{v:X}" if v > 9 else str(v) for v in args]
                body.append(f"     0x{addr:X}　參數 ({', '.join(shown)})")
            body.append(f"     內容 {p.data[:32].hex(' ')}")
            body.append("")
        return "\n".join(head + body)

    def copy_report(self) -> None:
        if not self._packets:
            self.status.setText("還沒攔到任何封包。")
            return
        text = self._report()
        QGuiApplication.clipboard().setText(text)
        self.status.setText(
            f"已複製整份記錄到剪貼簿（{len(self._packets)} 包，"
            f"{len(text)} 個字）。")

    def save_report(self) -> None:
        if not self._packets:
            self.status.setText("還沒攔到任何封包。")
            return
        name = f"封包記錄_{datetime.datetime.now():%m%d_%H%M%S}.txt"
        path = pathlib.Path.cwd() / name
        try:
            path.write_text(self._report(), encoding="utf-8")
        except OSError as exc:
            self.status.setText(f"存檔失敗：{exc}")
            return
        self.status.setText(f"已存檔：{path}")

    def _show_detail(self) -> None:
        rows = self.pkt_table.selectionModel().selectedRows()
        if not rows:
            return
        item0 = self.pkt_table.item(rows[0].row(), 0)
        if item0 is None:
            return
        seq = item0.data(Qt.UserRole)
        pkt = next((p for p in self._packets if p.seq == seq), None)
        if not pkt:
            return
        # ★ 呼叫鏈每一層的**參數**也一起列出來。
        #   線上封包是 16 bytes 一塊混淆過的，從 hex 看不出內容；
        #   但攔截時已經把各層函式收到的參數記下來了（見 app/core/injector.py），
        #   那才是真正看得懂的東西 —— 例如施放技能那層會直接顯示
        #   (技能ID, 目標實體ID, …)。以前只顯示位址，等於把最有用的資訊丟掉。
        lines = []
        for addr, args in zip(pkt.frames, pkt.args):
            if not (injector.CODE_LO <= addr < injector.CODE_HI):
                continue
            shown = [f"0x{v:X}" if v > 9 else str(v) for v in args]
            lines.append(f"    0x{addr:X}　參數 ({', '.join(shown)})")
        chain = "\n".join(lines) or "    （無）"
        verdict = "疑似加密/壓縮" if pkt.entropy > 7.5 else "疑似明文/輕度混淆"
        note = ""
        if self._group_mode:
            sig = self._signature(pkt)
            n = sum(1 for p in self._packets if self._signature(p) == sig)
            note = f"（分組模式：此來源在目前緩衝共 {n} 包，以下顯示其中一包代表）\n"
        self.detail.setPlainText(
            note
            + f"封包 #{pkt.seq}　長度 {pkt.length}　亂度 {pkt.entropy:.2f}/8 → {verdict}\n"
            f"直接呼叫者(send wrapper)：0x{pkt.caller:X}\n"
            "呼叫鏈與各層收到的參數（動作/登入函式候選在較上層）：\n"
            f"{chain}\n"
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
