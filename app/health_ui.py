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

import os

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core import health


class HealthWorker(QThread):
    """背景跑一次自我監察。壞掉時把項目清單送出來。

    ⚠ 關視窗時要能**中途收手**：`health.check()` 五台要跑好幾秒，關程式時
      沒人等它就會出現「QThread: Destroyed while thread is still running」
      → 0xC0000409 原生當機（見 `start()` 的說明）。所以掃描全程帶
      `should_stop`，`stop()` 一叫就在下一個記憶體區塊邊界停下來。
    """

    broken = Signal(object)      # list[str]
    ok = Signal(object)          # Report

    def run(self) -> None:
        try:
            rep = health.check(should_stop=self.isInterruptionRequested)
        except Exception:                      # noqa: BLE001
            return                             # 監察本身出錯 —— 不要嚇使用者
        if self.isInterruptionRequested():
            return                             # 中途被叫停 —— 結論不完整，不下判斷
        if rep.skipped:
            return
        bad = rep.broken
        if bad:
            self.broken.emit(bad)
        else:
            self.ok.emit(rep)

    def stop(self, wait_ms: int = 8000) -> None:
        """請它收手並等它結束。關視窗前一定要叫。"""
        self.requestInterruption()
        self.wait(wait_ms)


def start(parent) -> HealthWorker | None:
    """開始背景監察。回傳 worker（呼叫端要留著參考，不然會被回收）。

    ⚠⚠ **無頭模式一定要跳過**（`--selftest` 會設 offscreen）。
      冒煙測試建好主視窗就馬上結束，而這條執行緒還在掃五台分身的記憶體
      （`health.check()` 要 **10 秒**）—— 物件被解構時 Qt 丟
      「QThread: Destroyed while thread is still running」，
      Windows 直接以 **0xC0000409（STATUS_STACK_BUFFER_OVERRUN）原生當機**，
      Python 的例外攔截完全接不到，crash.log 也不會留東西。

      症狀就是「打包後的 exe 沒有載入分頁」，跟真正的白屏問題長得一模一樣，
      很容易誤判成打包遺漏模組。實測 0.3.6 和 0.4.0 兩版都中，所以
      **0.3.6 才會 commit 了版號卻始終沒發布**。

      `app/update_ui.py` 早就有同一道防護（那邊也踩過），這裡漏掉了。
    """
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return None
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
