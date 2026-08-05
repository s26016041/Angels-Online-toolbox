"""自動更新的介面：背景檢查、強制更新、下載進度、重新啟動。

**強制更新，不詢問** —— 查到新版就直接下載、換檔、重啟。只顯示進度，沒有選項。
理由是記憶體位址與物品對照表會隨遊戲改版而失效，舊版留在使用者手上只會顯示錯的
資料或完全抓不到，讓人以為程式壞了。

檢查在背景執行緒做（連 GitHub 可能要幾秒），不擋住視窗開啟。
沒網路 / 查不到 / 已是最新 → 完全安靜。
更新失敗 → 提示一下就繼續用舊版，不能因為更新不成功就讓人沒得用。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from app import __version__
from app.core import updater


class CheckThread(QThread):
    """背景查有沒有新版。"""

    done = Signal(object)   # dict 或 None

    def run(self) -> None:
        try:
            self.done.emit(updater.check())
        except Exception:
            self.done.emit(None)


class DownloadThread(QThread):
    """背景下載新版 exe。"""

    progress = Signal(int, int)
    done = Signal(bool)

    def __init__(self, info: dict, dest) -> None:
        super().__init__()
        self._info = info
        self._dest = dest

    def run(self) -> None:
        try:
            ok = updater.download(
                self._info, self._dest,
                progress=lambda got, total: self.progress.emit(got, total))
        except Exception:
            ok = False
        self.done.emit(ok)


class UpdateManager:
    """掛在主視窗上：開場檢查一次，有新版就問要不要更新。"""

    # 多久重查一次。這是掛機監控工具，使用者會開著好幾個小時不關 —— 只在啟動時
    # 檢查的話，那些人永遠不會更新，「強制更新」等於形同虛設。
    RECHECK_MS = 30 * 60 * 1000

    def __init__(self, parent) -> None:
        self._parent = parent
        self._check: CheckThread | None = None
        self._dl: DownloadThread | None = None
        self._dlg: QProgressDialog | None = None
        self._info: dict | None = None
        self._timer: QTimer | None = None
        # ⚠⚠ 收工的執行緒先擱這裡，**不要直接把參考丟掉**。
        #   `done` 是排隊送過來的，槽跑到時 run() 往往還在收尾；那一刻要是
        #   Python 把最後一個參考回收掉，C++ 端的 QThread 就在執行中被解構
        #   → 「Destroyed while thread is still running」→ 原生當機。
        self._retired: list[QThread] = []

    def start(self) -> None:
        """開場呼叫。只有開發模式與無頭模式不做，其餘一律檢查並強制更新。"""
        # ⚠ clean_leftovers() 會 rmtree 每個殘留的 _MEIxxxxxx（一個約 80MB，
        #   實際看過 9 個）——那是**開視窗前**的同步磁碟操作，會拖慢啟動。
        #   純清理，晚三秒做完全沒差，所以丟到視窗出來之後。
        QTimer.singleShot(3000, updater.clean_leftovers)
        if not updater.is_frozen():
            return
        # 無頭模式（--selftest 會設 offscreen）不要查：冒煙測試建好視窗就馬上結束，
        # 這條執行緒還連著網路沒收完，Qt 會丟「QThread: Destroyed while running」
        # 並中止行程，害冒煙測試誤判成「分頁載入失敗」。實際踩過。
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        self._run_check()
        self._timer = QTimer(self._parent)
        self._timer.timeout.connect(self._run_check)
        self._timer.start(self.RECHECK_MS)

    def _run_check(self) -> None:
        """查一次。上一輪還沒收完、或正在下載時就跳過這輪。"""
        if self._check is not None or self._dl is not None:
            return
        self._check = CheckThread()
        self._check.done.connect(self._on_checked)
        self._check.start()

    def _retire(self, thread: QThread | None) -> None:
        """把執行緒移出「現役」但**留著參考**，等它自己結束才放掉。"""
        if thread is None:
            return
        self._retired.append(thread)
        thread.finished.connect(lambda t=thread: self._forget(t))
        if thread.isFinished():
            self._forget(thread)

    def _forget(self, thread: QThread) -> None:
        if thread in self._retired:
            self._retired.remove(thread)
            thread.deleteLater()

    def stop(self) -> None:
        """關閉程式前呼叫：等背景執行緒收完，別讓 Qt 在解構時抱怨。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        for t in (self._check, self._dl, *self._retired):
            if t is not None and t.isRunning():
                t.wait(3000)
        self._check = None
        self._dl = None
        self._retired.clear()

    # ------------------------------------------------------------------
    def _on_checked(self, info) -> None:
        """查到新版就直接更新，不問使用者。"""
        self._retire(self._check)
        self._check = None
        if not info:
            # 查不到有兩種：已是最新（正常）、或連線/憑證出問題（要讓人看得到）。
            # 以前兩種都靜靜略過，結果使用者更新不了時完全沒有線索。
            err = updater.last_error()
            if err:
                self._show_status(f"⚠ 檢查更新失敗（{err}）—— 目前使用 {__version__}")
            return
        self._info = info
        self._start_download()

    def _show_status(self, text: str) -> None:
        """把訊息寫到主視窗狀態列（沒有狀態列就算了，不能因此壞掉）。"""
        try:
            bar = self._parent.statusBar()
        except Exception:
            return
        if bar is not None:
            bar.showMessage(text, 30000)

    def _start_download(self) -> None:
        cur = updater.exe_path()
        dest = cur.with_suffix(cur.suffix + ".new")
        # 強制更新：沒有取消鈕（傳 None）、應用程式層級 modal，避免使用者在換檔
        # 途中去操作視窗。下載完會自己重啟。
        self._dlg = QProgressDialog(
            f"正在更新到 {self._info['version']}…\n完成後會自動重新啟動。",
            None, 0, 100, self._parent)
        self._dlg.setWindowTitle("更新中")
        self._dlg.setWindowModality(Qt.ApplicationModal)
        self._dlg.setMinimumDuration(0)
        self._dlg.setAutoClose(False)
        self._dlg.setAutoReset(False)
        # 關掉標題列的關閉鈕，避免關掉對話框以為取消了、其實還在下載
        self._dlg.setWindowFlags(
            (self._dlg.windowFlags() | Qt.CustomizeWindowHint)
            & ~Qt.WindowCloseButtonHint)
        self._dlg.setValue(0)
        self._dl = DownloadThread(self._info, dest)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(lambda ok: self._on_downloaded(ok, dest))
        self._dl.start()

    def _on_progress(self, got: int, total: int) -> None:
        if not self._dlg:
            return
        ver = self._info["version"] if self._info else ""
        if total:
            self._dlg.setValue(int(got / total * 100))
            self._dlg.setLabelText(
                f"正在更新到 {ver}…　{got / 1e6:.1f} / {total / 1e6:.1f} MB\n"
                "完成後會自動重新啟動。")
        else:
            self._dlg.setLabelText(
                f"正在更新到 {ver}…　{got / 1e6:.1f} MB\n完成後會自動重新啟動。")

    def _on_downloaded(self, ok: bool, dest) -> None:
        self._retire(self._dl)
        self._dl = None
        if self._dlg:
            self._dlg.close()
            self._dlg = None
        # 更新失敗不能讓人沒得用 —— 講清楚原因就讓他繼續用舊版
        if not ok:
            QMessageBox.warning(
                self._parent, "更新失敗",
                f"下載 {self._info['version']} 沒有完成或檔案不完整，"
                "這次先用目前的版本。\n下次開啟會再試一次，"
                "也可以到 GitHub Releases 手動下載。")
            return
        if not updater.apply_and_restart(dest):
            QMessageBox.warning(
                self._parent, "更新失敗",
                "換檔時失敗，已保留原本的版本。\n"
                "若程式放在唯讀資料夾（例如 Program Files），"
                "請改用系統管理員身分執行，或手動下載。")
            return
        QApplication.quit()
