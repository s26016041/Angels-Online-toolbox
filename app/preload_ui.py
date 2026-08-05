"""開程式時的「讀取遊戲資料」進度視窗。

為什麼要有它
------------
預讀角色名與位址校正一台要 0.4~0.9 秒，五台就是三、四秒。
以前這件事是**第一次切到分頁時**才做，使用者看到的是「切過去卡一下」，
而且每個分頁各卡一次、還不知道在等什麼。

改成開程式時做完並且**把進度顯示出來**：等待變成看得見的、只發生一次的。

實作重點
--------
* 掃描在**工作執行緒**上跑，UI 執行緒只負責畫進度條 —— 不然視窗會整個凍住。
* 找不到遊戲、或掃描出錯，都**直接跳過**：預讀只是加速，不能擋住程式啟動。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QProgressDialog

from app.core import preload


class _Worker(QThread):
    """在背景跑預讀。progress 帶 (已完成, 總數, 現在在做誰)。"""

    progress = Signal(int, int, str)
    done = Signal(int)

    def run(self) -> None:
        try:
            got = preload.refresh(
                progress=lambda i, n, who: self.progress.emit(i, n, who))
            self.done.emit(len(got))
        except Exception:                      # noqa: BLE001
            self.done.emit(0)                  # 預讀失敗不影響程式使用


def run_with_dialog(parent) -> None:
    """跑預讀並顯示進度視窗。沒有遊戲開著就什麼都不做（不要跳空視窗）。

    ★ 設環境變數 `AOTB_NO_PRELOAD=1` 可以整段跳過 —— 診斷打包版當機時用，
      平常不會有人設。
    """
    if os.environ.get("AOTB_NO_PRELOAD") == "1":
        return
    if not preload.windows():
        return

    dlg = QProgressDialog("正在讀取遊戲資料…", "", 0, 100, parent)
    dlg.setWindowTitle("讀取中")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setCancelButton(None)                  # 中途取消會讓分頁拿到半套資料
    dlg.setMinimumDuration(0)                  # 立刻顯示，不要等 4 秒才冒出來
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setValue(0)

    worker = _Worker()

    def on_progress(i: int, n: int, who: str) -> None:
        dlg.setMaximum(max(1, n))
        dlg.setValue(i)
        dlg.setLabelText(
            f"正在讀取遊戲資料…（{i}/{n}）" + (f"\n{who}" if who else ""))

    def on_done(count: int) -> None:
        # ⚠ 這裡**不要** deleteLater()：這一刻 run() 還在收尾，而且我們是在
        #   dlg.exec() 的巢狀事件迴圈裡，延遲刪除何時執行並不確定。
        #   等下面 wait() 真的等到它結束再刪。
        dlg.close()

    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    # ⚠⚠ **一定要留住這個參考**。它原本只是區域變數，函式一回傳就可能被回收 ——
    #   QThread 物件在執行緒還在跑的時候被 destroy，Windows 直接丟
    #   0xC0000409（STATUS_STACK_BUFFER_OVERRUN）**原生當機**，
    #   Python 的例外攔截完全接不到，crash.log 也不會有東西。
    if parent is not None:
        parent._preload_worker = worker
    worker.start()
    dlg.exec()                                 # 擋在這裡直到 on_done 關掉它
    # ⚠⚠ 原本是 wait(3000)。**打包成 exe 之後預讀要 10 秒**（比原始碼慢很多），
    #   3 秒等不完就往下走，接著物件被回收 → 就是上面那個原生當機。
    #   症狀：exe 開起來白屏／沒視窗，而且 `--selftest` 必定失敗。
    #   實測 0.3.6 與 0.4.0 兩版一模一樣，所以這是既有問題不是新的。
    #   → 等到它真的結束為止；真的卡住才強制收尾。
    if not worker.wait(60000):
        worker.requestInterruption()
        worker.terminate()
        worker.wait(3000)
    # 這時執行緒已經結束（或被強制收掉），可以安全地放掉它了。
    if parent is not None:
        parent._preload_worker = None
    worker.deleteLater()
