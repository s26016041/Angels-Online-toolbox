"""自動更新的介面：背景檢查、詢問、下載進度、重新啟動。

檢查在背景執行緒做（連 GitHub 可能要幾秒），不擋住視窗開啟。
沒網路 / 查不到 / 已是最新 → 完全安靜，不打擾使用者。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from app import __version__
from app.config import config
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

    def __init__(self, parent) -> None:
        self._parent = parent
        self._check: CheckThread | None = None
        self._dl: DownloadThread | None = None
        self._dlg: QProgressDialog | None = None
        self._info: dict | None = None

    def start(self) -> None:
        """開場呼叫。開發模式或使用者關掉自動檢查就什麼都不做。"""
        updater.clean_leftovers()
        if not updater.is_frozen():
            return
        if not config.get("update.auto_check", True):
            return
        self._check = CheckThread()
        self._check.done.connect(self._on_checked)
        self._check.start()

    # ------------------------------------------------------------------
    def _on_checked(self, info) -> None:
        self._check = None
        if not info:
            return
        self._info = info
        notes = info.get("notes") or ""
        if len(notes) > 400:
            notes = notes[:400] + "…"
        box = QMessageBox(self._parent)
        box.setWindowTitle("有新版本")
        box.setText(f"目前版本 {__version__}，最新版本 {info['version']}。\n"
                    "要現在更新嗎？更新後程式會自動重新啟動。")
        if notes:
            box.setInformativeText(notes)
        yes = box.addButton("立即更新", QMessageBox.AcceptRole)
        box.addButton("稍後再說", QMessageBox.RejectRole)
        never = box.addButton("不再自動檢查", QMessageBox.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is never:
            config.set("update.auto_check", False)
            config.save()
            return
        if clicked is not yes:
            return
        self._start_download()

    def _start_download(self) -> None:
        cur = updater.exe_path()
        dest = cur.with_suffix(cur.suffix + ".new")
        self._dlg = QProgressDialog("下載新版本…", "取消", 0, 100, self._parent)
        self._dlg.setWindowTitle("更新中")
        self._dlg.setMinimumDuration(0)
        self._dlg.setAutoClose(False)
        self._dlg.setValue(0)
        self._dl = DownloadThread(self._info, dest)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(lambda ok: self._on_downloaded(ok, dest))
        self._dl.start()

    def _on_progress(self, got: int, total: int) -> None:
        if not self._dlg:
            return
        if total:
            self._dlg.setValue(int(got / total * 100))
            self._dlg.setLabelText(
                f"下載新版本…　{got / 1e6:.1f} / {total / 1e6:.1f} MB")
        else:
            self._dlg.setLabelText(f"下載新版本…　{got / 1e6:.1f} MB")

    def _on_downloaded(self, ok: bool, dest) -> None:
        self._dl = None
        if self._dlg:
            self._dlg.close()
            self._dlg = None
        if not ok:
            QMessageBox.warning(
                self._parent, "更新失敗",
                "下載沒有完成或檔案不完整，這次先跳過。\n"
                "可以稍後再試，或到 GitHub Releases 手動下載。")
            return
        if not updater.apply_and_restart(dest):
            QMessageBox.warning(
                self._parent, "更新失敗",
                "換檔時失敗，已保留原本的版本。\n"
                "若程式安裝在唯讀資料夾（例如 Program Files），"
                "請改用系統管理員身分執行，或手動下載。")
            return
        QApplication.quit()
