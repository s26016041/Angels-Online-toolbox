"""自動登入分頁。

功能：
1. 設定遊戲執行檔。
2. 管理「帳號清單」：新增 / 刪除帳號密碼（存進設定檔），並可勾選「監控」——
   勾選的帳號會被「監控數值」分頁拿去對應同名的天使之戀分身來監控。
3. 對選定的分身「帳密登入」與「進入遊戲」——**純寫記憶體＋呼叫遊戲自己的
   函式**，不占鍵盤滑鼠，遊戲視窗在背景也照做。實作在 app/game/login.py。

安全提醒：密碼以 base64 混淆後存進設定檔，這不是真正的加密，拿到設定檔的人可還原。

## 2026-08-07 改動：拿掉 pyautogui 那條路

原本的「啟動並自動登入」是等 N 秒之後用 pyautogui 打字。兩個問題：

  · **會搶走使用者當下的鍵盤** —— 它打字進的是「當時剛好有焦點的視窗」，
    使用者明確要求自動登入全程背景、完全不可占用實體鍵鼠
    （見 memory 的 auto-login-findings）。
  · 「啟動後等待」那個秒數只是在賭遊戲多久開得起來，賭錯就打進別的地方。

所以整條（等待秒數 + LoginWorker + pyautogui）一起移除，換成新的兩顆按鈕。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import locate, login, move
# fit_spin：數字框寬度照最大值算 —— 寫死的話上下箭頭會把數字擠掉
# （使用者回報過「框框被砍到一半」）。
from app.tabs.base_tab import BaseTab, fit_spin


class LoginTab(BaseTab):
    TAB_TITLE = "自動登入"
    ORDER = 10

    def build_ui(self) -> None:
        # 帳號清單：每筆 {"account": str, "password": obfuscated, "monitor": bool}
        self._accounts: list[dict] = []
        self._loading_table = False
        self._loading_clients = False
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}

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
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("密碼：", self.password_edit)
        cred_lay.addLayout(form)

        add_row = QHBoxLayout()
        add_btn = QPushButton("＋ 新增到帳號清單")
        add_btn.clicked.connect(self._add_account)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        cred_lay.addLayout(add_row)

        cred_lay.addWidget(
            QLabel("帳號清單（勾「監控」的帳號，會被『監控數值』分頁拿去對應同名分身監控）：")
        )
        self.acct_table = QTableWidget(0, 3)
        self.acct_table.setHorizontalHeaderLabels(["監控", "帳號", "密碼"])
        self.acct_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.acct_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.acct_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.acct_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.acct_table.setColumnWidth(0, 50)
        self.acct_table.itemChanged.connect(self._on_acct_item_changed)
        self.acct_table.doubleClicked.connect(self._load_account_to_edits)
        cred_lay.addWidget(self.acct_table)

        acct_btns = QHBoxLayout()
        del_btn = QPushButton("刪除選取帳號")
        del_btn.clicked.connect(self._delete_account)
        acct_btns.addWidget(del_btn)
        acct_btns.addStretch(1)
        cred_lay.addLayout(acct_btns)
        root.addWidget(cred_box)

        # --- 對分身下指令 ---
        act_box = QGroupBox("對分身下指令（背景執行，不會動到你的鍵盤滑鼠）")
        act_lay = QVBoxLayout(act_box)
        act_hint = QLabel(
            "送出的跟你自己在遊戲裡點按鈕是同一個封包。"
            "「帳密登入」要遊戲停在登入畫面（伺服器已選好，預設就選好第一個）；"
            "「進入遊戲」要已經到選角色畫面。")
        act_hint.setWordWrap(True)
        act_lay.addWidget(act_hint)

        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("分身："))
        self.client_combo = QComboBox()
        self.client_combo.setMinimumWidth(260)
        self.client_combo.currentIndexChanged.connect(self._sync_channel_range)
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
        self.signin_btn = QPushButton("帳密登入")
        self.signin_btn.setProperty("primary", True)  # 主要動作 → 主色（見 app/theme.py）
        self.signin_btn.setToolTip(
            "把上方的帳號密碼寫進遊戲的帳密緩衝區，再叫遊戲自己執行「按下登入鈕」。\n"
            "帳密不會經過鍵盤，也不必把遊戲視窗切到前景。")
        self.signin_btn.clicked.connect(self._do_sign_in)
        run_row.addWidget(self.signin_btn)

        run_row.addWidget(QLabel("第"))
        self.slot_spin = QSpinBox()
        self.slot_spin.setRange(1, 8)      # 這遊戲的角色欄位最多 8 格
        self.slot_spin.setValue(1)
        fit_spin(self.slot_spin)
        run_row.addWidget(self.slot_spin)
        run_row.addWidget(QLabel("個角色，頻道"))
        # ⚠ 上限先給遊戲的硬上限，接上分身之後改成**那台伺服器實際的分流數**
        #   （見 _sync_channel_range）—— 分流數是遊戲隨時會調的東西，不寫死。
        self.chan_spin = QSpinBox()
        self.chan_spin.setRange(1, login.MAX_SUBSET)
        self.chan_spin.setValue(1)
        fit_spin(self.chan_spin)
        run_row.addWidget(self.chan_spin)
        self.enter_btn = QPushButton("進入遊戲")
        self.enter_btn.setToolTip(
            "送出「選這個角色、這個頻道，進遊戲」那一包。\n"
            "格號照選角色畫面由左到右數，第 1 個就是 1。\n"
            "頻道就是「雅典娜-3」的那個 3 —— 不必自己去點頻道選擇畫面，\n"
            "頻道本來就是這一包裡的一個欄位。")
        self.enter_btn.clicked.connect(self._do_enter_game)
        run_row.addWidget(self.enter_btn)
        run_row.addStretch(1)
        act_lay.addLayout(run_row)
        root.addWidget(act_box)

        # --- 按鈕 ---
        btn_row = QHBoxLayout()
        self.launch_btn = QPushButton("啟動遊戲")
        self.launch_btn.clicked.connect(self._launch_game)
        btn_row.addWidget(self.launch_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status_label)
        root.addStretch(1)

        self._load_settings()
        self._load_accounts()
        self._rebuild_acct_table()

    # ------------------------------------------------------------------
    # 遊戲設定讀寫
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        self.exe_edit.setText(config.get("login.exe_path", ""))

    def _save_settings(self) -> None:
        config.set("login.exe_path", self.exe_edit.text().strip())
        config.save()

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
                })
            except (KeyError, TypeError):
                continue

    def _save_accounts(self) -> None:
        config.set("accounts", self._accounts)
        config.save()

    def _rebuild_acct_table(self) -> None:
        self._loading_table = True
        self.acct_table.setRowCount(len(self._accounts))
        for r, a in enumerate(self._accounts):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if a["monitor"] else Qt.Unchecked)
            self.acct_table.setItem(r, 0, chk)
            self.acct_table.setItem(r, 1, QTableWidgetItem(a["account"]))
            self.acct_table.setItem(r, 2, QTableWidgetItem("•" * 8))
        self._loading_table = False

    def _add_account(self) -> None:
        acct = self.account_edit.text().strip()
        pwd = self.password_edit.text()
        if not acct or not pwd:
            QMessageBox.warning(self, "缺少帳密", "請先在上方輸入帳號與密碼。")
            return
        for a in self._accounts:
            if a["account"] == acct:
                a["password"] = config.obfuscate(pwd)  # 同帳號 → 更新密碼
                self._save_accounts()
                self._rebuild_acct_table()
                self.status_label.setText(f"已更新帳號 {acct} 的密碼")
                return
        self._accounts.append({
            "account": acct, "password": config.obfuscate(pwd), "monitor": True,
        })
        self._save_accounts()
        self._rebuild_acct_table()
        self.account_edit.clear()
        self.password_edit.clear()
        self.status_label.setText(f"已新增帳號 {acct}")

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
            self.status_label.setText(f"已刪除帳號 {acct}")

    def _on_acct_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != 0:
            return
        r = item.row()
        if 0 <= r < len(self._accounts):
            self._accounts[r]["monitor"] = item.checkState() == Qt.Checked
            self._save_accounts()

    def _load_account_to_edits(self) -> None:
        rows = self.acct_table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if 0 <= i < len(self._accounts):
            self.account_edit.setText(self._accounts[i]["account"])
            self.password_edit.setText(config.deobfuscate(self._accounts[i]["password"]))

    # ------------------------------------------------------------------
    # 動作
    # ------------------------------------------------------------------
    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇遊戲執行檔", "", "執行檔 (*.exe);;所有檔案 (*.*)"
        )
        if path:
            self.exe_edit.setText(path)

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

    # ------------------------------------------------------------------
    # 分身：清單、掃描器、跳板
    # ------------------------------------------------------------------
    def _refresh_clients(self) -> None:
        """重新列出開著的分身。**還停在登入畫面的也要列出來**。

        ⚠ 不能像別的分頁那樣只認得出角色名 —— 登入畫面根本還沒有角色，
          `preload.name_of()` 會回空字串。這裡退回顯示視窗標題裡的帳號，
          再退回 PID。
        """
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
            #   （別的分頁都只處理已登入的分身，所以從來沒撞到），
            #   所以這裡先自己確認有沒有那個分隔號。
            acc = charname.account_from_title(w.title) if " - " in w.title else ""
            nm = preload.name_of(w.pid)          # 不帶 scanner ＝ 絕不現場掃描
            if nm and acc:
                label = f"{nm}（{acc}）"
            else:
                label = acc or f"尚未登入（PID {w.pid}）"
            self.client_combo.addItem(label, w.pid)
        if not seen:
            self._loading_clients = False
            self.server_label.setText("")
            self._set_status("找不到開著的遊戲分身。")
            return
        i = self.client_combo.findData(keep)
        self.client_combo.setCurrentIndex(max(i, 0))
        self._loading_clients = False
        self._sync_channel_range()

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
          同一個遊戲行程只能有一份跳板，自己裝會把掛機分頁那份拆掉
          （見 move.acquire 的說明）。
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

    def _target(self) -> tuple[int, MemoryScanner, move.Mover] | None:
        """選中的分身 + 它的掃描器與跳板；缺任何一樣就跳訊息回 None。"""
        pid = self.client_combo.currentData()
        if not pid:
            QMessageBox.information(self, "沒有分身", "請先按「重新整理」選一台分身。")
            return None
        sc = self._scanner(pid)
        if sc is None:
            QMessageBox.warning(self, "接不上", f"讀不到 PID {pid} 的記憶體（它可能剛關掉）。")
            return None
        mv = self._mover(pid)
        if mv is None or not mv.active:
            QMessageBox.warning(
                self, "跳板沒裝上",
                "裝不上呼叫遊戲函式用的跳板。遊戲剛啟動時再等幾秒，"
                "或確認工具箱是以系統管理員身分執行。")
            return None
        return pid, sc, mv

    def _sync_channel_range(self) -> None:
        """把頻道上限改成「這台伺服器實際有幾個分流」，並跟上它目前的頻道。

        讀不到就維持遊戲的硬上限 —— 讀不到只代表現在還沒接上（例如遊戲剛開），
        不該因此讓使用者連填都不能填。
        """
        if self._loading_clients:
            return
        pid = self.client_combo.currentData()
        if not pid:
            return
        sc = self._scanner(pid)
        if sc is None:
            return
        info = login.server_info(sc)
        if info is not None:
            name, subsets = info
            self.chan_spin.setMaximum(subsets)
            self.server_label.setText(f"伺服器：{name}（{subsets} 個分流）")
        else:
            self.server_label.setText("伺服器：讀不到（還沒接上遊戲）")
        now = login.read_channel(sc)
        if now is not None and now <= self.chan_spin.maximum():
            self.chan_spin.setValue(now)

    # ------------------------------------------------------------------
    # 兩顆動作按鈕
    # ------------------------------------------------------------------
    def _do_sign_in(self) -> None:
        account = self.account_edit.text().strip()
        password = self.password_edit.text()
        if not account or not password:
            QMessageBox.warning(self, "缺少帳密", "請在上方輸入（或雙擊清單載入）帳號與密碼。")
            return
        got = self._target()
        if got is None:
            return
        _, sc, mv = got
        # ⚠ 這一步裡有一次全記憶體掃描（找登入畫面物件，約 0.6 秒），
        #   跑在 GUI 執行緒上 —— 畫面會頓一下。**不要改成背景 QThread**：
        #   打包成 exe 之後背景執行緒沒無頭防護會原生當機
        #   （見 memory 的 packaging-and-release）。改成給個沙漏 + 先寫狀態，
        #   讓使用者知道不是當掉了。
        self._busy("正在找登入畫面…")
        try:
            err = login.sign_in(mv, sc, account, password)
        finally:
            self._idle()
        if err:
            self._set_status(f"帳密登入失敗：{err}")
            QMessageBox.warning(self, "帳密登入失敗", err)
            return
        self._set_status(f"已送出帳密登入（{account}）—— 接下來遊戲會自己連線、選角色畫面。")

    def _do_enter_game(self) -> None:
        got = self._target()
        if got is None:
            return
        _, sc, mv = got
        slot = self.slot_spin.value() - 1        # 畫面上 1 起算，遊戲裡 0 起算
        chan = self.chan_spin.value()            # 頻道兩邊都是 1 起算
        self._busy("送出進入遊戲…")
        try:
            err = login.enter_game(mv, sc, slot, chan)
        finally:
            self._idle()
        if err:
            self._set_status(f"進入遊戲失敗：{err}")
            QMessageBox.warning(self, "進入遊戲失敗", err)
            return
        self._set_status(f"已送出「進入遊戲」（第 {slot + 1} 個角色、頻道 {chan}）。")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _busy(self, text: str) -> None:
        """按鈕鎖住 + 沙漏 + 先把狀態文字畫出來（動作是同步的，會頓一下）。"""
        self.signin_btn.setEnabled(False)
        self.enter_btn.setEnabled(False)
        self._set_status(text)
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        # ⚠ 不呼叫這行的話，狀態文字要等整件事做完才會出現 —— 使用者看到的
        #   就是「按了沒反應然後畫面卡住」。
        QApplication.processEvents()

    def _idle(self) -> None:
        QGuiApplication.restoreOverrideCursor()
        self.signin_btn.setEnabled(True)
        self.enter_btn.setEnabled(True)

    def on_show(self) -> None:
        # ⚠ 不在 build_ui() 就列 —— 開機時列會多跑一次列舉視窗，
        #   打包成 exe 之後「開機掃描不能太重」（見 memory 的 packaging-and-release）。
        if self.client_combo.count() == 0:
            self._refresh_clients()

    def on_close(self) -> None:
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
