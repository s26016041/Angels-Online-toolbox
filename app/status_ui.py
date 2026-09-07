"""主視窗最下面那條狀態列上的常駐提示（2026-09-07 使用者要求搬到這裡）。

平常**完全不顯示**，只在出事時亮橘字：

* **⚠ 遊戲更新了** —— AOB 自動定位有項目失敗（相關功能已停用），或資料表
  戳記說遊戲換版了（寫死表還沒重新核對）。
  ⚠ 「偵測到遊戲改版，已自動重新定位 N 個位址」那種**成功**的情況不顯示 ——
  使用者 2026-09-07 定：綠字沒人要看，只有真的出事才要說話。
  兩種原因說的是同一件事（遊戲改版了），所以文字統一，細節放滑鼠提示。
* **⚠ 偵測到有新版** —— 每 10 分鐘背景問一次 GitHub 的最新 Release。
  ⛔ **只顯示，不下載、不重啟**：「用到一半不重查（強制更新）」是使用者
  2026-08-17 定的規矩，那條完全沒動（見 app/update_ui.py 檔頭）。掛機／刷副本
  做到一半被硬生生重啟過一次就夠了。

放在狀態列的**右邊**（addPermanentWidget）：左邊是 showMessage 的臨時訊息
（更新失敗提示之類），兩邊互不覆蓋。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QLabel

from app import __version__, theme
from app.core import updater
from app.game import locate, tablestamp

REFRESH_MS = 3000               # 定位／資料表狀態多久刷一次（純讀已算好的報告，很便宜）
RELEASE_MS = 10 * 60 * 1000     # 問 GitHub 的間隔（使用者 2026-09-07 定：每 10 分鐘）

PATCH_TEXT = "⚠ 遊戲更新了"
NEW_VER_TEXT = "⚠ 偵測到有新版"


class ReleaseThread(QThread):
    """背景問 GitHub 有沒有比自己新的版本。

    ⚠ 一定要背景做：連線最久會等 `updater.TIMEOUT`（15 秒），放在 UI 執行緒
      就是整個視窗凍住 15 秒。
    """

    done = Signal(bool)

    def run(self) -> None:
        try:
            info = updater.latest_release()
            ok = bool(info) and updater.is_newer(info["version"], __version__)
        except Exception:                      # noqa: BLE001
            ok = False                         # 沒網路／被限流：安靜當作沒新版
        self.done.emit(ok)


class StatusStrip:
    """掛在主視窗狀態列右邊的那個標籤（見檔頭）。"""

    def __init__(self, parent) -> None:
        self._parent = parent
        self._label = QLabel("")
        self._new_ver = False                  # GitHub 上有沒有更新的版本
        self._check: ReleaseThread | None = None
        # ⚠⚠ 收工的執行緒先擱著別丟參考，理由同 update_ui.UpdateManager：
        #   槽跑到時 run() 常常還在收尾，這時被回收＝QThread 在執行中被解構
        #   →「Destroyed while thread is still running」→ 原生當機。
        self._retired: list[QThread] = []
        bar = parent.statusBar()
        if bar is not None:
            bar.addPermanentWidget(self._label)

        self._timer = QTimer(parent)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)
        self._refresh()

        self._rel_timer = QTimer(parent)
        self._rel_timer.timeout.connect(self._run_check)
        # 無頭模式（--selftest）不要碰網路：冒煙測試建好視窗馬上結束，
        # 執行緒還連著網路沒收完會讓 Qt 中止行程（update_ui 踩過）。
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            self._rel_timer.start(RELEASE_MS)

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        """把「遊戲改版」與「有新版」兩件事湊成一行顯示（都沒有就空白）。"""
        parts, tips = [], []
        why = self._patch_reason()
        if why:
            parts.append(PATCH_TEXT)
            tips.append(why)
        if self._new_ver:
            parts.append(NEW_VER_TEXT)
            tips.append(f"GitHub 上有比 {__version__} 新的版本 —— 重開工具箱就會自動更新")
        self._label.setText("　".join(parts))
        # 細節（哪幾個位址掉了／該跑哪支工具）不佔畫面，放滑鼠提示裡
        self._label.setToolTip("\n".join(tips))
        self._label.setStyleSheet(f"color: {theme.WARN};" if parts else "")

    @staticmethod
    def _patch_reason() -> str:
        """真的有問題才回一句原因；一切正常（含「已自動重新定位」）回空字串。

        ⚠ 沒接上遊戲時**一律閉嘴**：`locate.failed()` 在還沒掃過時會回
          「定位還沒執行過」（那是給自我監察用的，不是改版）——這條槓沒開遊戲
          也看得到，照抄會變成一開工具箱就亮橘字的假警報。
        """
        if locate.image_identity() is None:
            return ""
        failed = locate.failed()
        if failed:
            # ⚠ 函式位址驗不過會被清成 0＝該功能停用（不是沿用舊值），
            #   資料位址才是沿用舊值。見 app/game/locate.py 檔頭。
            return (f"有 {len(failed)} 個遊戲位址定位失敗（相關功能已停用）："
                    + "、".join(failed[:3]))
        # 位址跟得上 ≠ 資料表跟得上：AOB 救位址，救不了抄來的內容（射程／地圖編號）。
        # 這條要一直亮到重新核對＋蓋章為止。
        return tablestamp.check() or ""

    # ------------------------------------------------------------------
    def _run_check(self) -> None:
        """問一次 GitHub。上一輪還沒收完就跳過這輪。"""
        if self._check is not None:
            return
        self._check = ReleaseThread()
        self._check.done.connect(self._on_checked)
        self._check.start()

    def _on_checked(self, has_new: bool) -> None:
        self._retire(self._check)
        self._check = None
        self._new_ver = bool(has_new)
        self._refresh()

    def _retire(self, thread: QThread | None) -> None:
        """移出「現役」但留著參考，等它自己結束才放掉。"""
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
        """關閉程式前呼叫：停計時器、等背景執行緒收完。"""
        self._timer.stop()
        self._rel_timer.stop()
        for t in (self._check, *self._retired):
            if t is not None and t.isRunning():
                t.wait(3000)
        self._check = None
        self._retired.clear()
