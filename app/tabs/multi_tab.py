"""分身總控：對所有勾選的分身**同時**下同一個指令。

目前只有一個動作 —— 一次把勾起來的角色全部換到同一個頻道。

畫面
----
    目標頻道 [3 ▾] [換頻] [停止] [重新整理]
    ┌────┬────────┬──────────────┬────────┬──────┬──────────┬────────┐
    │全選│ 角色名 │ 帳號         │ 伺服器 │ 頻道 │ 目前地圖 │ 狀態   │
    └────┴────────┴──────────────┴────────┴──────┴──────────┴────────┘

每個欄位的來源（全部是讀出來的，沒有寫死的遊戲資料）
----------------------------------------------------
    分身清單    preload.windows()
    角色名      preload.name_of()（開機預讀的快取）
    帳號        視窗標題
    伺服器/頻道 視窗標題（channel.server_name / channel.current）
                ⚠ 記憶體裡的 SYSTEM_CUR_CHANNEL 五台實測全是 1，不能當即時值
    目前地圖    scene.current(allow_scan=False)，靜態指標 1.5ms，位址走 AOB 自動定位
    分流數      login.servers()：遊戲自己解析進記憶體的伺服器陣列
                ⚠ 不用 channel.count()，那是全記憶體掃描（0.3~1 秒）會卡住介面
    換頻        channel.switch() → 遊戲自己的送包函式 0x5D3D97(0x47, 頻道-1)

「一次全換」是怎麼做到的
------------------------
按下換頻之後分兩段：

  1. **準備**：一拍裝一台跳板（`move.acquire`，第一次要組譯，0.3~1 秒）。
     一拍一台是為了不讓好幾台的組譯時間疊在一起把介面凍住。
  2. **發射**：跳板都備妥之後，在**同一拍**裡把所有分身的換頻包依序送出。
     每包只等遊戲一幀（`attack.CALL_TIMEOUT` 0.12 秒上限，正常約 16ms），
     所以五台之間差不到 0.1 秒 —— 對玩家來說就是一起換。

送完立刻 `preload.forget_state()`：換頻＝斷線重連，狀態物件會搬家，
快取位址一定要作廢（不作廢就是拿舊位址去寫別人的記憶體）。

失效模式
--------
  · 讀不到分流數 → 下拉留空、換頻鈕停用（**不猜** FALLBACK_MAX 去送）
  · 那台伺服器沒有第 N 頻 → 該列標 ⚠ 跳過，其他台照換
  · 還沒進遊戲（標題沒有頻道）→ 該列標「未進遊戲」跳過
  · 排不進指令槽／換頻逾時 → **自動重試不設上限**，「停止」鈕是唯一出口
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app import theme
from app.config import config
from app.core import charname, injector, preload
from app.core.memory import MemoryScanner
from app.game import channel, locate, login, move, scene
from app.tabs.base_tab import BaseTab

COLS = ("全選", "角色名", "帳號", "伺服器", "頻道", "目前地圖", "狀態")
(COL_PICK, COL_NAME, COL_ACCT, COL_SRV, COL_CHAN, COL_MAP, COL_STATE) = range(7)

# 使用者要求「快速更新、但穩定最重要」。一拍要做的事全部是純讀：
# 列視窗（EnumWindows）＋每台讀兩次視窗標題＋讀一次場景（靜態指標）。
# ★ 五台實測 **0.36 ms／拍**（其中列視窗 0.27 ms），300ms 一拍 ≈ 0.1% 的一顆核心。
#   所以更新頻率不是瓶頸，真正要防的是「整批重畫表格」（見 _rebuild／_set）。
# ★ 分頁沒在看、又沒有換頻任務在跑時整拍跳過（見 _tick）。
REFRESH_MS = 300
# 送出換頻到視窗標題真的變成新頻道的等待上限。實測換好約 1 秒，
# 給到 15 秒是留給連線不好的情況；超過就重送（不設上限，停止鈕是出口）。
SWITCH_TIMEOUT = 15.0
# 排不進指令槽時多久重試一次。指令槽只有一個，掛機分頁也在用，
# 太密集只是互相搶。
RETRY_GAP = 0.5
ROWS_SHOWN = 5              # 天使之戀最多開 5 台，表格就給看得到 5 列的高度
# 分流數讀不到時，隔多久再試一次（遊戲還在開、還沒解析完伺服器清單）。
SRV_RETRY = 2.0


def _acct(title: str) -> str:
    """視窗標題裡的帳號；**還沒登入的視窗回空字串**。

    ⚠ 不能無條件叫 `charname.account_from_title()` —— 登入畫面的標題是
      「Angels Online Global」，沒有「 - 帳號(伺服器-頻道)」那一段，
      那支會把整個標題當成帳號吐回來（欄位就變成一串沒意義的字）。
    """
    return charname.account_from_title(title) if " - " in title else ""


class MultiTab(BaseTab):
    TAB_TITLE = "分身總控"
    ORDER = 12                       # 排在自動登入（10）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._pids: list[int] = []           # 目前表格的列順序（依 pid 排序）
        self._named: set[int] = set()         # 已經（排程）解析過角色名的 pid
        self._srv_cache: list[tuple[str, int]] = []
        self._picked: set[str] = set(
            str(a) for a in (config.get("multi.picked", []) or []))
        self._state_txt: dict[int, str] = {}  # pid -> 狀態欄文字
        self._job: dict | None = None
        self._loading = False                 # 重建表格時擋掉 itemChanged
        self._srv_try = 0.0                   # 上次嘗試讀分流數的時間

        root = QVBoxLayout(self)

        hint = QLabel(
            "勾起要一起行動的角色，選好目標頻道按「換頻」，"
            "所有勾選的分身會在同一時間換到那一頻（送的是遊戲自己的換頻封包，"
            "斷線重連由客戶端自己處理，約 1 秒完成）。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("目標頻道"))
        self.chan = QComboBox()
        self.chan.setToolTip(
            "這個伺服器實際有幾個分流，是從遊戲載進記憶體的伺服器清單讀出來的，\n"
            "官方增減分流會自動跟上。讀不到時這裡會是空的，換頻也會停用 ——\n"
            "寧可不換，也不拿猜的編號送給伺服器。")
        bar.addWidget(self.chan)
        self.go_btn = QPushButton("換頻")
        self.go_btn.setProperty("primary", True)
        self.go_btn.setToolTip(
            "把所有勾選的分身一次換到上面選的頻道。\n"
            "  · 已經在那一頻的會跳過（免得白白斷線重連一次）\n"
            "  · 還沒進遊戲、或那台伺服器沒有這一頻的會跳過並標示原因\n"
            "  · 排不進指令槽或換頻逾時會自動重試，要停請按「停止」")
        self.go_btn.clicked.connect(self._do_switch)
        bar.addWidget(self.go_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setToolTip("停掉正在進行的換頻與重試。已經送出去的那幾包不會收回。")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self._stop("已停止"))
        bar.addWidget(self.stop_btn)
        reload_btn = QPushButton("重新整理")
        reload_btn.setToolTip(
            "重新找一次分身，並重讀角色名。\n"
            "同一台登出換角色時 pid 不會變，要按這顆才會更新成新角色的名字。")
        reload_btn.clicked.connect(self._hard_refresh)
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        # ⚠⚠ **不要用 ResizeToContents**：這張表每 300ms 就會更新一次儲存格，
        #   而 ResizeToContents 的表頭在每一次 setText 都會把整欄重量一遍
        #   （sell_tab 的 _refill 就是在講這個坑）。欄寬改用字型量一次算好，
        #   之後更新內容是 O(1)。
        hh.setSectionResizeMode(QHeaderView.Fixed)
        hh.setSectionResizeMode(COL_STATE, QHeaderView.Stretch)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._on_header_clicked)
        fm = self.table.fontMetrics()
        pad = 24
        for col, sample in ((COL_PICK, "全選"),
                            (COL_NAME, "十二個字的角色名字"),
                            (COL_ACCT, "fred26016041"),
                            (COL_SRV, "邱比特(NEW)"),
                            (COL_CHAN, "頻道"),
                            (COL_MAP, "史萊姆晴空牧場")):
            self.table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)
        self.table.horizontalHeaderItem(COL_PICK).setToolTip(
            "點一下「全選」這格就全部勾起來，再點一下全部取消。")
        vh = self.table.verticalHeader()
        self.table.setMinimumHeight(
            vh.defaultSectionSize() * (ROWS_SHOWN + 1) + 8)
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        self.status = QLabel("找分身中…")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(REFRESH_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        # force=True：切過來的當下 isVisible() 不保證已經是 True，
        # 被下面那道「看不見就跳過」擋掉的話，表格會一直是空的。
        self._tick(force=True)

    def on_close(self) -> None:
        self._timer.stop()
        self._job = None
        for pid in list(self._movers):
            self._drop_mover(pid)
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()

    # ------------------------------------------------------------------
    # 每一拍
    # ------------------------------------------------------------------
    def _tick(self, force: bool = False) -> None:
        # 沒在看這一頁、又沒有換頻任務在跑 → 整拍不做事（省 CPU）。
        # ⚠ 有任務時一定要照跑：使用者可能切去別的分頁等它換完。
        if not force and not self.isVisible() and self._job is None:
            return
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        pids = [w.pid for w in wins]
        if pids != self._pids:
            self._rebuild(wins)
        # 分流數第一次沒讀到（遊戲剛開、清單還沒解析完）就隔一下再試 ——
        # 不然要等到分身數量變動才會再問一次，換頻鈕會一直是灰的。
        elif self.chan.count() == 0 and wins:
            now = time.monotonic()
            if now - self._srv_try >= SRV_RETRY:
                self._srv_try = now
                self._fill_channels(wins)
        self._update_rows(wins)
        self._job_tick(wins)

    def _rebuild(self, wins) -> None:
        """分身多了／少了才重建表格（勾選狀態照帳號留著）。"""
        alive = {w.pid for w in wins}
        for pid in list(self._scanners):
            if pid not in alive:
                self._scanners.pop(pid).close()
                self._drop_mover(pid)
                self._state_txt.pop(pid, None)
                self._named.discard(pid)
        self._loading = True
        try:
            self.table.setRowCount(len(wins))
            for r, w in enumerate(wins):
                if w.pid not in self._scanners:
                    sc = MemoryScanner()
                    try:
                        sc.open(w.pid)
                        # 改版位移自動校正。同一份映像全域只會真的掃一次，
                        # 所以每台都叫是安全的。
                        locate.warm(sc)
                    except Exception:                       # noqa: BLE001
                        sc.close()
                        sc = None
                    if sc is not None:
                        self._scanners[w.pid] = sc
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                acc = _acct(w.title)
                chk.setCheckState(
                    Qt.Checked if acc in self._picked else Qt.Unchecked)
                self.table.setItem(r, COL_PICK, chk)
                for c in (COL_NAME, COL_ACCT, COL_SRV, COL_CHAN,
                          COL_MAP, COL_STATE):
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.ItemIsEnabled)
                    if c in (COL_CHAN, COL_SRV):
                        it.setTextAlignment(Qt.AlignCenter)
                    self.table.setItem(r, c, it)
        finally:
            self._loading = False
        self._pids = [w.pid for w in wins]
        self._srv_try = time.monotonic()
        self._fill_channels(wins)

    def _update_rows(self, wins) -> None:
        for r, w in enumerate(wins):
            pid = w.pid
            acc = _acct(w.title)
            self._set(r, COL_NAME,
                      preload.name_of(pid, account=acc) or "（還沒登入）")
            self._set(r, COL_ACCT, acc or "—")
            self._set(r, COL_SRV, channel.server_name(w.hwnd) or "—")
            cur = channel.current(w.hwnd)
            self._set(r, COL_CHAN, str(cur) if cur else "—")
            self._set(r, COL_MAP, self._map_of(pid))
            txt = self._state_txt.get(pid, "")
            colour = None
            if txt.startswith("✅"):
                colour = QColor(theme.OK)
            elif txt.startswith("⚠"):
                colour = QColor(theme.BAD)
            elif txt:
                colour = QColor(theme.MUTED)
            self._set(r, COL_STATE, txt, colour)
            # 角色名還沒解析過的分身（程式開起來之後才登入的那台）：
            # 排一次性的解析。★ 用 singleShot 而不是當場做 —— 沒有快取時
            # 它要掃一次記憶體（約 0.7 秒），當場做會讓這一拍看起來像卡住。
            # ⚠ 還在登入畫面的（沒有帳號）先不掃，等它進遊戲再說。
            if acc and pid not in self._named:
                self._named.add(pid)
                QTimer.singleShot(0, lambda p=pid, a=acc: self._resolve_name(p, a))
        if self._job is not None:
            return                              # 狀態列由 _job_tick 負責
        if not wins:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        elif self.chan.count() == 0:
            # 把「標題上寫什麼」跟「記憶體讀到什麼」一起寫出來 ——
            # 停用時最怕的是使用者只看到一句「讀不到」卻無從判斷哪裡對不上。
            here = "、".join(sorted({channel.server_name(w.hwnd) or "?"
                                     for w in wins}))
            names = "、".join(nm for nm, _ in self._server_list()) or "一個都沒有"
            self.status.setText(
                "⚠ 讀不到伺服器的分流數，換頻先停用 —— 寧可不換，也不拿猜的"
                f"頻道編號送給伺服器。（視窗標題上的伺服器：{here}；"
                f"記憶體讀到的伺服器清單：{names}）")
        else:
            self.status.setText(
                f"找到 {len(wins)} 個分身，已勾 {len(self._checked_pids())} 個")

    def _set(self, row: int, col: int, text: str,
             colour: QColor | None = None) -> None:
        """只有內容真的變了才寫進去（每 300ms 一拍，不要無謂重繪）。"""
        it = self.table.item(row, col)
        if it is None:
            return
        if it.text() != text:
            self._loading = True
            try:
                it.setText(text)
            finally:
                self._loading = False
        if colour is not None and it.foreground().color() != colour:
            it.setForeground(colour)

    def _map_of(self, pid: int) -> str:
        sc = self._scanners.get(pid)
        if sc is None:
            return "—"
        try:
            # allow_scan=False：只走靜態指標（1.5ms）。這裡每 300ms 會跑到，
            # 絕不能開那條 0.3 秒的全掃備援。
            got = scene.current(sc, allow_scan=False)
        except Exception:                                   # noqa: BLE001
            return "—"
        return got.name if got else "—"

    def _resolve_name(self, pid: int, account: str) -> None:
        """把某一台的角色名解出來並記進 preload 的快取（一個 pid 只做一次）。"""
        sc = self._scanners.get(pid)
        if sc is None:
            return
        try:
            preload.name_of(pid, sc, account)
        except Exception:                                   # noqa: BLE001
            pass

    def _hard_refresh(self) -> None:
        """「重新整理」：分身重找、角色名重掃（登出換角色時 pid 不變）。"""
        self._stop("")
        for pid in list(self._movers):
            self._drop_mover(pid)
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._pids = []
        self._named.clear()
        self._state_txt.clear()
        self._srv_cache = []
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        self._rebuild(wins)
        for w in wins:
            sc = self._scanners.get(w.pid)
            acc = _acct(w.title)
            if sc is not None and acc:
                # ★ force=True：同一台登出換角色時 pid 不變，不強制重掃
                #   永遠是舊角色名。一台約 0.7 秒，所以只在按鈕上做。
                preload.name_of(w.pid, sc, acc, force=True)
                self._named.add(w.pid)
        self._update_rows(wins)

    # ------------------------------------------------------------------
    # 勾選
    # ------------------------------------------------------------------
    def _on_item_changed(self, item) -> None:
        if self._loading or item.column() != COL_PICK:
            return
        acc = self.table.item(item.row(), COL_ACCT)
        acc = acc.text() if acc is not None else ""
        if not acc:
            return
        if item.checkState() == Qt.Checked:
            self._picked.add(acc)
        else:
            self._picked.discard(acc)
        config.set("multi.picked", sorted(self._picked))
        config.save()

    def _on_header_clicked(self, col: int) -> None:
        """點「全選」那一格＝全部勾起來，再點一次全部取消。"""
        if col != COL_PICK:
            return
        rows = self.table.rowCount()
        want = Qt.Unchecked if all(
            self.table.item(r, COL_PICK) is not None
            and self.table.item(r, COL_PICK).checkState() == Qt.Checked
            for r in range(rows)) and rows else Qt.Checked
        for r in range(rows):
            it = self.table.item(r, COL_PICK)
            if it is not None:
                it.setCheckState(want)          # 逐列觸發 _on_item_changed

    def _checked_pids(self) -> list[int]:
        out = []
        for r, pid in enumerate(self._pids):
            it = self.table.item(r, COL_PICK)
            if it is not None and it.checkState() == Qt.Checked:
                out.append(pid)
        return out

    def _win_of(self, pid: int, wins):
        return next((w for w in wins if w.pid == pid), None)

    # ------------------------------------------------------------------
    # 分流數
    # ------------------------------------------------------------------
    def _game_dir(self) -> str:
        exe = str(config.get("login.exe_path", "") or "")
        return os.path.dirname(exe) if exe else ""

    def _server_list(self) -> list[tuple[str, int]]:
        """伺服器 [(名稱, 分流數), …]，**讀出來的**不是寫死的。

        遊戲載進記憶體的伺服器陣列優先（指標路徑，毫秒級）；讀不到就退回
        遊戲資料夾的 server.xml。一個 session 內不會變，讀到就記著。
        """
        if self._srv_cache:
            return self._srv_cache
        sc = next(iter(self._scanners.values()), None)
        try:
            got = login.servers(sc, self._game_dir())
        except Exception:                                   # noqa: BLE001
            got = []
        if got:
            self._srv_cache = got
        return self._srv_cache

    def _subsets_of(self, w) -> int | None:
        """這台分身所在伺服器有幾個分流；查不到回 None（＝不准送）。

        兩條路，**都要求跟視窗標題的伺服器名一致**才採用 —— 標題才是「這個
        角色現在真的在哪個伺服器」的依據，比對不上就寧可不換：
          1. `login.servers()` 的伺服器陣列，用名字找那一筆
          2. `login.server_info()`（客戶端自己記的「選中的伺服器」）
        """
        if w is None:
            return None
        name = channel.server_name(w.hwnd)
        if not name:
            return None
        got = next((n for nm, n in self._server_list() if nm == name), None)
        if got:
            return got
        sc = self._scanners.get(w.pid)
        if sc is None:
            return None
        try:
            info = login.server_info(sc)
        except Exception:                                   # noqa: BLE001
            return None
        return info[1] if info and info[0] == name else None

    def _fill_channels(self, wins) -> None:
        """把頻道下拉重填成「目前這些分身的伺服器最多有幾頻」。

        多開跨伺服器時取最大值 —— 送出前每一台還會各自用**自己**伺服器的
        分流數驗一次（見 _do_switch），沒有那一頻的那台會被擋下並標示。
        """
        counts = [n for n in (self._subsets_of(w) for w in wins) if n]
        top = max(counts) if counts else 0
        if top == self.chan.count():
            return                                # 沒變就不要動（會清掉選擇）
        keep = self.chan.currentText()
        self.chan.blockSignals(True)
        self.chan.clear()
        self.chan.addItems([str(c) for c in range(1, top + 1)])
        i = self.chan.findText(keep)
        if i >= 0:
            self.chan.setCurrentIndex(i)
        self.chan.blockSignals(False)
        self.go_btn.setEnabled(top > 0 and self._job is None)

    # ------------------------------------------------------------------
    # 換頻
    # ------------------------------------------------------------------
    def _mover(self, pid: int):
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，**不要自己 new 一個 Mover** ——
          同一個遊戲行程只能有一份跳板，自己裝會把掛機分頁那份拆掉。
        """
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        path = injector.process_path(pid)
        if not path:
            return None
        try:
            mv = move.acquire(pid, path, self)
        except Exception:                                   # noqa: BLE001
            self._movers.pop(pid, None)
            return None
        self._movers[pid] = mv
        return mv

    def _drop_mover(self, pid: int) -> None:
        if self._movers.pop(pid, None) is not None:
            try:
                move.release(pid, self)
            except Exception:                               # noqa: BLE001
                pass

    def _do_switch(self) -> None:
        if self._job is not None:
            return
        target = self.chan.currentText()
        if not target.isdigit():
            self.status.setText("⚠ 還沒讀到分流數，不能換頻。")
            return
        target = int(target)
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        picked = self._checked_pids()
        if not picked:
            self.status.setText("還沒勾任何分身。")
            return
        self._state_txt.clear()
        todo: list[int] = []
        for pid in picked:
            w = self._win_of(pid, wins)
            cur = channel.current(w.hwnd) if w else None
            subs = self._subsets_of(w)
            if cur is None:
                self._state_txt[pid] = "未進遊戲，跳過"
            elif not subs:
                self._state_txt[pid] = "⚠ 讀不到分流數，跳過"
            elif target > subs:
                self._state_txt[pid] = f"⚠ 這個伺服器只有 {subs} 頻，跳過"
            elif cur == target:
                self._state_txt[pid] = f"✅ 已經在 {target} 頻"
            else:
                todo.append(pid)
                self._state_txt[pid] = "準備中…"
        self._update_rows(wins)
        if not todo:
            self.status.setText(f"沒有需要換頻的分身（目標 {target} 頻）。")
            return
        self._job = {"target": target, "prep": list(todo), "pend": [],
                     "wait": {}, "done": 0, "next_try": 0.0}
        self.go_btn.setEnabled(False)
        self.chan.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText(f"準備中 0/{len(todo)}（正在裝跳板）…")
        QTimer.singleShot(0, self._prep_tick)

    def _prep_tick(self) -> None:
        """一拍裝一台跳板 —— 疊在一起做會把介面凍住（第一次要組譯）。"""
        job = self._job
        if job is None:
            return
        if job["prep"]:
            pid = job["prep"].pop(0)
            if self._mover(pid) is not None:
                job["pend"].append(pid)
                self._state_txt[pid] = "準備好了"
            else:
                self._state_txt[pid] = "⚠ 裝不上跳板，跳過"
            total = len(job["prep"]) + len(job["pend"])
            self.status.setText(
                f"準備中 {len(job['pend'])}/{total}（正在裝跳板）…")
            QTimer.singleShot(0, self._prep_tick)
            return
        # 跳板都備妥 → 立刻進發射階段（不等下一拍，讓五台盡量同時）
        self._job_tick(sorted(preload.windows(), key=lambda w: w.pid))

    def _job_tick(self, wins) -> None:
        job = self._job
        if job is None:
            return
        if job["prep"]:                       # 還在裝跳板，這裡不動作
            return
        now = time.monotonic()
        target = job["target"]

        # --- 中途被關掉的分身要踢出任務 ------------------------------
        # 不踢的話 hwnd 查不到 → 每一拍都送不出去 → 卡在無止境的重試。
        alive = {w.pid for w in wins}
        for pid in [p for p in job["pend"] if p not in alive]:
            job["pend"].remove(pid)
            self._state_txt[pid] = "⚠ 分身已關閉"
        for pid in [p for p in job["wait"] if p not in alive]:
            del job["wait"][pid]
            self._state_txt[pid] = "⚠ 分身已關閉"

        # --- 發射：同一拍把所有備妥的分身一起送出去 -------------------
        if job["pend"] and now >= job["next_try"]:
            again: list[int] = []
            for pid in job["pend"]:
                # ⚠ 送出前當場重讀重驗 —— 不能信按下按鈕那一拍讀到的
                #   （中間可能換了伺服器、視窗被關掉）。
                subs = self._subsets_of(self._win_of(pid, wins))
                mv = self._movers.get(pid)
                ok = bool(subs) and mv is not None and mv.active and \
                    channel.switch(mv, target, subs)
                if ok:
                    # ⚠⚠ 換頻＝斷線重連，狀態物件／玩家物件／背包全部搬家。
                    #   快取位址一定要當場作廢，不然別的分頁會拿舊位址去寫。
                    preload.forget_state(pid)
                    job["wait"][pid] = now
                    self._state_txt[pid] = f"換頻中… → {target} 頻"
                else:
                    again.append(pid)
                    self._state_txt[pid] = "指令槽忙，重試中…"
            job["pend"] = again
            if again:
                job["next_try"] = now + RETRY_GAP

        # --- 確認：視窗標題變成目標頻道才算成功 -----------------------
        for pid in list(job["wait"]):
            w = self._win_of(pid, wins)
            cur = channel.current(w.hwnd) if w else None
            if cur == target:
                del job["wait"][pid]
                job["done"] += 1
                self._state_txt[pid] = f"✅ 已在 {target} 頻"
            elif now - job["wait"][pid] > SWITCH_TIMEOUT:
                # 逾時 → 重送。使用者要求：暫時性失敗自動重試不設上限，
                # 「停止」鈕是唯一出口。
                del job["wait"][pid]
                job["pend"].append(pid)
                job["next_try"] = now + RETRY_GAP
                self._state_txt[pid] = "⚠ 換頻逾時，重送中…"

        left = len(job["pend"]) + len(job["wait"])
        if left:
            self.status.setText(
                f"換頻中：完成 {job['done']}、還有 {left} 台（目標 {target} 頻）"
                "　—— 要停請按「停止」")
        else:
            done = job["done"]
            self._job = None
            self.go_btn.setEnabled(True)
            self.chan.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status.setText(f"換頻完成：{done} 台已經在 {target} 頻。")

    def _stop(self, why: str) -> None:
        if self._job is None:
            return
        for pid in list(self._job["prep"]) + list(self._job["pend"]) + \
                list(self._job["wait"]):
            self._state_txt[pid] = "已停止"
        self._job = None
        self.go_btn.setEnabled(self.chan.count() > 0)
        self.chan.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if why:
            self.status.setText(why + "（已經送出去的那幾包不會收回）")
