"""分頁基底類別。

所有工具分頁都繼承 BaseTab。主視窗靠這個共同介面來自動載入分頁：
每個分頁只要設定 TAB_TITLE（分頁標題）、GROUP（左側分類）與 ORDER（排序），
並實作 build_ui() 建立自己的介面即可。
"""
from __future__ import annotations

import threading
import time

# ★ 左側分類（主視窗照這個順序排）。2026-09-05 使用者定案：
#   開機與總控：自動登入、分身總控、遊戲總控、收益監控 —— 開遊戲第一件事、看全局
#   長時間自動化：自動掛機、自動刷副本、副本腳本製作、自動生產 —— 勾起來就掛著
#   角色雜務：領取每日、能量晶化、販賣裝備、強化裝備、活動 —— 按一下做完就走
#   開發工具：記憶體掃描、封包／登入攔截、視窗診斷 —— 使用者自己抓 bug 用（⛔ 不隱藏）
GROUP_LAUNCH = "開機與總控"
GROUP_AUTO = "長時間自動化"
GROUP_CHORES = "角色雜務"
GROUP_DEV = "開發工具"
GROUPS = (GROUP_LAUNCH, GROUP_AUTO, GROUP_CHORES, GROUP_DEV)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import charname, preload
from app.game import itemname

# 商城購買紀錄在記憶體裡最多留幾筆（session 狀態，工具箱重開清空）。
MALL_LOG_CAP = 500


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
    # ★ 左側分類（見 GROUPS）。2026-09-05 使用者：分頁太多不好選 → 主視窗改成
    #   「左邊分類、右邊該分類的分頁」。沒設就落在最後一個分類，不會不見。
    GROUP: str = "角色雜務"

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

    ## 「沒動」要驗身分，不能只看 pid（2026-08-15 斷線事故的教訓）

    斷網時所有分身退回登入畫面，pid 全都沒變；而一鍵登入是把帳號填進
    「**第一台空著的**視窗」（login_tab 的 find_client），重登之後帳號跟視窗的
    配對幾乎必然洗牌 —— 只認 pid 的對帳完全不會發現，結果就是拿 A 帳號的
    設定去指揮 B 的角色（實際發生：黑狐的分頁指揮著雪狐在跑）。
    所以每一拍都要拿**視窗標題裡的帳號**對身分：

        標題帳號消失      → 記進 _relog（退回登入畫面；回來的可能是別人）
        標題帳號 ≠ 頁帳號 → 整頁重建（設定照帳號存，舊頁整個不能用）
        _relog 後同帳號回來 → 角色名重解（自動登入固定進第 1 隻角色，
                              同帳號登回來的也可能是另一隻）

    ⚠ 換人時 preload 的「pid→角色名」快取一定要 forget()：不清的話重建的頁
      會從快取撿回上一個人的名字，而名字一旦解出就不再重讀（_name_ok）。

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
        # 背景讀完等 GUI 收的結果：pid → (當時的帳號, 角色名)。
        # ⚠ 要帶帳號：讀到一半分頁被重建（換帳號）時，舊帳號的結果必須丟棄，
        #   不然舊名字會黏到新帳號的分頁上、之後永遠不再重讀。
        self._nm_done: dict[int, tuple[str, str]] = {}
        self._nm_next: dict[int, float] = {}    # pid → 下次允許重讀的時刻
        self._relog: set[int] = set()           # 退回登入畫面過、等重新驗身分的 pid

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
        for pid, (acct, nm) in list(self._nm_done.items()):
            self._nm_done.pop(pid, None)
            page = self._pages.get(pid)
            # 帳號對不上＝讀到一半這台換了人（分頁已重建）→ 結果作廢，
            # 步驟 3 會用新帳號再讀一次。
            if (page is None or acct != page.account
                    or not nm or nm == page.char_name):
                continue
            page.char_name = nm
            i = self.tabs.indexOf(page)
            if i >= 0:
                self.tabs.setTabText(i, nm)
            self._maybe_resume(page)

        # 2. 視窗對帳：關掉的收走、新開的補上、**換了人的整頁重建**
        wins = {w.pid: w for w in preload.windows()}
        for pid in [p for p in list(self._pages) if p not in wins]:
            self._relog.discard(pid)
            self._client_gone(pid)
        for pid, w in wins.items():
            page = self._pages.get(pid)
            if page is None:
                self._client_new(w)
                continue
            acct = charname.account_from_title(w.title)
            if not acct:
                # 退回登入畫面（斷線／登出）。標題帳號是登入後才有的，
                # 現在什麼都不能斷定 —— 先記著，等有人登進來再驗身分。
                if page.account:
                    self._relog.add(pid)
                continue
            if acct != page.account:
                # 同一台視窗換了帳號（斷線後一鍵登入把帳號填進「第一台空的」，
                # 配對會洗牌；也涵蓋原本開在登入畫面、現在首次登入的頁）。
                # 設定是照帳號存的 —— 舊頁整個不能用，重建。
                # 重建會放掉勾勾，但意向照帳號記著，登入驗完名字會自動接回去。
                self._relog.discard(pid)
                preload.forget(pid)          # pid→角色名快取已是上一個人的
                self._nm_next.pop(pid, None)  # 新頁的名字立刻重解，不等節流
                self._client_gone(pid)
                self._client_new(w)
                continue
            if pid in self._relog:
                # 同帳號重新登入：角色可能換了（自動登入固定進第 1 隻）。
                # 名字退回「未解出」讓步驟 3 重讀；解出後換標籤、補套意向。
                self._relog.discard(pid)
                preload.forget(pid)
                page.char_name = page.account
                self._nm_next.pop(pid, None)

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
        self._nm_done[pid] = (acct, nm or "")
        self._nm_busy.discard(pid)


def mall_buys_dialog(parent, rows: list, who: str) -> QDialog:
    """「商城紀錄」單獨一個視窗（自動生產頁的按鈕、離線測試走這支）。

    ★ 內容本體在 `mall_buys_widget()` —— 掛機頁把同一份塞進「紀錄」視窗的
      一個分頁（2026-08-28 使用者要求三張表併成一顆按鈕）。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"商城購買紀錄 — {who}")
    v = QVBoxLayout(dlg)
    panel = mall_buys_widget(dlg, rows, who)
    v.addWidget(panel)
    dlg.resize(640, 420)
    dlg._head, dlg._tbl = panel._head, panel._tbl   # 給離線測試摸得到
    return dlg


def mall_buys_widget(parent, rows: list, who: str) -> QWidget:
    """「商城紀錄」表：時間／商城編號／商品／數量／花費(點數)＋總額。

    ★ 自動掛機與自動生產**共用這一份** —— 兩邊各畫一次的話，改欄位就會有
      一邊漏掉。每筆 = `(時間戳, 商城編號, 種類id, 數量, 點數)`。
    ⚠⚠ **不可以跟「商店紀錄」合併**：商店花金幣、商城花點數，
      幣別不同混在同一張表，總額就是錯的（使用者 2026-08-21 要求分開）。
    ⚠ 一次性快照：開的當下有什麼畫什麼（高頻改表是 qt-ui-pitfalls 的坑）。
    """
    rows = list(rows)[::-1]                     # 新的在上面
    total = sum(r[4] for r in rows)

    panel = QWidget(parent)
    v = QVBoxLayout(panel)
    v.setContentsMargins(0, 0, 0, 0)
    head = (f"總花費 {total:,} 點（共 {len(rows)} 筆）" if rows else
            "還沒有商城購買紀錄 —— 自動換球買不到備球時會記在這裡。")
    lab = QLabel(head)
    lab.setStyleSheet("font-weight: bold;")
    v.addWidget(lab)

    tbl = QTableWidget(len(rows), 5)
    tbl.setHorizontalHeaderLabels(
        ["時間", "商城編號", "商品", "數量", "花費(點數)"])
    tbl.setEditTriggers(QTableWidget.NoEditTriggers)
    tbl.verticalHeader().setVisible(False)
    for i, (ts, mid, tid, qty, cost) in enumerate(rows):
        cells = (time.strftime("%m/%d %H:%M:%S", time.localtime(ts)),
                 str(mid), itemname.label(tid), str(qty), f"{cost:,}")
        for col, text in enumerate(cells):
            it = QTableWidgetItem(text)
            if col in (1, 3, 4):                # 編號／數量／花費靠右
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            tbl.setItem(i, col, it)
    # 欄寬手動給、最後一欄補滿 —— 不用 ResizeToContents（qt-ui-pitfalls）。
    for col, w in enumerate((115, 80, 200, 55)):
        tbl.setColumnWidth(col, w)
    tbl.horizontalHeader().setStretchLastSection(True)
    v.addWidget(tbl, 1)
    panel._head, panel._tbl = lab, tbl          # 給離線測試摸得到
    return panel


def record_mall_buy(rows: list, g) -> None:
    """商城買到一筆 → 記進 `rows`。**背景執行緒呼叫**：只碰純資料，不碰 Qt。

    單價不必猜 —— `mall.Goods.price` 就是遊戲商品表裡的售價。
    """
    rows.append((time.time(), int(g.mall_id), int(g.type_id),
                 int(g.count), int(g.price)))
    if len(rows) > MALL_LOG_CAP:
        del rows[:-MALL_LOG_CAP]
