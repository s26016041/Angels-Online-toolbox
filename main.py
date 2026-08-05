"""天使之戀工具箱 — 程式進入點。

執行方式：
    py main.py              正常啟動 GUI
    py main.py --selftest   自我測試：不開視窗，離線建立主視窗並檢查分頁有無載入，
                            成功回傳 0、失敗回傳非 0（本地打包後的冒煙測試會用它）。
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _log_dir() -> Path:
    """崩潰記錄放使用者 AppData，打包成 exe 後也寫得進去。"""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = Path(base) / "AngelsOnlineToolbox"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(".")
    return path


def _install_excepthook() -> None:
    """全域例外攔截：把 traceback 寫進 log，並盡量跳訊息框。

    打包成 --windowed 的 exe 沒有主控台，未攔截的例外會靜默死掉（就是這次
    白屏 / 閃退看不到原因的根源）。裝了這個之後，未來任何未捕捉例外都會留下
    紀錄檔，也會彈出可讀的錯誤視窗。
    """

    def handler(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            log_path = _log_dir() / "crash.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
                f.write(text)
        except OSError:
            log_path = None

        # 先印到主控台（除錯版看得到），再嘗試跳訊息框（正式版看得到）。
        # ⚠ 打包成 --windowed 的 exe 沒有主控台，sys.stderr 是 None ——
        #   直接 .write() 會在例外處理器裡再炸一次，訊息框就永遠跳不出來。
        if sys.stderr is not None:
            sys.stderr.write(text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Critical)
                box.setWindowTitle("程式發生未預期的錯誤")
                box.setText("程式遇到未處理的例外，即將關閉。")
                detail = text
                if log_path is not None:
                    detail = f"紀錄檔：{log_path}\n\n{text}"
                box.setDetailedText(detail)
                box.exec()
        except Exception:
            pass

    sys.excepthook = handler


def _build_main_window():
    """建立並回傳主視窗（供正常啟動與自我測試共用）。"""
    from app import __app_name__
    from app.main_window import MainWindow
    from PySide6.QtWidgets import QApplication

    _trace("建 QApplication…")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    _trace("QApplication 好了")

    from app.theme import apply_theme
    apply_theme(app)
    _trace("主題套好，開始 MainWindow()")
    w = MainWindow()
    _trace("MainWindow() 回來了")
    return app, w


def _selftest() -> int:
    """離線建立主視窗，檢查分頁確實有載入。回傳 0=成功、1=失敗。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app import __version__

    _app, window = _build_main_window()
    count = len(getattr(window, "_loaded_tabs", []))
    print(f"[selftest] 版本 = {__version__}")
    print(f"[selftest] 已載入分頁數 = {count}")
    if count <= 0:
        print("[selftest] 失敗：沒有任何分頁被載入（打包遺漏了 app.* 子模組？）")
        return 1
    print("[selftest] 成功：分頁載入正常。")
    return 0


def _trace(stage: str) -> None:
    """啟動階段追蹤。設 AOTB_TRACE=1 才寫，用來定位打包版卡在哪一步。"""
    if os.environ.get("AOTB_TRACE") != "1":
        return
    try:
        with open(_log_dir() / "startup.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S.%f} {stage}\n")
    except OSError:
        pass


def main() -> int:
    _trace("main 進入")
    _install_excepthook()
    _trace("例外攔截裝好")

    if "--selftest" in sys.argv:
        return _selftest()

    _trace("開始建主視窗")
    app, window = _build_main_window()
    _trace("主視窗建好")
    window.show()
    _trace("show() 呼叫完")
    rc = app.exec()
    _trace(f"事件迴圈結束 rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
