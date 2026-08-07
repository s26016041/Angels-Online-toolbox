"""自動登入分頁。

一顆「一鍵登入」按鈕，把遊戲的三個步驟一路做完：

    帳密登入 → 選頻道 → 進入遊戲

全程寫記憶體＋呼叫遊戲自己的函式，**不占鍵盤滑鼠**，遊戲視窗在背景也照做。
實作在 app/game/login.py。

帳號清單每一列自己帶「備註」與「頻道」，選哪一列就用哪一列的設定登入，
存進設定檔，下次打開還在。

安全提醒：密碼以 base64 混淆後存進設定檔，這不是真正的加密，拿到設定檔的人可還原。
清單裡的密碼是**明文顯示**（使用者指定），旁邊有人時請留意。

## 為什麼是 QTimer 一拍做一步，不是背景執行緒

三個步驟中間都要等伺服器（登入要一個來回、選頻道要等角色清單送到）。
用 sleep 會把介面凍住；用背景 QThread 則是打包成 exe 之後會原生當機的老坑
（見 memory 的 packaging-and-release）。所以照「領取每日」分頁的做法：
單次觸發的 QTimer，每一拍檢查一個**真的讀得到的訊號**再決定下一步 ——

    等分流清單  → 固定等一下（登入來回約 1 秒）
    等選角色    → `login.find_screen()` 變成 None（登入畫面物件被釋放）
                  ＋ 第 1 格讀得到角色名（角色清單真的送到了）
    等進遊戲    → 視窗標題出現「 - 帳號(伺服器-頻道)」

## 2026-08-07 改動

* 拿掉 pyautogui 那條路（等 N 秒再打字）—— 它打字進的是「當時剛好有焦點的
  視窗」，跟使用者「自動登入全程背景、不可占用實體鍵鼠」的要求直接衝突。
* 角色固定第 1 個、不給選：使用者選到第 5 格（那格沒角色）時，伺服器會
  **直接切斷連線**。現在 `login.enter_game()` 送包前會照遊戲自己的做法先檢查。
"""
from __future__ import annotations

import os
import subprocess
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
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
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import locate, login, move
from app.tabs.base_tab import BaseTab

# ★ 角色固定第 1 個（0 起算 = 0），不給使用者選 —— 使用者指定。
#   會這樣定是因為選到空的角色格時伺服器直接斷線（見 login.enter_game 的檢查）。
CHAR_SLOT = 0

ACCT_COLS = ("監控", "帳號", "密碼", "備註", "頻道")
ACCT_ROWS_SHOWN = 5           # 使用者指定：清單至少看得到 5 列

STEP_MS = 300                 # 等待時每一拍的間隔
LOGIN_SETTLE_MS = 1500        # 送出帳密 → 等分流清單送到（實測登入來回 < 1 秒）
WAIT_CHARS_MS = 20000         # 等選角色畫面的上限
WAIT_INGAME_MS = 25000        # 等真的進到遊戲裡的上限


class LoginTab(BaseTab):
    TAB_TITLE = "自動登入"
    ORDER = 10

    def build_ui(self) -> None:
        # 帳號清單：每筆 {"account", "password"(混淆), "monitor", "note", "channel"}
        self._accounts: list[dict] = []
        self._loading_table = False
        self._loading_clients = False
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        self._job: dict | None = None          # 一鍵登入的進行狀態

        root = QVBoxLayout(self)

        # --- 遊戲設定 ---
        game_box = QGroupBox("遊戲設定")
        game_form = QFormLayout(game_box)
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText(r"例如 D:\AngelsOnline\Angels Online Global\start.exe")
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
        # ⚠ 使用者指定密碼不要遮起來（原本是 QLineEdit.Password）。
        self.password_edit = QLineEdit()
        form.addRow("密碼：", self.password_edit)
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

        cred_lay.addWidget(QLabel(
            "帳號清單（勾「監控」的帳號會被『監控數值』分頁拿去對應同名分身；"
            "「頻道」與「備註」點一下就能改，選中的那一列就是一鍵登入要用的）："))
        self.acct_table = QTableWidget(0, len(ACCT_COLS))
        self.acct_table.setHorizontalHeaderLabels(list(ACCT_COLS))
        self.acct_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.acct_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.acct_table.setEditTriggers(QAbstractItemView.DoubleClicked
                                        | QAbstractItemView.EditKeyPressed)
        hh = self.acct_table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.acct_table.setColumnWidth(0, 50)
        self.acct_table.setColumnWidth(4, 90)
        # ⚠ 高度用「列高 × 列數」算，不要寫死像素 —— 換字型／DPI 會走鐘。
        #   +2 是表頭與框線，寧可多一點也不要少一列（使用者反映只看得到 2 列）。
        rh = self.acct_table.verticalHeader().defaultSectionSize()
        self.acct_table.setMinimumHeight(rh * (ACCT_ROWS_SHOWN + 2))
        self.acct_table.itemChanged.connect(self._on_acct_item_changed)
        self.acct_table.itemSelectionChanged.connect(self._on_acct_selected)
        cred_lay.addWidget(self.acct_table)

        acct_btns = QHBoxLayout()
        del_btn = QPushButton("刪除選取帳號")
        del_btn.clicked.connect(self._delete_account)
        acct_btns.addWidget(del_btn)
        acct_btns.addStretch(1)
        cred_lay.addLayout(acct_btns)
        root.addWidget(cred_box)

        # --- 對分身下指令 ---
        act_box = QGroupBox("一鍵登入（背景執行，不會動到你的鍵盤滑鼠）")
        act_lay = QVBoxLayout(act_box)
        act_lay.addWidget(self._wrap(QLabel(
            "選一台停在登入畫面的分身、在上面的清單選一個帳號，按下去就會"
            "「帳密登入 → 選頻道 → 進入遊戲」一路做到底（中間要等伺服器，約 5~10 秒）。"
            f"角色固定用第 {CHAR_SLOT + 1} 個。")))

        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("分身："))
        self.client_combo = QComboBox()
        self.client_combo.setMinimumWidth(260)
        self.client_combo.currentIndexChanged.connect(self._on_client_changed)
        pick_row.addWidget(self.client_combo)
        refresh_btn = QPushButton("重新整理")
        refresh_btn.clicked.connect(self._refresh_clients)
        pick_row.addWidget(refresh_btn)
        self.server_label = QLabel("")
        self.server_label.setStyleSheet("color: #9aa2b8;")
        pick_row.addWidget(self.server_label)
        pick_row.addStretch(1)
        act_lay.addLayout(pick_row)

        run_row = QHBoxLayout()
        self.go_btn = QPushButton("一鍵登入")
        self.go_btn.setProperty("primary", True)   # 主要動作 → 主色（見 app/theme.py）
        self.go_btn.setToolTip(
            "照遊戲本來的三個步驟一路做完：\n"
            "  ① 帳密登入（帳密直接寫進遊戲，不經過鍵盤）\n"
            "  ② 選頻道（用清單那一列的頻道）\n"
            "  ③ 進入遊戲（第 1 個角色）\n"
            "中間會等伺服器回話，過程中按鈕會鎖住。")
        self.go_btn.clicked.connect(self._start_login)
        run_row.addWidget(self.go_btn)
        self.launch_btn = QPushButton("啟動遊戲")
        self.launch_btn.clicked.connect(self._launch_game)
        run_row.addWidget(self.launch_btn)
        run_row.addStretch(1)
        act_lay.addLayout(run_row)
        root.addWidget(act_box)

        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status_label)
        root.addStretch(1)

        # ⚠ 單次觸發：每一步自己決定下一拍要隔多久（等登入跟等角色清單不一樣久）。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        self._load_accounts()
        self._rebuild_acct_table()
        self._load_settings()

    @staticmethod
    def _wrap(label: QLabel) -> QLabel:
        label.setWordWrap(True)
        return label

    # ------------------------------------------------------------------
    # 設定讀寫
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        self.exe_edit.setText(config.get("login.exe_path", ""))
        # 上次用哪一個帳號 → 直接選回那一列（帳密／頻道就都跟著回來了）。
        last = str(config.get("login.last_account", ""))
        for r, a in enumerate(self._accounts):
            if a["account"] == last:
                self.acct_table.selectRow(r)
                break

    def _save_settings(self) -> None:
        config.set("login.exe_path", self.exe_edit.text().strip())
        a = self._selected_account()
        if a:
            config.set("login.last_account", a["account"])
        config.save()

    def _game_dir(self) -> str:
        exe = self.exe_edit.text().strip()
        return os.path.dirname(exe) if exe else ""

    # ------------------------------------------------------------------
    # 帳號清單
    # ------------------------------------------------------------------
    def _load_accounts(self) -> None:
        self._accounts = []
        for item in config.get("accounts", []) or []:
            try:
                self._accounts.append({
                    "account": str(item["account"]),
                    "password": str(item.get("password", "")),
                    "monitor": bool(item.get("monitor", False)),
                    "note": str(item.get("note", "")),
                    # 舊設定檔沒有這兩欄 → 給預設值，不要讓整筆被丟掉。
                    "channel": int(item.get("channel", 1) or 1),
                })
            except (KeyError, TypeError, ValueError):
                continue

    def _save_accounts(self) -> None:
        config.set("accounts", self._accounts)
        config.save()

    def _channel_choices(self) -> list[int]:
        """頻道下拉要列 1~幾。**讀出來的**，不是寫死的（見 login.subset_count）。"""
        pid = self.client_combo.currentData()
        sc = self._scanner(pid) if pid else None
        return list(range(1, login.subset_count(sc, self._game_dir()) + 1))

    def _rebuild_acct_table(self) -> None:
        self._loading_table = True
        choices = self._channel_choices()
        self.acct_table.setRowCount(len(self._accounts))
        for r, a in enumerate(self._accounts):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if a["monitor"] else Qt.Unchecked)
            self.acct_table.setItem(r, 0, chk)
            acct_item = QTableWidgetItem(a["account"])
            acct_item.setFlags(acct_item.flags() & ~Qt.ItemIsEditable)  # 帳號別誤改
            self.acct_table.setItem(r, 1, acct_item)
            # ⚠ 使用者指定密碼明文顯示（原本是 8 個圓點）。
            self.acct_table.setItem(
                r, 2, QTableWidgetItem(config.deobfuscate(a["password"])))
            self.acct_table.setItem(r, 3, QTableWidgetItem(a["note"]))
            # 頻道用下拉，選項是讀出來的分流數
            combo = QComboBox()
            combo.addItems([str(c) for c in choices])
            want = str(a["channel"])
            i = combo.findText(want)
            if i < 0:                       # 存的頻道超出現在的分流數 → 退回第 1 頻
                i = 0
                a["channel"] = choices[0] if choices else 1
            combo.setCurrentIndex(i)
            combo.currentTextChanged.connect(
                lambda text, row=r: self._on_channel_changed(row, text))
            self.acct_table.setCellWidget(r, 4, combo)
        self._loading_table = False

    def _selected_account(self) -> dict | None:
        rows = self.acct_table.selectionModel().selectedRows()
        if not rows:
            return None
        i = rows[0].row()
        return self._accounts[i] if 0 <= i < len(self._accounts) else None

    def _add_account(self) -> None:
        acct = self.account_edit.text().strip()
        pwd = self.password_edit.text()
        note = self.note_edit.text().strip()
        if not acct or not pwd:
            QMessageBox.warning(self, "缺少帳密", "請先在上方輸入帳號與密碼。")
            return
        for a in self._accounts:
            if a["account"] == acct:
                a["password"] = config.obfuscate(pwd)   # 同帳號 → 更新
                if note:
                    a["note"] = note
                self._save_accounts()
                self._rebuild_acct_table()
                self._set_status(f"已更新帳號 {acct}")
                return
        self._accounts.append({
            "account": acct, "password": config.obfuscate(pwd), "monitor": True,
            "note": note, "channel": 1,
        })
        self._save_accounts()
        self._rebuild_acct_table()
        self.account_edit.clear()
        self.password_edit.clear()
        self.note_edit.clear()
        self._set_status(f"已新增帳號 {acct}")

    def _delete_account(self) -> None:
        rows = self.acct_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在清單選一個帳號。")
            return
        i = rows[0].row()
        if 0 <= i < len(self._accounts):
            acct = self._accounts[i]["account"]
            del self._accounts[i]
            self._save_accounts()
            self._rebuild_acct_table()
            self._set_status(f"已刪除帳號 {acct}")

    def _on_acct_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table:
            return
        r = item.row()
        if not (0 <= r < len(self._accounts)):
            return
        if item.column() == 0:
            self._accounts[r]["monitor"] = item.checkState() == Qt.Checked
        elif item.column() == 2:
            self._accounts[r]["password"] = config.obfuscate(item.text())
        elif item.column() == 3:
            self._accounts[r]["note"] = item.text()
        else:
            return
        self._save_accounts()

    def _on_channel_changed(self, row: int, text: str) -> None:
        if self._loading_table or not (0 <= row < len(self._accounts)):
            return
        try:
            self._accounts[row]["channel"] = int(text)
        except ValueError:
            return
        self._save_accounts()

    def _on_acct_selected(self) -> None:
        a = self._selected_account()
        if not a:
            return
        self.account_edit.setText(a["account"])
        self.password_edit.setText(config.deobfuscate(a["password"]))
        self.note_edit.setText(a["note"])
        self._save_settings()          # 記住「上次用哪個帳號」

    # ------------------------------------------------------------------
    # 分身：清單、掃描器、跳板
    # ------------------------------------------------------------------
    def _refresh_clients(self) -> None:
        """重新列出開著的分身。**還停在登入畫面的也要列出來**。"""
        keep = self.client_combo.currentData()
        # ⚠ clear()/addItem() 都會觸發 currentIndexChanged —— 不擋的話重整一次
        #   會對「清單建到一半的中間狀態」開好幾次記憶體控制代碼。
        self._loading_clients = True
        self.client_combo.clear()
        seen: set[int] = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            # ⚠ 還沒登入時標題就只有「Angels Online Global」，沒有「 - 帳號」那段。
            #   `account_from_title()` 對這種標題會**把整個標題當帳號回傳**
            #   （別的分頁都只處理已登入的分身，所以從來沒撞到）。
            acc = charname.account_from_title(w.title) if " - " in w.title else ""
            nm = preload.name_of(w.pid)          # 不帶 scanner ＝ 絕不現場掃描
            if nm and acc:
                label = f"{nm}（{acc}）"
            else:
                label = acc or f"尚未登入（PID {w.pid}）"
            self.client_combo.addItem(label, w.pid)
        self._loading_clients = False
        if not seen:
            self.server_label.setText("")
            self._set_status("找不到開著的遊戲分身。")
            return
        i = self.client_combo.findData(keep)
        self.client_combo.setCurrentIndex(max(i, 0))
        self._on_client_changed()

    def _on_client_changed(self) -> None:
        """換分身：更新伺服器說明，並把頻道下拉的選項重新算一次。"""
        if self._loading_clients:
            return
        pid = self.client_combo.currentData()
        sc = self._scanner(pid) if pid else None
        info = login.server_info(sc) if sc else None
        if info is None:
            self.server_label.setText("")
        else:
            name, subsets = info
            now = login.read_channel(sc)
            tail = f"｜這台目前在 {now} 頻" if now is not None else ""
            self.server_label.setText(f"伺服器：{name}（{subsets} 個分流）{tail}")
        self._rebuild_acct_table()

    def _scanner(self, pid: int) -> MemoryScanner | None:
        sc = self._scanners.get(pid)
        if sc is not None:
            return sc
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)               # 改版位移自動校正（全域只做一次）
        except Exception:                              # noqa: BLE001
            sc.close()   # open() 成功但 warm() 炸掉時要收回 handle，不然每按一次洩一個
            return None
        self._scanners[pid] = sc
        return sc

    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，**不要自己 new 一個 Mover** ——
          同一個遊戲行程只能有一份跳板，自己裝會把掛機分頁那份拆掉。
        """
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
    def _start_login(self) -> None:
        acct = self._selected_account()
        if acct is None:
            QMessageBox.information(self, "沒選帳號", "請先在帳號清單點一列。")
            return
        pid = self.client_combo.currentData()
        if not pid:
            QMessageBox.information(self, "沒有分身", "請先按「重新整理」選一台分身。")
            return
        sc = self._scanner(pid)
        if sc is None:
            QMessageBox.warning(self, "接不上", f"讀不到 PID {pid} 的記憶體（它可能剛關掉）。")
            return
        mv = self._mover(pid)
        if mv is None or not mv.active:
            QMessageBox.warning(
                self, "跳板沒裝上",
                "裝不上呼叫遊戲函式用的跳板。遊戲剛啟動時再等幾秒，"
                "或確認工具箱是以系統管理員身分執行。")
            return
        self._save_settings()
        self._job = {
            "pid": pid, "sc": sc, "mv": mv,
            "account": acct["account"],
            "password": config.deobfuscate(acct["password"]),
            "channel": int(acct["channel"]),
            "step": "signin", "since": time.monotonic(),
        }
        self.go_btn.setEnabled(False)
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        self._timer.start(0)

    def _finish(self, text: str, warn: str = "") -> None:
        self._job = None
        self._timer.stop()
        QGuiApplication.restoreOverrideCursor()
        self.go_btn.setEnabled(True)
        self._set_status(text)
        if warn:
            QMessageBox.warning(self, "一鍵登入沒完成", warn)

    def _tick(self) -> None:
        job = self._job
        if job is None:
            return
        sc, mv = job["sc"], job["mv"]
        step = job["step"]
        waited = (time.monotonic() - job["since"]) * 1000

        if step == "signin":
            self._set_status(f"① 送出帳密登入（{job['account']}）…")
            err = login.sign_in(mv, sc, job["account"], job["password"])
            if err:
                self._finish(f"帳密登入失敗：{err}", err)
                return
            job["step"], job["since"] = "wait_channel", time.monotonic()
            self._timer.start(LOGIN_SETTLE_MS)
            return

        if step == "wait_channel":
            # 登入來回實測不到 1 秒；等 LOGIN_SETTLE_MS 之後就去確定分流。
            self._set_status(f"② 選頻道 {job['channel']}…")
            err = login.pick_channel(mv, sc, job["channel"])
            if err:
                self._finish(f"選頻道失敗：{err}", err)
                return
            job["step"], job["since"] = "wait_chars", time.monotonic()
            self._timer.start(STEP_MS)
            return

        if step == "wait_chars":
            # ★ 兩個都是**真的讀得到的訊號**，不是猜時間：
            #   登入畫面物件被釋放 ＝ 已經離開分流清單；
            #   第 1 格讀得到角色名 ＝ 角色清單真的送到了。
            name = login.character(sc, CHAR_SLOT)
            if login.find_screen(sc) is None and name:
                job["char_name"] = name
                job["step"], job["since"] = "enter", time.monotonic()
                self._timer.start(0)
                return
            if waited > WAIT_CHARS_MS:
                self._finish(
                    "等不到選角色畫面",
                    "選頻道之後等不到角色清單（超過 20 秒）。\n"
                    "可能是伺服器沒回應，或這個帳號在這個分流沒有角色。")
                return
            self._set_status(f"② 等角色清單…（{waited / 1000:.0f} 秒）")
            self._timer.start(STEP_MS)
            return

        if step == "enter":
            self._set_status(f"③ 進入遊戲（{job.get('char_name', '')}）…")
            err = login.enter_game(mv, sc, CHAR_SLOT, job["channel"])
            if err:
                self._finish(f"進入遊戲失敗：{err}", err)
                return
            job["step"], job["since"] = "wait_ingame", time.monotonic()
            self._timer.start(STEP_MS)
            return

        if step == "wait_ingame":
            # 進到遊戲裡之後視窗標題才會變成「… - 帳號(伺服器-頻道)」。
            title = self._title_of(job["pid"])
            if title is None:
                self._finish("遊戲關掉了", "登入途中遊戲視窗不見了。")
                return
            if " - " in title:
                self._finish(f"✅ 登入完成：{title.split(' - ', 1)[1]}")
                self._refresh_clients()
                return
            if waited > WAIT_INGAME_MS:
                self._finish(
                    "送出了但沒進到遊戲",
                    "「進入遊戲」已經送出，但超過 25 秒還沒進到遊戲裡。\n"
                    "如果遊戲跳出斷線訊息，多半是登入連線閒置太久 ——\n"
                    "重開遊戲之後再按一次一鍵登入。")
                return
            self._set_status(f"③ 等進入遊戲…（{waited / 1000:.0f} 秒）")
            self._timer.start(STEP_MS)
            return

    @staticmethod
    def _title_of(pid: int) -> str | None:
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if w.pid == pid and "_MIDAGEONL_" in w.class_name:
                return w.title
        return None

    # ------------------------------------------------------------------
    # 其他動作
    # ------------------------------------------------------------------
    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇遊戲執行檔", "", "執行檔 (*.exe);;所有檔案 (*.*)"
        )
        if path:
            self.exe_edit.setText(path)
            self._save_settings()

    def _launch_game(self) -> bool:
        exe = self.exe_edit.text().strip()
        if not exe:
            QMessageBox.warning(self, "缺少設定", "請先設定遊戲執行檔路徑。")
            return False
        if not os.path.isfile(exe):
            QMessageBox.warning(self, "路徑錯誤", f"找不到執行檔：\n{exe}")
            return False
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "啟動失敗", f"無法啟動遊戲：\n{exc}")
            return False
        self._set_status("已啟動遊戲")
        return True

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        # ⚠ 不呼叫這行的話，狀態文字要等整件事做完才會出現。
        QApplication.processEvents()

    def on_show(self) -> None:
        # ⚠ 不在 build_ui() 就列 —— 開機時列會多跑一次列舉視窗，
        #   打包成 exe 之後「開機掃描不能太重」（見 packaging-and-release）。
        if self.client_combo.count() == 0:
            self._refresh_clients()

    def on_close(self) -> None:
        self._timer.stop()
        self._job = None
        self._save_settings()
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
