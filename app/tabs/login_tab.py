"""自動登入分頁。

流程：
1. 啟動遊戲執行檔（若已在執行可略過）。
2. 等待登入視窗出現（可設定延遲秒數）。
3. 依序輸入帳號 → Tab → 密碼 → Enter（可設定，並非所有遊戲版面相同，
   之後可依實際登入畫面微調按鍵順序）。

自動化的按鍵輸入使用 pyautogui，採延遲載入：
即使沒安裝 pyautogui，這個分頁與整個程式仍可正常開啟，只是按下自動登入時會提示安裝。

安全提醒：勾選「記住帳密」會把密碼以 base64 混淆後存進設定檔，
這不是真正的加密，拿到設定檔的人可以還原。請自行斟酌。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.config import config
from app.tabs.base_tab import BaseTab


class LoginWorker(QThread):
    """在背景執行「等待 + 自動填入帳密」，避免卡住介面。"""

    status = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        account: str,
        password: str,
        delay_sec: int,
        parent=None,
    ) -> None:
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
        root = QVBoxLayout(self)

        # --- 遊戲設定 ---
        game_box = QGroupBox("遊戲設定")
        game_form = QFormLayout(game_box)

        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText(r"例如 C:\Angels Online\game.exe")
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

        # --- 帳號設定 ---
        cred_box = QGroupBox("帳號")
        cred_form = QFormLayout(cred_box)

        self.account_edit = QLineEdit()
        cred_form.addRow("帳號：", self.account_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        cred_form.addRow("密碼：", self.password_edit)

        self.remember_chk = QCheckBox("記住帳密（以 base64 混淆存於設定檔，非加密）")
        cred_form.addRow("", self.remember_chk)

        root.addWidget(cred_box)

        # --- 按鈕 ---
        btn_row = QHBoxLayout()
        self.launch_btn = QPushButton("啟動遊戲")
        self.launch_btn.clicked.connect(self._launch_game)
        self.login_btn = QPushButton("啟動並自動登入")
        self.login_btn.clicked.connect(self._launch_and_login)
        btn_row.addWidget(self.launch_btn)
        btn_row.addWidget(self.login_btn)
        root.addLayout(btn_row)

        # --- 狀態列 ---
        self.status_label = QLabel("就緒")
        self.status_label.setStyleSheet("color: gray;")
        root.addWidget(self.status_label)

        root.addStretch(1)

        self._worker: LoginWorker | None = None
        self._load_settings()

    # ------------------------------------------------------------------
    # 設定讀寫
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        self.exe_edit.setText(config.get("login.exe_path", ""))
        self.delay_spin.setValue(int(config.get("login.delay_sec", 8)))
        self.account_edit.setText(config.get("login.account", ""))
        remember = bool(config.get("login.remember", False))
        self.remember_chk.setChecked(remember)
        if remember:
            self.password_edit.setText(
                config.deobfuscate(config.get("login.password", ""))
            )

    def _save_settings(self) -> None:
        config.set("login.exe_path", self.exe_edit.text().strip())
        config.set("login.delay_sec", self.delay_spin.value())
        config.set("login.account", self.account_edit.text().strip())
        remember = self.remember_chk.isChecked()
        config.set("login.remember", remember)
        if remember:
            config.set(
                "login.password",
                config.obfuscate(self.password_edit.text()),
            )
        else:
            config.set("login.password", "")
        config.save()

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
            # cwd 設為執行檔所在目錄，許多遊戲需要從自身目錄啟動。
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
            QMessageBox.warning(self, "缺少帳密", "請輸入帳號與密碼。")
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
        # 儲存設定，並確保背景執行緒收尾。
        self._save_settings()
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
