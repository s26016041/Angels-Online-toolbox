"""背景自我監察：發現讀取邏輯壞掉就通知使用者並關掉程式。

為什麼要關掉程式
----------------
壞掉的時候讀到的是**垃圾數值**，不是「沒有數值」。繼續開著會讓使用者照著錯的
血量、錯的怪物清單、錯的能量做決定 —— 那比直接停下來糟。所以確認壞掉之後
就跳一個只有「確定」的視窗，按下去結束程式。

⚠⚠ 誤報的代價很高（把人家的程式關掉），所以判定條件很保守：
   一個項目要在**所有分身**上都失敗才算壞（見 app/core/health.py 的說明）。
   沒開遊戲時完全不檢查。

在**背景執行緒**跑，開程式時不會卡住畫面。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core import health


class HealthWorker(QThread):
    """背景跑一次自我監察。壞掉時把項目清單送出來。"""

    broken = Signal(object)      # list[str]
    ok = Signal(object)          # Report

    def run(self) -> None:
        try:
            rep = health.check()
        except Exception:                      # noqa: BLE001
            return                             # 監察本身出錯 —— 不要嚇使用者
        if rep.skipped:
            return
        bad = rep.broken
        if bad:
            self.broken.emit(bad)
        else:
            self.ok.emit(rep)


def start(parent) -> HealthWorker:
    """開始背景監察。回傳 worker（呼叫端要留著參考，不然會被回收）。"""
    worker = HealthWorker(parent)
    worker.broken.connect(lambda items: _warn_and_quit(parent, items))
    worker.start(QThread.LowPriority)
    return worker


def _warn_and_quit(parent, items) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle("無法讀取遊戲資料")
    box.setText("工具箱讀不到遊戲的資料了。")
    box.setInformativeText(
        "多半是遊戲更新過，程式裡的位址對不上了。\n"
        "這種情況下顯示出來的數值會是錯的，所以先停下來。\n\n"
        "請等作者推出新版本。\n\n"
        "按「確定」會關閉工具箱。")
    box.setDetailedText("讀不到的項目：\n" + "\n".join(f"· {i}" for i in items))
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()
    QApplication.quit()
