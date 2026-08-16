"""自動登入分頁。

勾幾個帳號，按一顆「一鍵登入」，剩下的全部自動：

    掃現有分身 → 已登入的跳過 → 沒登入的拿來用 → 不夠就自己開遊戲
    → 切伺服器 → 帳密登入 → 選頻道 → 進入遊戲（第 1 個角色）

全程寫記憶體＋呼叫遊戲自己的函式，**不占鍵盤滑鼠**，遊戲視窗在背景也照做。
實作在 app/game/login.py。

## 為什麼不經過登入機（start.exe）

登入機那排 START／EXIT 按鈕是它自己畫的、**沒有視窗控制代碼**，實測背景點擊
完全無效 —— 要按到只能佔用使用者的實體滑鼠，那是明令禁止的。而登入機做完
更新檢查後也只是用 `./angel.dat` 把遊戲叫起來而已，所以直接開遊戲就好。
唯一多的那道授權合約是一個位元組的閘門，開起來時先寫進去就不會跳。
⚠ 代價：不跑登入機的版本更新檢查，官方改版時要自己開一次登入機更新。

## 為什麼是 QTimer 一拍做一步，不是背景執行緒

每一步中間都要等伺服器（登入一個來回、選頻道要等角色清單、開遊戲要等十幾秒
跑動畫）。用 sleep 會把介面凍住；用背景 QThread 則是打包成 exe 之後會原生當機
的老坑（見 memory 的 packaging-and-release）。所以照「領取每日」分頁的做法：
單次觸發的 QTimer，每一拍檢查一個**真的讀得到的訊號**再決定下一步 ——

    遊戲開好了沒 → 登入畫面物件出現（不是固定等 N 秒；實測要 13 秒）
    到選角色了沒 → 登入畫面物件消失 ＋ 第 1 格讀得到角色名
    進到遊戲了沒 → 視窗標題出現「 - 帳號(伺服器-頻道)」

安全提醒：密碼以 base64 混淆後存進設定檔，這不是真正的加密，拿到設定檔的人可
還原。清單裡的密碼是**明文顯示**（使用者指定），旁邊有人時請留意。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.config import config
from app.core import charname, injector
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import locate, login, move
from app.tabs.base_tab import BaseTab

# ★ 角色固定第 1 個（0 起算 = 0），不給使用者選 —— 使用者指定。
#   會這樣定是因為選到空的角色格時伺服器直接斷線（見 login.enter_game 的檢查）。
CHAR_SLOT = 0
MAX_CLIENTS = 5               # 使用者指定：天使之戀最多開 5 台

(COL_DEL, COL_PICK, COL_ACCT, COL_PWD, COL_PROT,
 COL_NOTE, COL_SRV, COL_CHAN) = range(8)
ACCT_COLS = ("", "選擇", "帳號", "密碼", "保護密碼", "備註", "伺服器", "頻道")
ACCT_ROWS_SHOWN = 5           # 使用者指定：清單至少看得到 5 列
# ⚠ 列高要比預設高一點：那兩個下拉（伺服器／頻道）塞進儲存格之後，
#   用預設列高會把字上下切掉（使用者回報）。加的量用字高算，不寫死像素。
ROW_PAD = 10

STEP_MS = 400                 # 等待時每一拍的間隔
LOGIN_SETTLE_MS = 1500        # 送出帳密 → 等分流清單送到（實測登入來回 < 1 秒）
# 等新開的遊戲跑到登入畫面。單獨開實測約 13 秒；但 2026-08-16 實測「四台分身
# 開著時再開第五台」超過 60 秒（磁碟被搶＋開機期 find_screen 全掃特別慢）。
# 逾時報失敗後那台會留著變成「空的分身」，下一輪會被撿回來用，所以放寬只有好處。
WAIT_BOOT_MS = 120000
WAIT_CHARS_MS = 20000         # 等選角色畫面
WAIT_INGAME_MS = 25000        # 等真的進到遊戲裡


class LoginTab(BaseTab):
    TAB_TITLE = "自動登入"
    ORDER = 10

    def build_ui(self) -> None:
        # 每筆 {"account","password","protect"(都混淆),"selected",
        #       "note","server","channel"}
        self._accounts: list[dict] = []
        self._loading_table = False
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        self._job: dict | None = None
        self._srv_cache: list[tuple[str, int]] = []

        root = QVBoxLayout(self)

        # --- 遊戲設定 ---
        game_box = QGroupBox("遊戲設定")
        game_form = QFormLayout(game_box)
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText(r"例如 D:\AngelsOnline\Angels Online Global\start.exe")
        self.exe_edit.setText(config.get("login.exe_path", ""))
        self.exe_edit.setToolTip(
            "只用來找出遊戲資料夾（開遊戲、讀伺服器清單都在那個資料夾）。\n"
            "填 start.exe 或 angel.dat 都可以。")
        browse_btn = QPushButton("瀏覽…")
        browse_btn.clicked.connect(self._browse_exe)
        exe_row = QHBoxLayout()
        exe_row.addWidget(self.exe_edit)
        exe_row.addWidget(browse_btn)
        game_form.addRow("遊戲執行檔：", exe_row)
        root.addWidget(game_box)

        # --- 帳號 ---
        cred_box = QGroupBox("帳號")
        cred_lay = QVBoxLayout(cred_box)
        form = QFormLayout()
        self.account_edit = QLineEdit()
        form.addRow("帳號：", self.account_edit)
        # ⚠ 使用者指定密碼不要遮起來。
        self.password_edit = QLineEdit()
        form.addRow("密碼：", self.password_edit)
        self.protect_edit = QLineEdit()
        self.protect_edit.setPlaceholderText("進遊戲最後一步要輸入的；沒設定就留空")
        form.addRow("保護密碼：", self.protect_edit)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("自己看的，例如「主帳」「練功號」")
        form.addRow("備註：", self.note_edit)
        cred_lay.addLayout(form)

        add_row = QHBoxLayout()
        add_btn = QPushButton("＋ 新增／更新到帳號清單")
        add_btn.clicked.connect(self._add_account)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        cred_lay.addLayout(add_row)

        cred_lay.addWidget(self._wrap(QLabel(
            "勾「選擇」的帳號會被一鍵登入處理（可以勾很多個）。"
            "密碼、保護密碼、備註、伺服器、頻道點兩下就能改，改完立刻存。"
            "最前面的 ✕ 點一下就刪掉那一列。")))
        self.acct_table = QTableWidget(0, len(ACCT_COLS))
        self.acct_table.setHorizontalHeaderLabels(list(ACCT_COLS))
        self.acct_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.acct_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.acct_table.setEditTriggers(QAbstractItemView.DoubleClicked
                                        | QAbstractItemView.EditKeyPressed)
        hh = self.acct_table.horizontalHeader()
        hh.setSectionResizeMode(COL_ACCT, QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_NOTE, QHeaderView.Stretch)
        # ⚠ 欄寬用**實際字型**量，不要寫死像素 —— 寫死的話換字型／DPI／
        #   伺服器改名就會把字切掉（使用者回報伺服器與頻道被切）。
        fm = self.acct_table.fontMetrics()
        self.acct_table.setColumnWidth(COL_DEL, fm.horizontalAdvance("✕") + 16)
        self.acct_table.setColumnWidth(COL_PICK, fm.horizontalAdvance("選擇") + 24)
        self.acct_table.setColumnWidth(COL_PROT, fm.horizontalAdvance("保護密碼") + 24)
        # 下拉要留出「最長的選項 + 下拉箭頭 + 內距」
        widest = max([fm.horizontalAdvance(n) for n, _ in self._server_list()]
                     or [fm.horizontalAdvance("邱比特(NEW)")])
        self.acct_table.setColumnWidth(COL_SRV, widest + 56)
        self.acct_table.setColumnWidth(COL_CHAN, fm.horizontalAdvance("頻道") + 56)
        # ⚠ 列高也要加高：儲存格裡塞了下拉之後，用預設列高會把字上下切掉。
        vh = self.acct_table.verticalHeader()
        vh.setDefaultSectionSize(max(vh.defaultSectionSize(), fm.height() + ROW_PAD * 2))
        rh = vh.defaultSectionSize()
        self.acct_table.setMinimumHeight(rh * (ACCT_ROWS_SHOWN + 2))
        self.acct_table.itemChanged.connect(self._on_item_changed)
        self.acct_table.itemSelectionChanged.connect(self._on_row_selected)
        # ✕ 是純文字不是按鈕（使用者不要那個框），所以點擊由這裡接。
        self.acct_table.cellClicked.connect(self._on_cell_clicked)
        cred_lay.addWidget(self.acct_table)
        root.addWidget(cred_box)

        # --- 一鍵登入 ---
        btn_row = QHBoxLayout()
        self.go_btn = QPushButton("一鍵登入")
        self.go_btn.setProperty("primary", True)   # 主要動作 → 主色（見 app/theme.py）
        self.go_btn.setToolTip(
            "把勾選的帳號全部弄上線：\n"
            "  · 已經登入的帳號直接跳過\n"
            "  · 有開著沒登入的分身就拿來用\n"
            f"  · 不夠就自己開遊戲（最多 {MAX_CLIENTS} 台）\n"
            "  · 每台都做完 切伺服器 → 帳密登入 → 選頻道 → 進入遊戲\n"
            "全程背景，不會動到你的鍵盤滑鼠。")
        self.go_btn.clicked.connect(self._start_login)
        btn_row.addWidget(self.go_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet("color: #9aa2b8;")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        root.addStretch(1)

        # ⚠ 單次觸發：每一步自己決定下一拍要隔多久（等開遊戲跟等登入差很多）。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        self._load_accounts()
        self._rebuild_table()

    @staticmethod
    def _wrap(label: QLabel) -> QLabel:
        label.setWordWrap(True)
        return label

    # ------------------------------------------------------------------
    # 設定與帳號清單
    # ------------------------------------------------------------------
    def _game_dir(self) -> str:
        exe = self.exe_edit.text().strip()
        return os.path.dirname(exe) if exe else ""

    def _load_accounts(self) -> None:
        self._accounts = []
        for item in config.get("accounts", []) or []:
            try:
                self._accounts.append({
                    "account": str(item["account"]),
                    "password": str(item.get("password", "")),
                    # 舊設定檔的欄位叫 monitor（那時是「監控」）—— 沿用它的值，
                    # 免得使用者升級之後勾選全部不見。
                    "selected": bool(item.get("selected",
                                              item.get("monitor", False))),
                    "protect": str(item.get("protect", "")),
                    "note": str(item.get("note", "")),
                    "server": str(item.get("server", "")),
                    "channel": int(item.get("channel", 1) or 1),
                })
            except (KeyError, TypeError, ValueError):
                continue

    def _save_accounts(self) -> None:
        config.set("accounts", self._accounts)
        config.set("login.exe_path", self.exe_edit.text().strip())
        config.save()

    def _server_list(self) -> list[tuple[str, int]]:
        """伺服器 [(名稱, 分流數), …]。**讀出來的**，不是寫死的。

        遊戲開著就讀記憶體，沒開就讀遊戲資料夾的 server.xml（兩邊實測一致，
        連順序都一樣）。讀到過就記起來，之後遊戲關掉也還有得用。
        """
        sc = None
        for w in self._game_windows():
            sc = self._scanner(w.pid)
            if sc:
                break
        got = login.servers(sc, self._game_dir())
        if got:
            self._srv_cache = got
        return self._srv_cache

    def _rebuild_table(self) -> None:
        self._loading_table = True
        srv = self._server_list()
        self.acct_table.setRowCount(len(self._accounts))
        for r, a in enumerate(self._accounts):
            # ⚠ 用純文字不要用 QPushButton —— 按鈕會畫出一個框，使用者說「不用
            #   太大現在是一個框框」。點擊由 cellClicked 接（見 _on_cell_clicked）。
            x = QTableWidgetItem("✕")
            x.setFlags(Qt.ItemIsEnabled)          # 不可選取、不可編輯
            x.setTextAlignment(Qt.AlignCenter)
            x.setForeground(QColor("#d06a6a"))
            x.setToolTip("刪掉這一列")
            self.acct_table.setItem(r, COL_DEL, x)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if a["selected"] else Qt.Unchecked)
            self.acct_table.setItem(r, COL_PICK, chk)

            acct_item = QTableWidgetItem(a["account"])
            acct_item.setFlags(acct_item.flags() & ~Qt.ItemIsEditable)  # 帳號別誤改
            self.acct_table.setItem(r, COL_ACCT, acct_item)
            # ⚠ 使用者指定密碼明文顯示。
            self.acct_table.setItem(
                r, COL_PWD, QTableWidgetItem(config.deobfuscate(a["password"])))
            prot = QTableWidgetItem(config.deobfuscate(a["protect"]))
            prot.setToolTip("進遊戲最後一步要輸入的保護密碼；沒設定就留空")
            self.acct_table.setItem(r, COL_PROT, prot)
            self.acct_table.setItem(r, COL_NOTE, QTableWidgetItem(a["note"]))

            srv_combo = QComboBox()
            srv_combo.addItems([n for n, _ in srv])
            i = srv_combo.findText(a["server"])
            if i < 0:
                i = 0                       # 沒設過／名字對不上 → 用第一個
                a["server"] = srv[0][0] if srv else ""
            srv_combo.setCurrentIndex(i)
            srv_combo.currentTextChanged.connect(
                lambda text, row=r: self._on_server_changed(row, text))
            self.acct_table.setCellWidget(r, COL_SRV, srv_combo)

            self._fill_channels(r, a)
        self._loading_table = False

    def _fill_channels(self, row: int, a: dict) -> None:
        """把某一列的頻道下拉重填成「這個伺服器實際有幾個分流」。"""
        srv = self._server_list()
        subsets = next((n for name, n in srv if name == a["server"]), None)
        if subsets is None:
            subsets = srv[0][1] if srv else login.MAX_SUBSET
        combo = QComboBox()
        combo.addItems([str(c) for c in range(1, subsets + 1)])
        i = combo.findText(str(a["channel"]))
        if i < 0:
            i = 0
            a["channel"] = 1
        combo.setCurrentIndex(i)
        combo.currentTextChanged.connect(
            lambda text, r=row: self._on_channel_changed(r, text))
        self.acct_table.setCellWidget(row, COL_CHAN, combo)

    def _add_account(self) -> None:
        acct = self.account_edit.text().strip()
        pwd = self.password_edit.text()
        prot = self.protect_edit.text()
        note = self.note_edit.text().strip()
        if not acct or not pwd:
            QMessageBox.warning(self, "缺少帳密", "請先在上方輸入帳號與密碼。")
            return
        for a in self._accounts:
            if a["account"] == acct:
                a["password"] = config.obfuscate(pwd)   # 同帳號 → 更新
                a["protect"] = config.obfuscate(prot)
                if note:
                    a["note"] = note
                self._save_accounts()
                self._rebuild_table()
                self._set_status(f"已更新帳號 {acct}")
                return
        srv = self._server_list()
        self._accounts.append({
            "account": acct, "password": config.obfuscate(pwd), "selected": True,
            "protect": config.obfuscate(prot), "note": note,
            "server": srv[0][0] if srv else "", "channel": 1,
        })
        self._save_accounts()
        self._rebuild_table()
        self.account_edit.clear()
        self.password_edit.clear()
        self.protect_edit.clear()
        self.note_edit.clear()
        self._set_status(f"已新增帳號 {acct}")

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """點到最前面那個 ✕ 就刪掉那一列。"""
        if col == COL_DEL and 0 <= row < len(self._accounts):
            self._delete(self._accounts[row]["account"])

    def _delete(self, account: str) -> None:
        """✕ 按鈕：刪掉這個帳號。用帳號而不是列號 —— 列號會因為重建而過期。"""
        self._accounts = [a for a in self._accounts if a["account"] != account]
        self._save_accounts()
        self._rebuild_table()
        self._set_status(f"已刪除帳號 {account}")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table:
            return
        r, c = item.row(), item.column()
        if not (0 <= r < len(self._accounts)):
            return
        if c == COL_PICK:
            self._accounts[r]["selected"] = item.checkState() == Qt.Checked
        elif c == COL_PWD:
            self._accounts[r]["password"] = config.obfuscate(item.text())
        elif c == COL_PROT:
            self._accounts[r]["protect"] = config.obfuscate(item.text())
        elif c == COL_NOTE:
            self._accounts[r]["note"] = item.text()
        else:
            return
        self._save_accounts()

    def _on_server_changed(self, row: int, text: str) -> None:
        if self._loading_table or not (0 <= row < len(self._accounts)):
            return
        a = self._accounts[row]
        a["server"] = text
        # 換伺服器 → 分流數可能不同，頻道下拉要跟著重填。
        self._loading_table = True
        self._fill_channels(row, a)
        self._loading_table = False
        self._save_accounts()

    def _on_channel_changed(self, row: int, text: str) -> None:
        if self._loading_table or not (0 <= row < len(self._accounts)):
            return
        try:
            self._accounts[row]["channel"] = int(text)
        except ValueError:
            return
        self._save_accounts()

    def _on_row_selected(self) -> None:
        rows = self.acct_table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if not (0 <= i < len(self._accounts)):
            return
        a = self._accounts[i]
        self.account_edit.setText(a["account"])
        self.password_edit.setText(config.deobfuscate(a["password"]))
        self.protect_edit.setText(config.deobfuscate(a["protect"]))
        self.note_edit.setText(a["note"])

    # ------------------------------------------------------------------
    # 分身
    # ------------------------------------------------------------------
    @staticmethod
    def _game_windows() -> list:
        seen, out = set(), []
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" in w.class_name and w.pid not in seen:
                seen.add(w.pid)
                out.append(w)
        return out

    @staticmethod
    def _account_of(title: str) -> str:
        """視窗標題裡的帳號；還沒登入（標題沒有「 - 」）回空字串。

        ⚠ 實測四台已登入分身，標題那串跟遊戲記憶體裡的帳號欄位完全一致，
          所以拿它判斷「這個帳號登入了沒」是可靠的。
        """
        return charname.account_from_title(title) if " - " in title else ""

    def _scanner(self, pid: int) -> MemoryScanner | None:
        sc = self._scanners.get(pid)
        if sc is not None:
            return sc
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)               # 改版位移自動校正（全域只做一次）
        except Exception:                              # noqa: BLE001
            sc.close()   # open() 成功但 warm() 炸掉時要收回 handle，不然會洩
            return None
        self._scanners[pid] = sc
        return sc

    def _mover(self, pid: int) -> move.Mover | None:
        """⚠⚠ 一定要走 move.acquire()，不要自己 new 一個 —— 同一個遊戲行程
        只能有一份跳板，自己裝會把掛機分頁那份拆掉。"""
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception:                              # noqa: BLE001
            self._movers.pop(pid, None)
            return None
        self._movers[pid] = mv
        return mv

    # ------------------------------------------------------------------
    # 一鍵登入：QTimer 一拍做一步
    # ------------------------------------------------------------------
    def _precheck(self) -> str:
        """開工前的環境檢查。回錯誤訊息，一切就緒回空字串。"""
        game_dir = self._game_dir()
        if not game_dir or not os.path.isdir(game_dir):
            return "請先在「自動登入」分頁設定遊戲執行檔（用來找出遊戲資料夾）。"
        if not os.path.isfile(os.path.join(game_dir, login.GAME_EXE)):
            return (f"在 {game_dir} 找不到 {login.GAME_EXE}。"
                    "請確認遊戲執行檔的路徑指到正確的資料夾。")
        return ""

    def _launch_job(self, picked: list[dict], cursor: bool) -> None:
        """把這批帳號弄上線（手動與自動回連共用同一台狀態機）。

        cursor: 手動按鈕才轉等待游標；自動回連在背景跑，
                轉游標只會讓正在用別的分頁的使用者一頭霧水。
        """
        online = {self._account_of(w.title) for w in self._game_windows()}
        online.discard("")
        queue = [a for a in picked if a["account"] not in online]
        already = [a["account"] for a in picked if a["account"] in online]

        self._job = {
            "game_dir": self._game_dir(), "queue": queue, "cur": None,
            "ok": [], "failed": [], "already": already, "cursor": cursor,
            "step": "next", "since": time.monotonic(), "proc": None,
        }
        self.go_btn.setEnabled(False)
        if cursor:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        self._timer.start(0)

    def _start_login(self) -> None:
        picked = [a for a in self._accounts if a["selected"]]
        if not picked:
            QMessageBox.information(self, "沒勾帳號", "請先在清單勾「選擇」要登入的帳號。")
            return
        err = self._precheck()
        if err:
            QMessageBox.warning(self, "還不能登入", err)
            return
        self._launch_job(picked, cursor=True)

    # -- 給「自動回連」（分身總控）用的無人值守入口 ---------------------
    def busy(self) -> bool:
        """一鍵登入／自動回連的登入流程正在跑嗎？"""
        return self._job is not None

    def known_accounts(self) -> set[str]:
        """帳號清單裡存了哪些帳號（存過帳密才有辦法自動回連）。"""
        return {a["account"] for a in self._accounts}

    def start_auto_login(self, accounts: list[str]) -> str:
        """把這些帳號（用帳號名點名）弄上線。不跳任何對話框 ——
        錯誤用回傳值講（空字串＝已開始跑）。呼叫端用 busy() 等它做完，
        再自己數視窗標題驗收，不要信「開始跑了」＝「登回去了」。
        """
        if self._job is not None:
            return "登入流程正在進行中，等它做完。"
        err = self._precheck()
        if err:
            return err
        have = {a["account"]: a for a in self._accounts}
        picked = [have[x] for x in accounts if x in have]
        if not picked:
            return "帳號清單裡沒有存這些帳號的帳密。"
        self._launch_job(picked, cursor=False)
        return ""

    def _finish(self) -> None:
        job = self._job or {}
        self._job = None
        self._timer.stop()
        if job.get("cursor"):
            QGuiApplication.restoreOverrideCursor()
        self.go_btn.setEnabled(True)
        parts = []
        if job.get("ok"):
            parts.append(f"完成 {len(job['ok'])} 個（{'、'.join(job['ok'])}）")
        if job.get("already"):
            parts.append(f"本來就在線 {len(job['already'])} 個（{'、'.join(job['already'])}）")
        if job.get("failed"):
            parts.append("沒成功：" + "；".join(f"{a}（{why}）" for a, why in job["failed"]))
        self._set_status("　｜　".join(parts) or "沒有需要處理的帳號。")

    def _fail_current(self, why: str) -> None:
        job = self._job
        job["failed"].append((job["cur"]["account"], why))
        job["cur"], job["proc"] = None, None
        job["step"], job["since"] = "next", time.monotonic()
        self._timer.start(0)

    def _goto(self, step: str, delay: int = 0) -> None:
        self._job["step"] = step
        self._job["since"] = time.monotonic()
        self._timer.start(delay)

    def _tick(self) -> None:                    # noqa: C901
        job = self._job
        if job is None:
            return
        step = job["step"]
        waited = (time.monotonic() - job["since"]) * 1000

        if step == "next":
            if not job["queue"]:
                self._finish()
                return
            job["cur"] = job["queue"].pop(0)
            job["pid"] = None
            self._set_status(f"處理 {job['cur']['account']}…")
            self._goto("find_client")
            return

        if step == "find_client":
            acct = job["cur"]["account"]
            wins = self._game_windows()
            # 這個帳號在剛剛那一輪裡已經上線了 → 不必再做
            if acct in {self._account_of(w.title) for w in wins}:
                job["ok"].append(acct)
                job["cur"] = None
                self._goto("next")
                return
            free = [w for w in wins if not self._account_of(w.title)]
            if free:
                job["pid"] = free[0].pid
                self._goto("prepare")
                return
            if len(wins) >= MAX_CLIENTS:
                self._fail_current(f"已經開滿 {MAX_CLIENTS} 台，沒有空的分身")
                return
            try:
                job["proc"] = login.launch(job["game_dir"])
            except Exception as exc:                       # noqa: BLE001
                self._fail_current(f"開遊戲失敗：{exc}")
                return
            self._set_status(f"{acct}：正在開遊戲…")
            self._goto("booting", STEP_MS)
            return

        if step == "booting":
            pid = job["proc"].pid if job["proc"] else 0
            sc = self._scanner(pid) if pid else None
            if sc is not None:
                # ⚠ 每一拍都寫：遊戲剛起來時模組還沒載完，第一次多半會失敗。
                #   它只是一個 byte，重複寫沒有副作用。
                login.skip_eula(sc)
                if login.find_screen(sc) is not None:
                    job["pid"] = pid
                    self._set_status(f"{job['cur']['account']}：遊戲已就緒")
                    self._goto("prepare")
                    return
            if waited > WAIT_BOOT_MS:
                self._fail_current("開了遊戲但等不到登入畫面")
                return
            self._set_status(
                f"{job['cur']['account']}：等遊戲開好…（{waited / 1000:.0f} 秒）")
            self._timer.start(STEP_MS)
            return

        if step == "prepare":
            sc = self._scanner(job["pid"])
            mv = self._mover(job["pid"]) if sc else None
            if sc is None or mv is None or not mv.active:
                self._fail_current("接不上這台分身（跳板裝不上）")
                return
            job["sc"], job["mv"] = sc, mv
            self._goto("signin")
            return

        if step == "signin":
            a = job["cur"]
            srv = self._server_list()
            idx = next((i for i, (n, _) in enumerate(srv) if n == a["server"]), None)
            if idx is None:
                self._fail_current(f"找不到伺服器「{a['server']}」")
                return
            self._set_status(f"{a['account']}：帳密登入（{a['server']}）…")
            err = login.sign_in(job["mv"], job["sc"], a["account"],
                                config.deobfuscate(a["password"]), server_index=idx)
            if err:
                self._fail_current(err)
                return
            self._goto("wait_channel", LOGIN_SETTLE_MS)
            return

        if step == "wait_channel":
            a = job["cur"]
            self._set_status(f"{a['account']}：選頻道 {a['channel']}…")
            err = login.pick_channel(job["mv"], job["sc"], a["channel"])
            if err:
                self._fail_current(err)
                return
            self._goto("wait_chars", STEP_MS)
            return

        if step == "wait_chars":
            # ★ 兩個都是真的讀得到的訊號：登入畫面物件被釋放＝已離開分流清單；
            #   第 1 格讀得到角色名＝角色清單真的送到了。
            name = login.character(job["sc"], CHAR_SLOT)
            if login.find_screen(job["sc"]) is None and name:
                job["char"] = name
                self._goto("enter")
                return
            if waited > WAIT_CHARS_MS:
                self._fail_current("等不到選角色畫面（伺服器沒回應，或這個分流沒有角色）")
                return
            self._timer.start(STEP_MS)
            return

        if step == "enter":
            a = job["cur"]
            self._set_status(f"{a['account']}：進入遊戲（{job.get('char', '')}）…")
            err = login.enter_game(job["mv"], job["sc"], CHAR_SLOT, a["channel"],
                                   protect=config.deobfuscate(a["protect"]))
            if err:
                self._fail_current(err)
                return
            self._goto("wait_ingame", STEP_MS)
            return

        if step == "wait_ingame":
            acct = job["cur"]["account"]
            titles = {w.pid: w.title for w in self._game_windows()}
            if job["pid"] not in titles:
                self._fail_current("登入途中遊戲關掉了")
                return
            if self._account_of(titles[job["pid"]]):
                job["ok"].append(acct)
                job["cur"], job["proc"] = None, None
                self._set_status(f"{acct} 完成 ✅")
                self._goto("next")
                return
            if waited > WAIT_INGAME_MS:
                self._fail_current("送出進入遊戲了，但沒進到遊戲裡")
                return
            self._timer.start(STEP_MS)
            return

    # ------------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------------
    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇遊戲執行檔", "", "執行檔 (*.exe *.dat);;所有檔案 (*.*)")
        if path:
            self.exe_edit.setText(path)
            self._save_accounts()
            self._rebuild_table()      # 資料夾變了 → 伺服器清單可能讀得到了

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        # ⚠ 不呼叫這行的話，狀態文字要等整件事做完才會出現。
        QApplication.processEvents()

    def on_close(self) -> None:
        self._timer.stop()
        self._job = None
        self._save_accounts()
        # ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
        #   掛機分頁可能還在用（見 move.acquire）。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                          # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
