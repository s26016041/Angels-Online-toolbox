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
from app.game import (channel, jumpmap, locate, login, move, robot, scene,
                      team)
from app.tabs.base_tab import BaseTab

COLS = ("全選", "角色名", "帳號", "伺服器", "頻道", "目前地圖", "隊伍", "狀態")
(COL_PICK, COL_NAME, COL_ACCT, COL_SRV, COL_CHAN, COL_MAP, COL_TEAM,
 COL_STATE) = range(8)

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
# --- 自動組隊的節奏 ---------------------------------------------------------
# 邀請隔多久補送一次（等對方的用戶端收到、跳出邀請視窗）。
INVITE_GAP = 2.0
# 同意隔多久補送一次。比邀請密是因為它便宜、而且要搶在邀請視窗還開著的時候。
JOIN_GAP = 0.5
# 退組之後等隊員陣列真的清空。清不掉就一直重送（停止鈕是出口）。
LEAVE_GAP = 1.0
# 精靈開關寫下去到讀得回來的緩衝（純記憶體寫，其實是立即的，留一拍保險）。
ROBOT_SETTLE = 0.3


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

        # --- 自動組隊 ---------------------------------------------------
        bar2 = QHBoxLayout()
        self.team_btn = QPushButton("自動組隊")
        self.team_btn.setToolTip(
            "把勾選的角色組成一隊，順序是：\n"
            "  ① 全部關掉天使守護精靈總開關\n"
            "  ② 已經有隊伍的通通退組（等隊員名單真的清空）\n"
            "  ③ 第一個勾選的當隊長，一個一個邀請其他人\n"
            "  ④ 被邀請的送出同意，等隊長的隊員名單真的出現他才換下一個\n"
            "  ⑤ 全部重新打開天使守護精靈總開關\n"
            "\n"
            "⚠ 勾選的分身**必須在同一頻**，不然邀請送不到 ——\n"
            "　 不同頻會直接擋下來並告訴你，請先用上面的「換頻」弄到同一頻。\n"
            "⚠ 已經跟別人（不是這裡勾的角色）組隊的，也會被退組。")
        self.team_btn.clicked.connect(self._do_team)
        bar2.addWidget(self.team_btn)
        bar2.addWidget(QLabel("分配方式"))
        self.share = QComboBox()
        self.share.addItem("均分制", team.SHARE_EVEN)
        self.share.addItem("獨享制", team.SHARE_SOLO)
        self.share.setToolTip(
            "送出邀請時帶的分配方式，跟你在遊戲裡選的是同一個欄位。\n"
            "均分制＝隊伍均分，獨享制＝各自撿各自的。")
        i = self.share.findData(int(config.get("multi.share", team.SHARE_EVEN)))
        self.share.setCurrentIndex(max(i, 0))
        self.share.currentIndexChanged.connect(self._save_share)
        bar2.addWidget(self.share)
        bar2.addStretch(1)
        root.addLayout(bar2)

        # --- 天使趴趴GO ---------------------------------------------------
        bar3 = QHBoxLayout()
        bar3.addWidget(QLabel("趴趴GO"))
        self.jclass = QComboBox()
        self.jclass.setToolTip(
            "傳送點的分類，跟遊戲趴趴GO視窗左邊那排是同一份資料。\n"
            "⚠ 一個傳送點可能同時掛在好幾個分類底下（遊戲本來就這樣分），\n"
            "　 所以在不同分類看到同一個地點是正常的。")
        self.jclass.currentIndexChanged.connect(self._fill_jumps)
        bar3.addWidget(self.jclass)
        self.jump = QComboBox()
        self.jump.setToolTip(
            "要傳送到哪裡。名稱與落點座標是從遊戲資源包抽出來的，\n"
            "送出的封包跟你在趴趴GO視窗點下去完全一樣。")
        # ⚠ 主視窗是**固定 940 寬**的。下拉的預設策略會照「最長的那一筆」
        #   決定寬度 —— 120 筆地圖名裡只要有一個特別長，整排就被撐開跑版
        #   （見 memory 的 qt-ui-pitfalls）。這裡把控制項本身的寬度釘在
        #   固定字數，另外把**展開的清單**放寬，兩邊都顧到。
        # ⚠ Qt6 只剩 …WithIcon 這個列舉，Qt5 的 AdjustToMinimumContentsLength
        #   已經被拿掉（用了會 AttributeError → 分頁建不起來）。
        self.jump.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.jump.setMinimumContentsLength(26)
        self.jump.view().setMinimumWidth(
            self.jump.fontMetrics().horizontalAdvance(
                "喧嘩雨林(LV105~120)重生點（123,456）") + 40)
        self.jump.currentIndexChanged.connect(self._save_jump)
        bar3.addWidget(self.jump, 1)
        self.jump_btn = QPushButton("傳送")
        self.jump_btn.setToolTip(
            "把所有勾選的分身傳送到選定的地點。\n"
            "  · 送出的是遊戲自己的傳送封包，不會去碰趴趴GO那個視窗\n"
            "  · 傳送完會等**目前地圖真的變成目的地**才算成功\n"
            "  · 排不進指令槽或逾時會自動重試，要停請按「停止」\n"
            "⚠ 到不到得看伺服器（等級不足、任務未完成之類會被拒絕）。")
        self.jump_btn.clicked.connect(self._do_jump)
        bar3.addWidget(self.jump_btn)
        root.addLayout(bar3)
        self._fill_classes()

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
                            (COL_MAP, "史萊姆晴空牧場"),
                            (COL_TEAM, "隱狐、天狐、北極狐")):
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
                          COL_MAP, COL_TEAM, COL_STATE):
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
            self._set(r, COL_TEAM, self._team_of(pid))
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

    def _team_of(self, pid: int) -> str:
        """隊友名單（不含自己）。⚠ 讀不到與沒隊友是兩件事，顯示上要分得開。"""
        sc = self._scanners.get(pid)
        if sc is None:
            return "—"
        try:
            got = team.members(sc)
        except Exception:                                   # noqa: BLE001
            return "—"
        if got is None:
            return "讀不到"
        return "、".join(m.name for m in got) if got else "沒隊伍"

    def _save_share(self) -> None:
        config.set("multi.share", int(self.share.currentData()))
        config.save()

    # ------------------------------------------------------------------
    # 趴趴GO 清單
    # ------------------------------------------------------------------
    def _fill_classes(self) -> None:
        self.jclass.blockSignals(True)
        self.jclass.addItem("全部", None)
        names = jumpmap.classes()
        for cid in sorted(names) or []:
            self.jclass.addItem(names.get(cid) or f"分類 {cid}", cid)
        want = config.get("multi.jump_class", None)
        i = self.jclass.findData(want) if want is not None else 0
        self.jclass.setCurrentIndex(max(i, 0))
        self.jclass.blockSignals(False)
        self._fill_jumps()
        if not jumpmap.entries():
            self.jump_btn.setEnabled(False)
            self.jump.addItem("⚠ 讀不到傳送表（assets/jumpmap.tsv）", None)

    def _fill_jumps(self) -> None:
        """依分類重填地點清單，選擇盡量留住（使用者常常在分類間來回看）。"""
        keep = self.jump.currentData()
        self.jump.blockSignals(True)
        self.jump.clear()
        cid = self.jclass.currentData()
        for e in jumpmap.by_class(cid):
            self.jump.addItem(f"{e.name}（{e.x},{e.y}）", e.jump_id)
        i = self.jump.findData(keep)
        if i < 0:
            i = self.jump.findData(config.get("multi.jump_id", None))
        self.jump.setCurrentIndex(max(i, 0))
        self.jump.blockSignals(False)
        config.set("multi.jump_class", cid)
        config.save()

    def _save_jump(self) -> None:
        jid = self.jump.currentData()
        if jid is not None:
            config.set("multi.jump_id", int(jid))
            config.save()

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
        self._job = {"kind": "chan", "target": target, "prep": list(todo),
                     "pend": [], "wait": {}, "done": 0, "next_try": 0.0}
        self._busy_ui(True)
        self.status.setText(f"準備中 0/{len(todo)}（正在裝跳板）…")
        QTimer.singleShot(0, self._prep_tick)

    def _busy_ui(self, busy: bool) -> None:
        """任務進行中把會打架的控制項關掉，只留「停止」。"""
        self.go_btn.setEnabled(not busy and self.chan.count() > 0)
        self.team_btn.setEnabled(not busy)
        self.jump_btn.setEnabled(not busy and bool(jumpmap.entries()))
        self.chan.setEnabled(not busy)
        self.share.setEnabled(not busy)
        self.jclass.setEnabled(not busy)
        self.jump.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

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
        if job is None or job["prep"]:        # 還在裝跳板，這裡不動作
            return
        if job["kind"] == "team":
            self._team_tick(job, wins)
        elif job["kind"] == "jump":
            self._jump_tick(job, wins)
        else:
            self._chan_tick(job, wins)

    def _chan_tick(self, job, wins) -> None:
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
            self._busy_ui(False)
            self.status.setText(f"換頻完成：{done} 台已經在 {target} 頻。")

    # ------------------------------------------------------------------
    # 天使趴趴GO
    # ------------------------------------------------------------------
    def _do_jump(self) -> None:
        if self._job is not None:
            return
        jid = self.jump.currentData()
        e = jumpmap.get(int(jid)) if jid is not None else None
        if e is None:
            self.status.setText("⚠ 沒有選到傳送點（傳送表讀不到？）。")
            return
        picked = self._checked_pids()
        if not picked:
            self.status.setText("還沒勾任何分身。")
            return
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        self._state_txt.clear()
        todo: list[int] = []
        for pid in picked:
            w = self._win_of(pid, wins)
            if w is None or channel.current(w.hwnd) is None:
                self._state_txt[pid] = "未進遊戲，跳過"
                continue
            todo.append(pid)
            self._state_txt[pid] = "準備中…"
        self._update_rows(wins)
        if not todo:
            self.status.setText("沒有可以傳送的分身。")
            return
        self._job = {"kind": "jump", "jump_id": e.jump_id, "scene": e.scene_id,
                     "name": e.name, "prep": list(todo), "pend": [],
                     "wait": {}, "here": set(), "done": 0, "next_try": 0.0}
        self._busy_ui(True)
        self.status.setText(f"準備中 0/{len(todo)}（正在裝跳板）…")
        QTimer.singleShot(0, self._prep_tick)

    def _jump_tick(self, job, wins) -> None:
        now = time.monotonic()
        alive = {w.pid for w in wins}
        want = job["scene"]

        for pid in [p for p in job["pend"] if p not in alive]:
            job["pend"].remove(pid)
            self._state_txt[pid] = "⚠ 分身已關閉"
        for pid in [p for p in job["wait"] if p not in alive]:
            del job["wait"][pid]
            self._state_txt[pid] = "⚠ 分身已關閉"

        # --- 發射：同一拍全部送出 -------------------------------------
        if job["pend"] and now >= job["next_try"]:
            again: list[int] = []
            for pid in job["pend"]:
                sc = self._scanners.get(pid)
                mv = self._movers.get(pid)
                if sc is None or mv is None:
                    again.append(pid)
                    continue
                # 送之前先記下「本來就在這張圖」的 —— 那種情況地圖不會變，
                # 沒辦法用地圖當驗收訊號（見下面的說明）。
                cur = scene.current_id(sc, allow_scan=False)
                already = scene.same_map(cur, want)
                ok, why = jumpmap.teleport(mv, sc, job["jump_id"])
                if ok:
                    # ⚠ 換地圖＝物件搬家，快取位址一定要作廢。
                    preload.forget_state(pid)
                    if already:
                        job["here"].add(pid)
                    job["wait"][pid] = now
                    self._state_txt[pid] = "傳送中…"
                else:
                    again.append(pid)
                    self._state_txt[pid] = f"⚠ {why}，重試中…"
            job["pend"] = again
            if again:
                job["next_try"] = now + RETRY_GAP

        # --- 確認：目前地圖真的變成目的地 -----------------------------
        for pid in list(job["wait"]):
            sc = self._scanners.get(pid)
            cur = scene.current_id(sc, allow_scan=False) if sc else None
            if scene.same_map(cur, want):
                # ⚠ 本來就在這張圖的沒辦法用「地圖變了」驗收 —— 老實講出來，
                #   不要拿一個必然成立的條件冒充「已確認到達」。
                if pid in job["here"]:
                    self._state_txt[pid] = "✅ 已送出（本來就在這張地圖）"
                else:
                    self._state_txt[pid] = f"✅ 已到 {job['name']}"
                del job["wait"][pid]
                job["done"] += 1
            elif now - job["wait"][pid] > SWITCH_TIMEOUT:
                del job["wait"][pid]
                job["pend"].append(pid)
                job["next_try"] = now + RETRY_GAP
                self._state_txt[pid] = "⚠ 傳送逾時，重送中…"

        left = len(job["pend"]) + len(job["wait"])
        if left:
            self.status.setText(
                f"傳送中：完成 {job['done']}、還有 {left} 台"
                f"（目的地 {job['name']}）　—— 要停請按「停止」")
        else:
            done, name = job["done"], job["name"]
            self._job = None
            self._busy_ui(False)
            self.status.setText(f"傳送完成：{done} 台已到 {name}。")

    # ------------------------------------------------------------------
    # 自動組隊
    # ------------------------------------------------------------------
    def _do_team(self) -> None:
        if self._job is not None:
            return
        wins = sorted(preload.windows(), key=lambda w: w.pid)
        picked = self._checked_pids()
        if len(picked) < 2:
            self.status.setText("自動組隊至少要勾兩台。")
            return
        # ⚠ 同一頻才組得起來（隊伍是伺服器端的，跨分流根本收不到邀請）。
        #   這是「大聲停用」不是安靜略過 —— 讓使用者知道要先換頻。
        chans = {}
        for pid in picked:
            w = self._win_of(pid, wins)
            chans[pid] = channel.current(w.hwnd) if w else None
        if None in chans.values():
            bad = [self._name_of(pid) for pid, c in chans.items() if c is None]
            self.status.setText(f"⚠ 這幾台還沒進遊戲：{'、'.join(bad)}")
            return
        if len(set(chans.values())) > 1:
            spread = "、".join(f"{self._name_of(p)}={c}頻"
                               for p, c in chans.items())
            self.status.setText(
                f"⚠ 勾選的分身不在同一頻（{spread}），組隊會收不到邀請。"
                "請先用上面的「換頻」把他們弄到同一頻。")
            return
        # ⚠⚠ 邀請封包帶的是**角色名**。角色名沒解析出來時 name_of() 會退回
        #   帳號 —— 拿帳號去邀請等於邀請一個不存在的人（而且是安靜地失敗）。
        #   所以這裡當場補掃一次，補不到就整個拒絕動作。
        unresolved = []
        for pid in picked:
            w = self._win_of(pid, wins)
            acc = _acct(w.title) if w else ""
            if acc and preload.name_of(pid, account=acc) == acc:
                self._resolve_name(pid, acc)
            if not acc or preload.name_of(pid, account=acc) == acc:
                unresolved.append(acc or str(pid))
        if unresolved:
            self.status.setText(
                f"⚠ 這幾台讀不出角色名：{'、'.join(unresolved)}。"
                "邀請封包帶的是角色名，讀不到就不送 —— 請按「重新整理」再試。")
            return
        self._state_txt.clear()
        for pid in picked:
            self._state_txt[pid] = "準備中…"
        self._update_rows(wins)
        self._job = {
            "kind": "team", "phase": "start", "prep": list(picked),
            "pend": [], "all": [], "leader": None, "targets": [],
            "cur": None, "next_invite": 0.0, "next_join": 0.0,
            "next_leave": 0.0, "share": int(self.share.currentData()),
        }
        self._busy_ui(True)
        self.status.setText(f"準備中 0/{len(picked)}（正在裝跳板）…")
        QTimer.singleShot(0, self._prep_tick)

    def _name_of(self, pid: int) -> str:
        r = self._pids.index(pid) if pid in self._pids else -1
        it = self.table.item(r, COL_NAME) if r >= 0 else None
        return it.text() if it is not None else str(pid)

    def _team_tick(self, job, wins) -> None:
        now = time.monotonic()
        alive = {w.pid for w in wins}

        # --- 開場：跳板都裝好了才決定隊長與邀請名單 -------------------
        if job["phase"] == "start":
            job["all"] = [p for p in job["pend"] if p in alive]
            if len(job["all"]) < 2:
                self._job = None
                self._busy_ui(False)
                self.status.setText("⚠ 能用的分身不到兩台（跳板裝不上？），組隊取消。")
                return
            job["leader"] = job["all"][0]
            job["targets"] = job["all"][1:]
            self._state_txt[job["leader"]] = "隊長"
            job["phase"] = "robot_off"

        # --- 中途被關掉的分身要踢出任務 ------------------------------
        for pid in [p for p in job["all"] if p not in alive]:
            job["all"].remove(pid)
            if pid in job["targets"]:
                job["targets"].remove(pid)
            if job["cur"] == pid:
                job["cur"] = None
            self._state_txt[pid] = "⚠ 分身已關閉"
        if job["leader"] not in alive:
            self._job = None
            self._busy_ui(False)
            self.status.setText("⚠ 隊長那台被關掉了，組隊中止。")
            return

        # --- ① 全部關掉天使守護精靈總開關 -----------------------------
        if job["phase"] == "robot_off":
            for pid in job["all"]:
                self._robot(pid, False)
            self.status.setText("已關掉天使守護精靈，接著清空原本的隊伍…")
            job["phase"] = "leave"
            job["next_leave"] = 0.0
            return

        # --- ② 有隊伍的通通退組（等隊員名單真的清空才算數）------------
        if job["phase"] == "leave":
            waiting = []
            for pid in job["all"]:
                sc = self._scanners.get(pid)
                got = team.members(sc) if sc is not None else None
                if got is None:
                    waiting.append(f"{self._name_of(pid)}(讀不到)")
                    self._state_txt[pid] = "⚠ 讀不到隊伍狀態"
                elif got:
                    waiting.append(self._name_of(pid))
                    self._state_txt[pid] = "退組中…"
                elif not self._state_txt.get(pid, "").startswith("✅"):
                    self._state_txt[pid] = "沒隊伍了"
            if not waiting:
                job["phase"] = "invite"
                self._state_txt[job["leader"]] = "隊長"
                return
            if now >= job["next_leave"]:
                job["next_leave"] = now + LEAVE_GAP
                for pid in job["all"]:
                    sc = self._scanners.get(pid)
                    if sc is not None and team.members(sc):
                        team.leave(self._movers.get(pid))
            self.status.setText(
                f"退組中：還有 {'、'.join(waiting)} 沒清空　—— 要停請按「停止」")
            return

        # --- ③④ 一個一個邀請，等他真的進隊伍才換下一個 ---------------
        if job["phase"] == "invite":
            lead_sc = self._scanners.get(job["leader"])
            mates = team.members(lead_sc) if lead_sc is not None else None
            names = {m.name for m in mates} if mates else set()

            if job["cur"] is not None:
                want = self._name_of(job["cur"])
                if want in names:                       # ★ 驗收訊號＝真的在名單裡
                    self._state_txt[job["cur"]] = "✅ 已入隊"
                    job["cur"] = None
                else:
                    if now >= job["next_invite"]:
                        job["next_invite"] = now + INVITE_GAP
                        ok, why = team.invite(
                            self._movers.get(job["leader"]), want, job["share"])
                        if not ok:
                            self._state_txt[job["cur"]] = f"⚠ {why}"
                    if now >= job["next_join"]:
                        job["next_join"] = now + JOIN_GAP
                        sc = self._scanners.get(job["cur"])
                        if sc is not None:
                            team.join(self._movers.get(job["cur"]), sc)
                    self.status.setText(
                        f"邀請 {want} 入隊中…（已入隊 {len(names)} 人）"
                        "　—— 要停請按「停止」")
                    return

            if job["targets"]:
                job["cur"] = job["targets"].pop(0)
                job["next_invite"] = 0.0            # 立刻送第一次
                job["next_join"] = now + JOIN_GAP   # 同意晚一點，等邀請先到
                self._state_txt[job["cur"]] = "邀請中…"
                return

            job["phase"] = "robot_on"
            return

        # --- ⑤ 全部重新打開天使守護精靈總開關 -------------------------
        if job["phase"] == "robot_on":
            for pid in job["all"]:
                self._robot(pid, True)
            n = len(job["all"])
            self._job = None
            self._busy_ui(False)
            self.status.setText(
                f"組隊完成：{n} 人一隊（隊長 {self._name_of(job['leader'])}），"
                "天使守護精靈已重新打開。")

    def _robot(self, pid: int, on: bool) -> None:
        """開／關某一台的天使守護精靈總開關（純寫記憶體，見 robot.set_run）。"""
        sc = self._scanners.get(pid)
        mv = self._movers.get(pid)
        if sc is None or mv is None:
            return
        try:
            robot.set_run(mv, sc, on)
        except Exception:                                   # noqa: BLE001
            pass

    def _stop(self, why: str) -> None:
        job = self._job
        if job is None:
            return
        # ⚠⚠ 組隊做到一半被停掉，精靈還關著 —— 那是我們留下的副作用，
        #   一定要收乾淨（使用者按停止是要「別再動了」，不是「幫我關精靈」）。
        if job["kind"] == "team" and job["phase"] not in ("start", "robot_off"):
            for pid in job.get("all", []):
                self._robot(pid, True)
            why = (why + "，天使守護精靈已重新打開") if why else why
        for pid in (list(job["prep"]) + list(job.get("pend", []))
                    + list(job.get("wait", {})) + list(job.get("all", []))):
            if not self._state_txt.get(pid, "").startswith("✅"):
                self._state_txt[pid] = "已停止"
        self._job = None
        self._busy_ui(False)
        if why:
            self.status.setText(why + "（已經送出去的那幾包不會收回）")
