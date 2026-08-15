"""分頁基底類別。

所有工具分頁都繼承 BaseTab。主視窗靠這個共同介面來自動載入分頁：
每個分頁只要設定 TAB_TITLE（分頁標題）與 ORDER（排序），
並實作 build_ui() 建立自己的介面即可。
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget

from app.core import charname, preload


def fit_spin(spin) -> None:
    """把數字框調成「**最大的那個值**也放得下」的寬度。

    ★ 使用者要求所有文字都要完整顯示。原本各分頁一律寫死 60~70px，
      實測不夠 —— 上下箭頭會把數字擠掉（使用者回報「框框被砍到一半」）。
    ⚠ 不能照**目前的值**算：3600 比 60 寬，用 60 算出來的寬度換到 3600 就切字。
    ⚠ 一定要在**主題套用之後**呼叫，不然量到的是沒有內距的尺寸。
    """
    keep = spin.value()
    spin.setValue(spin.maximum())
    need = spin.sizeHint().width()
    spin.setValue(keep)
    spin.setFixedWidth(need)


def no_elide(lst) -> None:
    """清單不要把字縮成「曼陀羅怪…」，太長就給水平捲軸讓人捲。"""
    lst.setTextElideMode(Qt.ElideNone)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def fit_list(box, lst, sample: str) -> None:
    """讓清單至少放得下 `sample` 這麼長的一行，寬度用**實際字型**量。

    ★ 原本欄寬是寫死的（190/240），換字型或遇到長名字就會切字。
    """
    pad = (lst.frameWidth() * 2 + 26                      # 邊框 + 內距
           + lst.verticalScrollBar().sizeHint().width())  # 直捲軸佔的位置
    box.setMinimumWidth(lst.fontMetrics().horizontalAdvance(sample) + pad)


class BaseTab(QWidget):
    """所有功能分頁的基底類別。

    子類別需要覆寫：
        TAB_TITLE：顯示在分頁上的標題文字。
        ORDER：分頁排序（數字越小越靠左），可選。
        build_ui()：建立此分頁的介面內容。
    """

    TAB_TITLE: str = "未命名分頁"
    ORDER: int = 100
    # 設為 False 可讓自動載入器略過此分頁（例如尚未完成的功能）。
    ENABLED: bool = True

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.build_ui()

    def build_ui(self) -> None:
        """建立分頁介面。子類別必須覆寫。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 必須實作 build_ui() 方法"
        )

    def on_show(self) -> None:
        """分頁被切換到（顯示）時呼叫，可選覆寫。"""

    def on_close(self) -> None:
        """應用程式關閉前呼叫，用於釋放資源，可選覆寫。"""


class ClientWatchMixin:
    """背景自動對帳「現在開著哪些遊戲分身」——取代「重新偵測分身」按鈕。

    每 WATCH_MS 對一次帳（跟遊戲總控同節奏）：關掉的分身收走、新開的補上、
    **沒動的完全不碰**（正在掛機／生產的分頁絕不重建）。

    子類別要提供：
        self._pages : dict[pid, page]，page 要有 account / char_name / run_cb / sc
        self.tabs   : QTabWidget（子分頁容器）
        _client_new(w)    接上一台新分身（w＝preload.windows() 的視窗物件）
        _client_gone(pid) 收掉一台已關閉的分身（含把 self._pages 裡的項目拿掉）
    並在 _client_new 裡把 run_cb.clicked 接到 _note_intent()。

    ## 勾勾的「意向記憶」（使用者要求）

    「開始掛機／開始自動生產」的勾選狀態記在**程式記憶體**（帳號 → bool）：
    分身關掉再開回來、**登入之後**會自動幫他勾回去。**不寫進 config** ——
    工具箱重開一律回到預設關閉（使用者明確要求：這是 session 狀態不是設定）。
    ★ 只有**使用者親手點**勾選框才更新意向（接的是 clicked 不是 toggled）——
      程式自己放掉勾勾（halt、分身消失時的收尾）不算改變心意，
      這樣「掛機中遊戲當掉 → 重開 → 登入」才會自動接回去繼續掛。

    ## 角色名背景重讀

    分身剛開還在登入畫面時讀不到角色名（＝暫時用帳號當標籤），這裡每
    RESOLVE_RETRY_SECS 丟一條**背景執行緒**重讀（`preload.name_of(force=True)`
    一台約 0.7 秒，放 GUI 執行緒會一頓一頓的）。讀到真名 → 換分頁標籤、
    補套意向。⚠ 執行緒還掛著時那台的 scanner **不准關**（正卡在
    ReadProcessMemory 時控制碼被關掉＝控制碼可能被回收給別的物件），
    _client_gone 收尾前要問 `_sc_busy(pid)`，忙就寧可漏關（行程結束 OS 會收）。
    """

    WATCH_MS = 3000
    RESOLVE_RETRY_SECS = 10.0

    def _watch_init(self) -> None:
        self._watch_timer: QTimer | None = None
        self._intent: dict[str, bool] = {}      # 帳號 → 使用者最後一次親手勾/取消
        self._nm_busy: set[int] = set()         # 正在背景讀名字的 pid
        self._nm_done: dict[int, str] = {}      # 背景讀完等 GUI 收的結果
        self._nm_next: dict[int, float] = {}    # pid → 下次允許重讀的時刻

    def _watch_start(self) -> None:
        """第一次切到分頁才開始跑（保持懶載入：沒開過這個分頁就完全不動）。
        開始之後就算切去別的分頁也繼續對帳 —— 掛機中分身當掉/重開時人多半
        正看著別頁。"""
        if self._watch_timer is not None:
            return
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start(self.WATCH_MS)
        self._watch_tick()

    def _watch_stop(self) -> None:
        if self._watch_timer is not None:
            self._watch_timer.stop()
            self._watch_timer = None

    def _sc_busy(self, pid: int) -> bool:
        return pid in self._nm_busy

    # -- 意向記憶 ------------------------------------------------------
    def _note_intent(self, page) -> None:
        """使用者親手點了主勾選框 → 記住這個帳號「想不想跑」。"""
        if page.account:
            self._intent[page.account] = page.run_cb.isChecked()

    def _name_ok(self, page) -> bool:
        """角色名解出來了沒。還等於帳號＝多半還沒登入（或讀不到玩家物件）。"""
        return bool(page.char_name) and page.char_name != page.account

    def _maybe_resume(self, page) -> None:
        """意向說要跑、而且人已經登入（名字讀得到）→ 幫他把勾勾補回去。

        ⚠ 沒登入就不補：勾下去整條啟動流程會對著登入畫面空轉猛重試。
        等背景把名字解出來，_watch_tick 會再叫一次這裡。
        """
        if (self._intent.get(page.account) and self._name_ok(page)
                and not page.run_cb.isChecked()):
            page.run_cb.setChecked(True)

    # -- 對帳主迴圈 ----------------------------------------------------
    def _watch_tick(self) -> None:
        # 1. 收背景解出來的角色名（換標籤、補套意向）
        for pid, nm in list(self._nm_done.items()):
            self._nm_done.pop(pid, None)
            page = self._pages.get(pid)
            if page is None or not nm or nm == page.char_name:
                continue
            page.char_name = nm
            i = self.tabs.indexOf(page)
            if i >= 0:
                self.tabs.setTabText(i, nm)
            self._maybe_resume(page)

        # 2. 視窗對帳：關掉的收走、新開的補上（沒動的不碰）
        wins = {w.pid: w for w in preload.windows()}
        for pid in [p for p in list(self._pages) if p not in wins]:
            self._client_gone(pid)
        for pid, w in wins.items():
            page = self._pages.get(pid)
            if page is None:
                self._client_new(w)
                continue
            # 開在登入畫面的分身，視窗標題裡的帳號是登入後才有的 ——
            # 帳號一出現就整頁重建（設定是照帳號存的，空帳號那頁載不到）。
            # 這台一定沒在跑（沒登入），重建不會打斷任何事。
            acct = charname.account_from_title(w.title)
            if not page.account and acct:
                self._client_gone(pid)
                self._client_new(w)

        # 3. 名字還沒解出來的：丟背景執行緒重讀（節流、一個 pid 一條）
        now = time.monotonic()
        for pid, page in self._pages.items():
            if (self._name_ok(page) or pid in self._nm_busy
                    or now < self._nm_next.get(pid, 0.0)):
                continue
            self._nm_next[pid] = now + self.RESOLVE_RETRY_SECS
            self._nm_busy.add(pid)
            threading.Thread(
                target=self._nm_worker,
                args=(pid, page.sc, page.account or ""),
                daemon=True).start()

    def _nm_worker(self, pid: int, sc, acct: str) -> None:
        try:
            nm = preload.name_of(pid, sc, acct, force=True)
        except Exception:                          # noqa: BLE001
            nm = ""
        # 先放結果再解除 busy —— 反過來的話 _client_gone 可能在空窗關掉 scanner
        self._nm_done[pid] = nm or ""
        self._nm_busy.discard(pid)
