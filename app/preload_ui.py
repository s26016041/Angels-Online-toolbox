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
    """跑預讀並顯示進度視窗。沒有遊戲開著就什麼都不做（不要跳空視窗）。"""
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
        dlg.close()
        worker.deleteLater()

    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    worker.start()
    dlg.exec()                                 # 擋在這裡直到 on_done 關掉它
    worker.wait(3000)
