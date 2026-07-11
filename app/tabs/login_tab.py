"""自動登入分頁。

功能：
1. 設定遊戲執行檔與啟動等待秒數。
2. 管理「帳號清單」：新增 / 刪除帳號密碼（存進設定檔），並可勾選「監控」——
   勾選的帳號會被「監控數值」分頁拿去對應同名的天使之戀分身來監控。
3. 啟動遊戲並（以上方帳密）自動輸入登入。

安全提醒：密碼以 base64 混淆後存進設定檔，這不是真正的加密，拿到設定檔的人可還原。

自動輸入按鍵使用 pyautogui，採延遲載入：即使沒安裝，本分頁與整個程式仍可正常開啟。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from app.tabs.base_tab import BaseTab


class LoginWorker(QThread):
    """在背景執行「等待 + 自動填入帳密」，避免卡住介面。"""

    status = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, account: str, password: str, delay_sec: int, parent=None) -> None:
        super().__init__(parent)
        self._account = account
        self._password = password
        self._delay = delay_sec

    def run(self) -> None:
        try:
            import pyautogui  # 延遲載入
        except ImportError:
            self.failed.emit(
                "尚未安裝 pyautogui，無法自動輸入帳密。\n"
                "請執行：py -m pip install pyautogui"
            )
            return
        try:
            for remaining in range(self._delay, 0, -1):
                self.status.emit(f"等待登入視窗… {remaining} 秒後開始輸入")
                self.msleep(1000)
            self.status.emit("輸入帳號…")
            pyautogui.typewrite(self._account, interval=0.05)
            pyautogui.press("tab")
            self.status.emit("輸入密碼…")
            pyautogui.typewrite(self._password, interval=0.05)
            pyautogui.press("enter")
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"自動輸入時發生錯誤：{exc}")


class LoginTab(BaseTab):
    TAB_TITLE = "自動登入"
    ORDER = 10

    def build_ui(self) -> None:
        self._worker: LoginWorker | None = None
        # 帳號清單：每筆 {"account": str, "password": obfuscated, "monitor": bool}
        self._accounts: list[dict] = []
        self._loading_table = False

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
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 120)
        self.delay_spin.setSuffix(" 秒")
        game_form.addRow("啟動後等待：", self.delay_spin)
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

        # --- 按鈕 ---
        btn_row = QHBoxLayout()
        self.launch_btn = QPushButton("啟動遊戲")
        self.launch_btn.clicked.connect(self._launch_game)
        self.login_btn = QPushButton("啟動並自動登入（用上方帳密）")
        self.login_btn.setProperty("primary", True)  # 主要動作 → 主色（見 app/theme.py）
        self.login_btn.clicked.connect(self._launch_and_login)
        btn_row.addWidget(self.launch_btn)
        btn_row.addWidget(self.login_btn)
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
        self.delay_spin.setValue(int(config.get("login.delay_sec", 8)))

    def _save_settings(self) -> None:
        config.set("login.exe_path", self.exe_edit.text().strip())
        config.set("login.delay_sec", self.delay_spin.value())
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

    def _launch_and_login(self) -> None:
        self._save_settings()
        account = self.account_edit.text().strip()
        password = self.password_edit.text()
        if not account or not password:
            QMessageBox.warning(self, "缺少帳密", "請在上方輸入（或雙擊清單載入）帳號與密碼。")
            return
        if not self._launch_game():
            return
        self.login_btn.setEnabled(False)
        self._worker = LoginWorker(account, password, self.delay_spin.value())
        self._worker.status.connect(self._set_status)
        self._worker.finished_ok.connect(self._on_login_done)
        self._worker.failed.connect(self._on_login_failed)
        self._worker.start()

    def _on_login_done(self) -> None:
        self._set_status("自動登入完成")
        self.login_btn.setEnabled(True)

    def _on_login_failed(self, msg: str) -> None:
        self._set_status("自動登入失敗")
        self.login_btn.setEnabled(True)
        QMessageBox.warning(self, "自動登入失敗", msg)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def on_close(self) -> None:
        self._save_settings()
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
