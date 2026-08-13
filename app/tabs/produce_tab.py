"""自動生產分頁。

版面跟自動掛機一樣：**每台分身一個子分頁**（標題是角色名）。

## 開始自動生產（一個開關 —— 使用者要求把生產與採集合成一個）

勾下去先做**前置設定**，全部走 `robot.apply_prefs`：

    · 天使守護精靈主開關                       → 開
    · 生產頁「自動採集」「採集指定資源」        → 關（見下面「採集」）
    · 補給頁「裝備損壞回城」「修理裝備」
      「購買物品保持身上數量」                 → 開
    · 購買清單的天使之翼                       → 補到 50 個

每一項都「先讀再動」：本來就對的完全不碰，也不改使用者其他設定（購買清單
只加翼那一列，已經設更多也不改小）。取消勾選**不會**把設定改回去 ——
那是他的設定，跟自動掛機同一個原則。

⚠⚠ **採集動作本身還沒接上** —— 前置設定做完就停在那裡，狀態列會講清楚。
    不做成「勾著看起來在跑」的假象（CLAUDE.md 的失效模式）。

## 採集：交棒給官方的自動採集，但**指定要採什麼**

走過去、採、採完換下一個，都是精靈本來就會做的事。它唯一的毛病是
**不看你選了什麼**（使用者實測「選了魚藻，她跑去採花叢」），而那正好
有解 —— 遊戲自己的「採集指定資源」就是為此存在的：

    ① 使用者勾的種類 → 寫進「目標資源列表」(`DATAID_DESIGNATED_RES_LIST`)
       ＋**強制開「採集指定資源」**  → `robot.set_res_list()`
    ② 採集中心點放在要採的那種資源上（沒得挑就放腳下）
    ③ 開「自動採集」、關自動攻擊（採集跟打怪互斥，遊戲自己也這樣）
       → 以上 ②③ 都在 `robot.begin_gather()`

⚠ 那份清單是**字串**清單、記錄原本不存在，只能靠遊戲自己的
  `robotvar_add_stringlist` 建（為此讓 `lua.call` 支援了字串參數）。
  ★ 2026-08-12 使用者要求「採集指定資源」**強制勾**：一律開著，清單寫不進去
    **也不退回「全都採」** —— 寧可採不到（狀態列大聲說），也不採沒選的種類。

⛔ 走過的死路（2026-08-11，別再試）：自己送採集封包。封包結構其實解出來了
   （`0x2D` = `0x5D199B(格子X, 格子Y)`，統一入口 `0x50794B`），但**實測沒有
   任何效果** —— 走到 2 格內、照順序送 0x07/0x0C/0x2D 都沒採到。
   還缺一個沒找到的前置狀態。詳見 memory 的 gather-resource-objects。

## 周圍採集品是**按按鈕才掃**（使用者要求）

不像掛機那樣一直刷。掃一次是純讀、約幾毫秒，但沒必要一直做。

## 定位點

跟掛機的巡邏點同一套：記下角色現在的格子**與地圖**（座標是每張地圖各自
從 0 算的，不記地圖就會在別張圖走到不相干的地方）。⚠ 現在只是記錄。

## 負重滿了 → 做半成品 → 捐公會（使用者定的流程，已接上）

    負重 ≥ 95%（＝剩 5%）
      → 關天使守護精靈主開關 → 天使之翼回程
      → 走到製作檯（2~4 格內）→ 身上資源**全做成半成品**
      → 全捐公會　或　做完就停著（介面上選）
      → 回標記點 → 設定 → 開自動採集 → **最後**開主精靈

★ **什麼時候勾都能跑，而且會一直循環下去**（使用者要求）：

    採集（裝備壞了 → 官方補給流程 → 回來繼續）
      → 負重 95% → 製作 → 捐獻 → 回來採集 → …一直下去
      （處理方式選「做完就停著」的話，捐獻那一格就是終點）

勾選當下的分支（也是使用者定的）：

    已經 95% ＋ 選了捐公會 → 先試捐一次
                              還是 95% → 走上面那一整趟
                              降下來了 → 直接去標記點開工
    已經 95% ＋ 選了不處理 → 直接走回程那一趟
    沒有 95%               → **先回定位點**（需要就趴趴GO）再開工

★ 做什麼、做幾個、捐哪些**都不必人挑**：背包裡做得出來的半成品全做
  （捐得掉的先做）、湊得成整組的貢獻品全捐。所以介面上沒有選配方的清單。
  製作中狀態列會報「做什麼／第幾個／共幾個／大概還要多久」——
  時間是**量出來的**（已完成幾個 ÷ 花掉的時間），量不到就老實說估算中。

要做什麼、做得出幾個、捐不捐得掉，**全部讀遊戲自己載的表**（`recipes.py`）：

    · 學會了哪些配方 →「配方學會了沒」位元圖
    · 哪些產物算半成品 → 產物的物品分類 == 27（＝介面字串「半成品」）
    · 材料與數量       → 配方記錄自己的欄位
    · 捐不捐得掉、一組幾個 → 公會貢獻品表

⛔ **存倉庫已刪**（2026-08-11 使用者決定）：開倉庫要選 NPC 的對話選項，
   選項編號是伺服器當下發的、湊不出來（見 `app/game/produce.py` 檔頭）。
   → 處理方式只剩「全部捐公會」與「做完就停著」兩個。

★★ 進度**以遊戲自己的製作清單為準**（2026-08-13 改）：開一批之後那列
   「還剩幾個」做好一個 −1，遊戲排得比要求少（負重／背包）、吃了製作
   加倍、被打斷，全反映在上面 —— 不再自己預計然後跟現實脫鉤。清單讀
   不到才退回「材料變少」備援。「封包送出去了」永遠不算數：站錯地方、
   伺服器忙、背包滿都會讓那包沒有效果（跟掛機「真打得到的唯一訊號是
   目標血量」同一個教訓）。沒反應就重送、**不設上限**（使用者的規矩），
   出口是取消勾選。

⚠ 整趟與製作各有兜底逾時，卡住會**大聲**停並放掉勾勾，不會無聲無息停住
  （見 memory 的 frozen-tick-state-machines）。
★ 這條路整條用假遊戲層離線推過 8 個情境（`scratchpad/trip_sim.py`），
  含「沒記製作檯」「落錯地圖」「背包讀不到」「送了沒反應」四個壞情況。

## 製作檯：**工具箱自己找、自己記**（2026-08-12 改成全自動）

送 `0x36` 之前，伺服器要先知道「你在檯子前面」—— 遊戲自己的做法是**點一下
檯子**（封包 `0x05`，就是打怪那組的第二包 `attack.THIRD_FN`，動作碼給 0）。
所以製作那一段現在是：

    走到（記住的）製作檯位置 → 點旁邊的東西 → 製作面板開起來
      （Lua 全域 `WND_MAKE` 從 0 變非 0）→ 送 0x36 → **材料變少**＝真的做出來

★ 檯子的實體 ID **每次載入地圖都會重配**（低 16 位是表格索引、高 16 位是
  伺服器的世代碼），所以只存位置、不存 ID；每一趟到了現場再從
  `scenery.nearby()` 拿當下的 ID（見 app/game/scenery.py 的說明）。
★ 哪一個才是檯子，客戶端身上**沒有任何欄位**寫著（查過整個版面與資源包）
  —— 所以是**點點看**：由近而遠一個一個點，面板開起來的那個就是它，
  然後把「位置＋外觀編號」記進設定檔，下次直接用。點到桌子椅子不會有事
  （玩家本來就一直在點東西）。
★ 介面上**沒有要按的東西**（使用者 2026-08-12 要求全自動）：舊的
  「記下製作檯位置」按鈕已經拿掉，改成做成功一次就自己記起來。
"""
from __future__ import annotations

import datetime
import time
from collections import Counter

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import config
from app.core import charname, crashlog, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import (bag, entity, gather, itemname, jumpmap, locate, lua, move,
                      navigate, produce, recipes, robot, scene, scenery)
from app.tabs.base_tab import BaseTab, fit_list, no_elide

SETTINGS_PREFIX = "produce"
# 採集動作還沒接上時，狀態列一定會附上的尾巴。
# 開著時多久檢查一次「有沒有在正確地採、有沒有停下來」（使用者要求 5 秒）。
# 一次是幾毫秒的純讀，5 秒一次完全不吃資源。
GATHER_TICK_MS = 5000
# 連續幾次「位置沒動、資源也沒少」就判定停住了 → 踢一下（重設中心點）。
# 3 次 ≈ 15 秒：採一次要幾秒，走去下一個也要幾秒，太短會把正常的採集誤判成卡住。
STUCK_TICKS = 3
# ── 壞裝補給那一趟的時間（全部是使用者指定的）──────────────
RECALL_OFF_SECS = 5.0        # 回程送出後多久關掉自動採集
SUPPLY_WAIT_SECS = 120.0     # 關掉之後等 2 分鐘（讓精靈在城裡修裝、補貨；2026-08-12 從 3 分鐘改）
JUMP_LAND_SECS = 10.0        # 趴趴GO 送出後等著陸再看有沒有到
WALK_DONE = 0.8              # 走到定位點**那一格**才算到（同上，要精確）
SUPPLY_MAX_SECS = 900.0      # 整趟的兜底：超過就放棄，回到採集（大聲說）
# 做完的半成品要怎麼處理（使用者要求介面上要能選）
# ⛔ 「存進倉庫」2026-08-11 已刪：開倉庫要選 NPC 對話選項，而選項編號是
#    伺服器當下發的、湊不出來（見 app/game/produce.py 檔頭）。
#    → 只剩「捐公會」；沒選就做完停著不動。
# 捐獻歷史紀錄最多留幾筆（每台分身各一份，存在設定檔）。
DONATE_LOG_MAX = 500
DISPOSE_GUILD, DISPOSE_NONE = "guild", "none"
DISPOSE_LABELS = ((DISPOSE_GUILD, "全部捐公會"),
                  (DISPOSE_NONE, "做完就停著（不處理）"))
# ⚠ 舊設定若存的是已刪掉的「存進倉庫」，一律退成「不處理」——
#   **不准自動改成捐公會**：捐出去收不回來，那是使用者的東西。
DISPOSE_RETIRED = {"bank": DISPOSE_NONE}

# ── 負重滿了那一趟（使用者定的流程）──────────────────────────
FULL_PCT = 95.0              # 負重到這個百分比就回程做半成品（＝「剩下 5%」）
# 製作那一段要跑得比 5 秒快很多：一個半成品做幾秒，5 秒一拍等於一半時間在發呆。
# 這一拍只讀背包（幾毫秒的純讀），不送任何東西，所以 0.6 秒完全吃得消。
CRAFT_TICK_MS = 600
# 開製作面板：點了站台之後等多久沒開起來就換下一個候選點。
# ⚠ 這是**暫時性失敗自動重試**（使用者的規矩：不設上限，「取消勾選」當出口）。
CRAFT_RETRY_SECS = 6.0
# 一批最多排幾個（makeadd 的數量）。客戶端會自己把整批做完，做完再開下一批。
# ★ 開大一點＝少幾次 makeadd；材料不夠時 make_batch 只會排「做得出來的」那些。
BATCH_MAX = 999
# 一批開始後，多久沒看到進度（清單變少／材料變少）就當「卡住」→ 收掉重算。
# 客戶端一個約 2.9 秒，8 秒留足伺服器延遲。
BATCH_STALL_SECS = 8.0
# 製作的兜底：**沒有進展**超過這麼久才算卡住（每做出一個就重新起算）。
# ⚠⚠ 不能拿「總時長」當上限（2026-08-13 使用者實測踩到）：材料多的時候
#   一趟做上萬個、好幾個小時是正常的 —— 舊寫法 30/60 分鐘總時長一到就把
#   整趟砍掉、勾勾放掉，遊戲自己把已排進去的那批（≤999 個）做完就停，
#   看起來就是「工具說 9000 多、遊戲做 900 多、做完發呆」。
CRAFT_MAX_SECS = 3600.0
# ★★ 使用者要求：**要走到他定的那個點**，不要有差距（2026-08-12）。
#   所以門檻是「站上同一格」而不是「靠近就好」——格子座標都是 x.5，
#   同一格的距離差不會超過 ~0.7。
# ⚠ 走位分兩段：遠距離 navigate（A* 最短路），最後一段 move.walk_exact
#   （直送目的地、**不套打怪用的 MIN_GAP**，那個下限會讓人永遠差一格多）。
BENCH_DONE = 0.8
# 整趟（回程→製作→捐→回標記點）的兜底：**沒有進展**超過這麼久才放棄。
# ★ 製作有進展（清單變少／材料變少／開了新批）都會把起算點往後推 ——
#   見 CRAFT_MAX_SECS 的教訓：總時長上限會把大批製作砍在半路。
TRIP_MAX_SECS = 1800.0
# ★ 找製作檯：到了現場之後，多遠以內的佈景物件算「可能是檯子」。
#   點東西伺服器會判距離，站在檯子前面的話它一定在幾格內；範圍開太大只是
#   讓「一個一個點」變慢（每 CRAFT_RETRY_SECS 才點一個）。
BENCH_SCAN_R = 8.0
# 記住的檯子長什麼樣（外觀編號）要在多近的範圍內才算「就是上次那個」。
BENCH_SAME = 2.0


class CharProducePage(QWidget):
    """一台分身的生產頁。"""

    def __init__(self, pid: int, hwnd: int, title: str,
                 sc: MemoryScanner, account: str, char_name: str) -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        self.account = account
        self.char_name = char_name
        self._mover: move.Mover | None = None
        self._mover_failed = False
        self._loading = True
        # (x, y, 場景編號)；場景編號讀不到時是 None（那個點不做地圖比對）
        self._spots: list[tuple[float, float, int | None]] = []
        # ★★ 走遠路一律走 Navigator（讀地形圖自己算 A*），**不要**用
        #   mover.walk_route 直接對目標尋路 —— 那是問遊戲的尋路，距離一遠
        #   就回 0 個路徑點＝一步都不走。2026-08-11 使用者實測：在別張圖開
        #   自動生產，趴趴GO 回去之後卡在「走回標記點…還有 60 格」不動。
        #   掛機的巡邏點早就改用它了（見 memory 的 terrain-grid）。
        self._nav = navigate.Navigator()
        self._sup: dict | None = None   # 壞裝補給那一趟的狀態
        self._trip: dict | None = None  # 負重滿了那一趟的狀態
        # ★ 測試鈕用的一次性旗標（使用者要求：不必真的把背包裝滿／打壞裝備
        #   就能驗整條流程）。**只騙判斷、不騙結果** —— 後續每一步讀到的
        #   都是真的負重／真的背包，所以跑起來跟真的觸發完全一樣。
        self._fake_full = False
        self._fake_broken = False
        # 製作檯 (x, y, 場景編號, 外觀編號)；None = 還沒找到過。
        # ★ 這是**工具箱自己學的**：做成功一次就記住站在哪、點中的東西長什麼樣
        #   （實體 ID 不記 —— 換地圖就重配，見 app/game/scenery.py）。
        self._bench: tuple[float, float, int | None, int] | None = None
        # 「點了會把人帶走」的東西（門？傳送點？）——(場景, 外觀編號)。
        # ⚠ 只留在記憶體裡不存檔：這是保險，不是資料；工具箱重開就重新學。
        #   沒有它的話，第一個候選剛好是門的時候會變成「點→被傳走→回來→
        #   再點同一個」的無限迴圈。
        self._bad_props: set[tuple[int | None, int]] = set()
        self._watch_reset()
        self._build()
        self._load_settings()
        self._loading = False

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ★ 一個主開關（使用者要求把「開始生產」與「開始自動採集」合成一個）。
        self.run_cb = QCheckBox("開始自動生產")
        self.run_cb.setStyleSheet("font-weight: bold;")
        self.run_cb.setToolTip(
            "勾選後做兩件事：\n"
            "\n"
            "① 把天使守護精靈調到位\n"
            "　・主開關「開啟天使守護精靈」→ 開\n"
            "　・補給頁「裝備損壞回城」「修理裝備」「購買物品保持身上數量」→ 開\n"
            f"　・購買清單的天使之翼補到 {robot.BUY_KEEP_WINGS} 個\n"
            "　・關掉自動攻擊（採集跟打怪互斥，遊戲自己也是這樣）\n"
            "\n"
            "② 開始採集\n"
            "　・「要採集的」有勾 → 寫進遊戲的「目標資源列表」並開\n"
            "　　「採集指定資源」，**只採你選的那幾種**\n"
            "　・一個都沒勾 → 附近全部種類都採\n"
            "　・中心點放在要採的那種資源上；走過去、採、採完換下一個\n"
            "　　都是精靈做的\n"
            "\n"
            "★ 每 5 秒檢查一次：精靈被關掉會重開、這一片採光了會換地方、\n"
            "　 卡住 15 秒會重新指一次。\n"
            "\n"
            f"③ 負重到 {FULL_PCT:.0f}% 就自己回去做半成品\n"
            "　・關主精靈 → 天使之翼回程 → 走到你記的製作檯\n"
            "　・身上資源全做成半成品（可捐公會的先做）\n"
            "　・依上面選的：全捐公會／做完就停著\n"
            "　・回標記點 → 設定 → 開自動採集 → 最後開主精靈\n"
            "　★ 勾選當下就已經滿的話：先試捐一次，還是滿才跑這一趟\n"
            "\n"
            "★ 每一項都先讀再動，本來就對的完全不碰。\n"
            "★ 取消勾選只關「自動採集」，其他設定不會被改回去；\n"
            "　 正在做半成品的話也會就地停下（重試不設上限，這就是出口）。")
        self.run_cb.toggled.connect(self._on_toggle)

        run_bar = QHBoxLayout()
        run_bar.addWidget(self.run_cb)
        run_bar.addStretch(1)
        root.addLayout(run_bar)

        # ── 採集品：左邊「要採集的」、右邊「周圍採集品」──────
        panes = QHBoxLayout()

        left = QGroupBox("要採集的")
        lv = QVBoxLayout(left)
        self.picked = QListWidget()
        self.picked.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.picked)
        self.picked.setToolTip("雙擊一列可以移除。空的＝中心點放腳下，精靈自己找。")
        self.picked.itemDoubleClicked.connect(self._remove_picked)
        lv.addWidget(self.picked)
        fit_list(left, self.picked, "小天使草花叢")
        panes.addWidget(left)

        right = QGroupBox("周圍採集品")
        rv = QVBoxLayout(right)
        self.near = QListWidget()
        no_elide(self.near)
        self.near.setToolTip("雙擊一列把它加進左邊「要採集的」。")
        self.near.itemDoubleClicked.connect(self._add_from_near)
        rv.addWidget(self.near)
        # ★ 使用者要求：**按按鈕才掃**，不要一直刷。
        self.scan_btn = QPushButton("掃描周圍")
        self.scan_btn.setToolTip(
            "掃一次周圍有哪些採集品（純讀記憶體，不會對遊戲做任何事）。\n"
            "掃到的是遊戲自己那份資源清單，怪物、NPC、玩家都不會出現。")
        self.scan_btn.clicked.connect(self._scan_near)
        rv.addWidget(self.scan_btn)
        fit_list(right, self.near, "小天使草花叢 ×12（最近 6.9 格）")
        panes.addWidget(right, 1)
        root.addLayout(panes)

        # ── 定位點（做法跟掛機的巡邏點一樣）────────────────
        spot = QGroupBox("定位點")
        sv = QVBoxLayout(spot)
        self.spot_list = QListWidget()
        self.spot_list.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.spot_list)
        sv.addWidget(self.spot_list)
        srow = QHBoxLayout()
        # ⚠ 字別太長：主題給按鈕的 padding 是左右各 14px（掛機頁踩過）。
        add_btn = QPushButton("加入位置")
        add_btn.setToolTip(
            "把角色現在站的位置記下來，並記下是哪一張地圖。\n"
            "⚠ 現在只是記錄，製作動作接上之後才會用到。")
        add_btn.clicked.connect(self._add_spot)
        srow.addWidget(add_btn)
        # 用 ASCII 的 X，不要用 ✕（部分中文字型沒有那個字形會變豆腐）
        rm_btn = QPushButton("X")
        rm_btn.setFixedSize(32, 32)
        rm_btn.setStyleSheet("padding: 0;")
        rm_btn.setToolTip("刪掉選起來的定位點")
        rm_btn.clicked.connect(self._remove_spots)
        srow.addWidget(rm_btn)
        srow.addStretch(1)
        sv.addLayout(srow)
        fit_list(spot, self.spot_list, "10. 專家級遺落之地 (242, 178)")
        root.addWidget(spot)

        root.addWidget(self._build_make())

        # ── 測試（使用者要求）──────────────────────────────
        # 不必真的把背包裝滿、也不必真的把裝備打壞，就能驗整條流程。
        # ★ 只騙「要不要觸發」那一下，之後每一步讀的都是真資料。
        test_bar = QHBoxLayout()
        test_lbl = QLabel("測試：")
        test_lbl.setStyleSheet("color: #9aa2b8;")
        test_bar.addWidget(test_lbl)
        self.test_full_btn = QPushButton("假裝負重 96%")
        self.test_full_btn.setToolTip(
            "下一拍當成負重 96%（超過 95%）→ 觸發「回去做半成品」那一整趟：\n"
            "關主精靈 → 天使之翼 → 走到製作檯 → 做半成品 → 捐公會／停 →\n"
            "趴趴GO 回來 → 走回標記點 → 設定 → 開採集 → 開主精靈。\n"
            "★ 只有這一次是假的，後面每一步（包括「捐完還滿不滿」）\n"
            "　 讀到的都是真負重。\n"
            "⚠ 要先勾「開始自動生產」才有作用。")
        self.test_full_btn.clicked.connect(self._test_full)
        test_bar.addWidget(self.test_full_btn)
        self.test_broken_btn = QPushButton("假裝裝備壞掉")
        self.test_broken_btn.setToolTip(
            "下一拍當成身上有裝備耐久剩 1 → 觸發回程補給那一趟：\n"
            "天使之翼回程 → 5 秒後關自動採集 → 等 2 分鐘 →\n"
            "趴趴GO 回原圖 → 走回定位點 → 重新設定＋開採集。\n"
            "★ 只有這一次是假的。\n"
            "⚠ 要先勾「開始自動生產」才有作用。")
        self.test_broken_btn.clicked.connect(self._test_broken)
        test_bar.addWidget(self.test_broken_btn)
        test_bar.addStretch(1)
        root.addLayout(test_bar)

        self.status = QLabel("待命中")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)
        root.addStretch(1)

        # 自動採集的看門狗（只有勾著才做事）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._gather_tick)
        self._timer.start(GATHER_TICK_MS)
        # 負重那一趟自己一個快的拍子 —— 製作要一個接一個送，5 秒一拍太慢。
        # ★ 只有那一趟進行中才會轉，平常是停的（不佔資源）。
        self._trip_timer = QTimer(self)
        self._trip_timer.timeout.connect(self._trip_tick)

    # ------------------------------------------------------------------
    def _build_make(self) -> QGroupBox:
        """負重滿了要做的事：做半成品 → 捐公會（或不處理）。

        這一格只有三樣東西：負重、處理方式、製作檯位置 —— 其他都不必人挑，
        「做什麼、做幾個、捐哪些」由工具箱自己從背包與遊戲的表算出來。
        """
        box = QGroupBox("負重滿了：做半成品")
        v = QVBoxLayout(box)

        top = QHBoxLayout()
        self.weight_lbl = QLabel("負重：讀取中")
        self.weight_lbl.setStyleSheet("color: #9aa2b8;")
        top.addWidget(self.weight_lbl)
        top.addSpacing(12)
        # ⚠ 單位/說明放框外，不要用 setPrefix/setSuffix（qt-ui-pitfalls）
        top.addWidget(QLabel("做完怎麼處理"))
        self.dispose = QComboBox()
        for key, text in DISPOSE_LABELS:
            self.dispose.addItem(text, key)
        self.dispose.setToolTip(
            "半成品做好之後怎麼處理：\n"
            "・全部捐公會 —— 公會貢獻沒有上限、隨時可捐，一次一「組」\n"
            "　（一組幾個看遊戲自己的貢獻品表，多數是 200）\n"
            "・做完就停著 —— 東西留在背包，不做任何處理\n"
            "\n"
            "⛔「存進倉庫」已拿掉：開倉庫要選 NPC 的對話選項，而選項編號是\n"
            "　 伺服器當下發的清單位置、我們湊不出來，也沒有「開倉庫」的封包。\n"
            "　 與其留一條每次都要你自己開倉庫的半自動路徑，不如不做。")
        self.dispose.currentIndexChanged.connect(lambda _i: self._save_settings())
        top.addWidget(self.dispose)
        top.addSpacing(12)
        self.donate_log_btn = QPushButton("捐獻紀錄")
        self.donate_log_btn.setToolTip(
            "看這台分身捐給公會的歷史：每次捐了什麼、加了多少名聲（貢獻點數）。\n"
            "★ 名聲是照遊戲自己的貢獻品表算的（每組的點數 × 捐的組數）。")
        self.donate_log_btn.clicked.connect(self._show_donate_log)
        top.addWidget(self.donate_log_btn)
        top.addStretch(1)
        v.addLayout(top)

        # ★ 做什麼、做幾個、捐哪些**全部由工具箱自己判斷**（使用者要求）：
        #   背包裡做得出來的半成品全做、湊得成整組的貢獻品全捐。
        #   所以這裡不放「挑配方」的清單 —— 沒有東西要挑。
        row = QHBoxLayout()
        # ── 製作檯位置（手動標記 ＋ 顯示）───────────────────────
        # ★ 2026-08-12 傍晚把「記下製作檯位置」按鈕加回來（先前改全自動時拿掉了）：
        #   自動學到的位置有時落在檯子那一格，而遊戲的走路踩不上站台那格 →
        #   回程卡在 2 格外。改成「使用者站在走得到的好位置自己標」最準，
        #   4 個主號職業不同、製作檯不同各標各的。沒手動標的話仍會自動學（備援）。
        self.mark_bench_btn = QPushButton("記下製作檯位置")
        self.mark_bench_btn.setToolTip(
            "把角色**現在站的位置**記成這台分身的製作檯位置。\n"
            "★ 站在你**走得到**的好位置再按（製作檯旁邊一格就好）——\n"
            "　 別站在檯子那一格上：那一格遊戲的走路踩不上去，回程會卡住。\n"
            "★ 4 個主號職業不同、製作檯不同，各自站好各自按一次。\n"
            "　 之後每一趟回程就走到你記的這一點，再點旁邊的檯子開工。")
        self.mark_bench_btn.clicked.connect(self._mark_bench)
        row.addWidget(self.mark_bench_btn)
        self.bench_lbl = QLabel("")
        self.bench_lbl.setStyleSheet("color: #9aa2b8;")
        row.addWidget(self.bench_lbl)
        row.addStretch(1)
        v.addLayout(row)

        return box

    # ------------------------------------------------------------------
    # -- 製作檯（工具箱自己找、自己記，使用者不必按任何東西）--------------
    def _mark_bench(self) -> None:
        """**手動**把角色現在站的位置記成製作檯位置（使用者按按鈕）。

        ★ 為什麼要有這顆（2026-08-12 加回來）：自動學到的位置有時正好落在
          檯子那一格，而遊戲的走路**踩不上站台那格**（回程會卡在 2 格外）。
          讓使用者站在**走得到的好位置**（檯子旁邊一格）自己按，最準。
          4 個主號職業不同、製作檯不同，各自標各自的。
        ⚠ 純讀角色位置，不必跳板；站的那格請確定是能走上去的（旁邊一格）。
        """
        pos = self._my_pos()
        if pos is None:
            self._note("讀不到角色位置（還沒進到地圖？）—— 這次沒記下來",
                       warn=True)
            return
        # 記最近站台的外觀當指紋（找不到就記 0，位置照樣記）
        props = scenery.nearby(self.sc, pos, BENCH_SCAN_R)
        model = props[0].model if props else 0
        sid = scene.current_id(self.sc)
        self._bench = (pos[0], pos[1], sid, model)
        self._refresh_bench()
        self._save_settings()
        where = scene.scene_name(sid) if sid is not None else "未知地圖"
        near = f"（旁邊有站台，外觀 {model}）" if model else "（附近沒掃到站台）"
        self._note(f"已記下製作檯位置：{where} ({pos[0]:.0f}, {pos[1]:.0f}){near}")

    def _learn_bench(self, pos: tuple[float, float], model: int) -> None:
        """做成功一次就自動記住站在哪、點中的東西長什麼樣。

        ⚠ **只在還沒有製作檯位置時才自動學**（`self._bench is None`）——
          使用者手動標記過（或上一趟已學過）就**不覆蓋**：手動標的那點是他
          挑的走得到的好位置，自動學可能學回檯子那一格（走不上去）。
        ⚠ **不記實體 ID** —— 那號碼每次載入地圖重配（見 scenery.py），位置與
          外觀編號才是穩的。
        """
        if self._bench is not None:
            return                                 # 已有（手動標或先前學的）→ 不覆蓋
        sid = scene.current_id(self.sc)
        self._bench = (pos[0], pos[1], sid, model)
        self._refresh_bench()
        self._save_settings()

    def _refresh_bench(self) -> None:
        """製作檯那一行的文字：還沒找過就直說，找過就顯示地圖與座標。"""
        if not self._bench:
            self.bench_lbl.setText("製作檯：還沒標（站到檯子旁按「記下製作檯位置」）")
            self.bench_lbl.setStyleSheet("color: #9aa2b8;")
            self.bench_lbl.setToolTip(
                "建議：走到製作檯**旁邊一格**（走得到的位置）站好，\n"
                "按「記下製作檯位置」記起來，以後每趟回程就走到這一點。\n"
                "沒標的話工具箱也會在回程時自己找（備援），但自己找到的\n"
                "位置有時會落在檯子那格、走路踩不上去 —— 手動標最保險。")
            return
        x, y, sid = self._bench[0], self._bench[1], self._bench[2]
        where = scene.scene_name(sid) if sid is not None else "未標記地圖"
        text = f"製作檯：{where} ({x:.0f}, {y:.0f})"
        self.bench_lbl.setText(text)
        self.bench_lbl.setStyleSheet("color: #9aa2b8;")
        self.bench_lbl.setToolTip(
            text + "　（要換位置就站到新位置再按「記下製作檯位置」）"
            + (f"\n場景編號 {sid}" if sid is not None else "\n沒記到地圖"))

    # ------------------------------------------------------------------
    def _have(self) -> dict[int, int] | None:
        """背包裡每種東西各幾個。**整袋沒讀完就回 None**（不是空 dict）。

        ⚠ 讀半份背包會把「做得出幾個」算少 —— 少做無所謂，但把「讀不到」
          當成「沒材料」就是安靜地什麼都不做（bag-false-empty-guards）。
        """
        its, whole = bag.scan(self.sc)
        if not whole:
            return None
        have: Counter = Counter()
        for it in its:
            have[it.type_id] += it.count
        return dict(have)

    def _refresh_weight(self) -> None:
        """負重顯示（純讀）。讀不到就照實說，不要顯示 0%。"""
        try:
            pf = move.pathfinder_this(self.sc)
            wt = entity.weight(self.sc, pf + 8) if pf else None
        except Exception:                          # noqa: BLE001
            wt = None
        if wt is None:
            self.weight_lbl.setText("負重：讀不到")
            self.weight_lbl.setStyleSheet("color: #e0b040;")
            return
        cur, cap = wt
        pct = cur * 100.0 / cap
        self.weight_lbl.setText(f"負重：{cur} / {cap}（{pct:.0f}%）")
        self.weight_lbl.setStyleSheet(
            f"color: {'#e0b040' if pct >= 95 else '#9aa2b8'};")

    # ------------------------------------------------------------------
    # -- 開始生產 --------------------------------------------------------
    def _on_toggle(self, on: bool) -> None:
        if not on:
            # ★ 只關「自動採集」，其他設定（補給、指定資源清單）不動 ——
            #   那是使用者的設定，跟自動掛機同一個原則。
            # ★ 取消勾選也是「負重那一趟」的出口（重試不設上限，靠這個停）。
            self._trip = None
            self._trip_timer.stop()
            try:
                if self._mover is not None and self._mover.active:
                    notes = robot.end_gather(self._mover, self.sc)
                    self._note("　".join(notes) if notes else "已停止採集")
                    return
            except Exception:                      # noqa: BLE001
                pass                               # 停止流程本身不准再炸
            self._note("已停止（精靈的設定不會改回去）")
            return
        try:
            if not self._ensure_mover():
                return
            # ★ 開頭先看負重（使用者定的分支）：
            #     已經滿了 → 先試捐一次，還是滿的才跑回程那一趟
            #     沒滿     → 照舊直接開工
            # ⚠ 讀不到負重時**當成沒滿**照舊開工 —— 讀不到就把人送去回程，
            #   等於憑一個沒有的數字打斷他的掛機。
            pct = self._weight_pct()
            if pct is not None and pct >= FULL_PCT:
                if self.dispose.currentData() == DISPOSE_GUILD:
                    self._trip_start(
                        "donate_first",
                        f"一開始負重就 {pct:.0f}% → 先試著捐公會")
                else:
                    self._trip_start(
                        "robot_off",
                        f"一開始負重就 {pct:.0f}% → 回程去做半成品")
                return
            # ★ 負重沒滿 → **先回定位點**再開工（使用者要求「何時開都能跑」）。
            #   走的是同一條收尾路徑：需要的話趴趴GO → 走到定位點 → 設定
            #   → 開採集 → 最後開主精靈。人已經在點上的話這幾步幾乎是零成本。
            here = f"（負重 {pct:.0f}%）" if pct is not None else ""
            self._trip_start("back", f"回定位點準備開工{here}")
        except Exception as exc:                   # noqa: BLE001
            # ⚠ Qt 訊號槽的例外只會印到 stderr，打包成 exe 後沒人看得到。
            self._fail("開始自動生產", exc, self.run_cb)

    # ------------------------------------------------------------------
    # -- 自動採集 --------------------------------------------------------
    def wanted(self) -> list[str]:
        return [self.picked.item(i).text() for i in range(self.picked.count())]

    def _add_from_near(self, item) -> None:
        name = item.data(0x0100) or ""          # Qt.UserRole
        if name and name not in self.wanted():
            self.picked.addItem(name)
            self._save_settings()

    def _remove_picked(self, item) -> None:
        self.picked.takeItem(self.picked.row(item))
        self._save_settings()

    def _scan_near(self) -> None:
        """按一下掃一次周圍採集品（純讀）。"""
        try:
            me = self._my_pos()
            res = gather.nearby(self.sc)
        except Exception as exc:                   # noqa: BLE001
            self._note(f"⚠ 掃描失敗：{exc}", bad=True)
            return
        self.near.clear()
        if not res:
            # ⚠ 「掃不到」不等於「這裡沒有資源」—— 講清楚是哪一種
            #   （bag-false-empty-guards 那類誤報踩過六次）。
            self._note("這次沒掃到採集品（這張地圖沒有，或還沒進到地圖）",
                       warn=True)
            return
        groups: dict[str, list] = {}
        for r in res:
            groups.setdefault(r.name, []).append(r)
        for name in sorted(groups):
            rs = groups[name]
            near = min((r.dist(me) for r in rs), default=None) if me else None
            label = (f"{name} ×{len(rs)}"
                     + (f"（最近 {near:.1f} 格）" if near is not None else ""))
            self.near.addItem(label)
            it = self.near.item(self.near.count() - 1)
            it.setData(0x0100, name)               # Qt.UserRole：純名字
            it.setToolTip(f"{name}　採集種類 {rs[0].kind}　共 {len(rs)} 個\n"
                          "雙擊加進左邊「要採集的」")
        self._note(f"掃到 {len(res)} 個採集品、{len(groups)} 種")

    def target(self) -> gather.Resource | None:
        """現在該採哪一個：選中那幾種裡**離我最近**的；一個都沒選就全部算。

        ⚠ 每次都重掃、不快取資源位址：採完的資源物件會被遊戲回收，
          留著舊位址就是「對著已經不存在的東西下指令」。
        """
        me = self._my_pos()
        if me is None:
            return None
        want = set(self.wanted())
        res = [r for r in gather.nearby(self.sc) if not want or r.name in want]
        return min(res, key=lambda r: r.dist(me)) if res else None

    # ------------------------------------------------------------------
    # -- 每 5 秒的健康檢查（使用者要求）----------------------------------
    def _watch_reset(self) -> None:
        self._sig = None            # 上一拍的 (座標, 還剩幾個要採的)
        self._same = 0              # 連續幾拍完全沒變

    def _gather_tick(self) -> None:
        """每 5 秒查一次「有沒有在正確地採、有沒有停下來」（使用者要求）。

        會自己修的三件事，都是**先讀再動**、沒事就什麼都不做：
          ① 精靈主開關或「自動採集」被關掉 → 重新開起來
          ② 這一片採光了（要採的都跑出搜尋範圍外）→ 中心點移到下一處
          ③ 連續 STUCK_TICKS 拍人沒動、資源也沒少 → 當它卡住，重指一次

        ⚠ 「資源變少」才是真的有採到 —— 光看角色在不在動會騙人（走過去也
          在動）。跟掛機那邊「真打得到的唯一訊號＝目標血量」同一個教訓。
        ⚠ 沒事**不要動中心點**：改一次就打斷精靈正在進行的那一次採集。
        """
        if self._loading:
            return
        # 負重是純讀、幾微秒，不管有沒有在採都更新（使用者要看得到）。
        self._refresh_weight()
        if not self.run_cb.isChecked():
            return
        try:
            if self._mover is None or not self._mover.active:
                return
            # ★ 有哪一趟在跑就全權交給它 —— 這期間不要再去碰中心點／自動
            #   採集，不然兩邊會互相打斷（掛機那邊踩過同樣的坑）。
            if self._trip is not None:
                return                             # 由 _trip_timer 那一拍推
            if self._sup is not None:
                self._supply_tick()
                return
            # ★ 負重滿了（使用者：剩 5%）→ 交給「回程做半成品」那一趟。
            # ⚠ 讀不到負重就什麼都不做 —— 讀不到不等於滿了。
            pct = self._weight_pct()
            if pct is not None and pct >= FULL_PCT:
                self._trip_start("robot_off",
                                 f"⚠ 負重 {pct:.0f}% → 回程做半成品")
                return
            if self._check_broken():
                return
            me = self._my_pos()
            if me is None:
                self._note("⚠ 讀不到角色位置（換地圖／讀取中？）", warn=True)
                return
            want = set(self.wanted())
            res = [r for r in gather.nearby(self.sc)
                   if not want or r.name in want]
            # 座標取到小數一位就夠：採集時角色是站著不動的，走位才會變。
            sig = ((round(me[0], 1), round(me[1], 1)), len(res))
            self._same = 0 if (self._sig is None or sig != self._sig
                               ) else self._same + 1
            self._sig = sig

            # ① 精靈停了 → 重開（「有沒有停下來」最直接的一種）
            if not robot.gather_running(self.sc):
                notes = robot.begin_gather(
                    self._mover, self.sc,
                    (res[0].x, res[0].y) if res else me,
                    scene.current_id(self.sc), self.wanted())
                self._watch_reset()
                self._note("⚠ 精靈停了 → 已重新開起來　" + "、".join(notes),
                           warn=True)
                return

            if not res:
                self._note("附近沒有要採的資源了（換個地方，或按「掃描周圍」）",
                           warn=True)
                return
            near = min(r.dist(me) for r in res)

            # ② 這一片採光了：要採的都在搜尋範圍外 → 中心點移過去
            cx = robot.get_int(self._mover, self.sc, robot.AS_GATHER_ORG_X)
            cy = robot.get_int(self._mover, self.sc, robot.AS_GATHER_ORG_Y)
            rng = robot.get_int(self._mover, self.sc, robot.AS_GATHER_RANGE)
            if cx is not None and cy is not None:
                center = (cx / robot.TILE, cy / robot.TILE)
                best = min(res, key=lambda r: r.dist(center))
                if best.dist(center) > (rng or 20):
                    notes = robot.begin_gather(
                        self._mover, self.sc, (best.x, best.y),
                        scene.current_id(self.sc), self.wanted())
                    self._watch_reset()
                    self._note(f"這一片採完了 → 中心點移到 {best.name}"
                               f"（{best.dist(me):.1f} 格外）　"
                               + "、".join(notes))
                    return

            # ③ 卡住了 → 重指一次
            if self._same >= STUCK_TICKS:
                secs = self._same * GATHER_TICK_MS // 1000
                best = min(res, key=lambda r: r.dist(me))
                notes = robot.begin_gather(
                    self._mover, self.sc, (best.x, best.y),
                    scene.current_id(self.sc), self.wanted())
                self._watch_reset()
                self._note(f"⚠ {secs} 秒沒動也沒採到東西 → 重新指到 "
                           f"{best.name}（{best.dist(me):.1f} 格外）　"
                           + "、".join(notes), warn=True)
                return
            self._note(f"採集中：要採的還剩 {len(res)} 個、最近 {near:.1f} 格")
        except Exception as exc:                   # noqa: BLE001
            self._fail("健康檢查", exc, self.run_cb)

    # ------------------------------------------------------------------
    # -- 壞裝 → 回程補給 → 回來繼續採（流程是使用者定的）------------------
    def _check_broken(self) -> bool:
        """身上有壞掉的裝備就開始補給那一趟。回傳「有沒有接手」。

        ⚠ `worn_broken()` 讀不到容器會回 **None**（不是空清單）——
          那是「這一拍讀不到」，不能當成「沒壞」，更不能當成「壞了」。
          （bag-false-empty-guards 那類誤報在這個專案已經復發過六次。）
        """
        if self._fake_broken:
            self._fake_broken = False
            broken, names = [], "測試鈕"
        else:
            broken = bag.worn_broken(self.sc)
            if not broken:                # None 或空清單都不動作
                return False
            names = "、".join(i.name for i in broken[:3])
        # 記下「回來要去哪」：現在這張地圖 ＋ 這張圖的定位點。
        sid = scene.current_id(self.sc)
        self._sup = {"t0": time.monotonic(), "step": "recall",
                     "scene": sid, "spot": self._spot_here(sid),
                     "jumped": 0.0}
        self._note(f"⚠ 裝備壞了（{names}）→ 開始回程補給", warn=True)
        return True

    def _spot_here(self, sid) -> tuple[float, float] | None:
        """這張地圖的第一個定位點；沒有就回 None（那就回到原地繼續採）。

        ⚠ 沒標記地圖的舊點一律當「可以用」—— 我們不知道它當初存在哪，
          直接擋掉會讓使用者原本設好的點突然全部失效（跟掛機同一個處理）。
        """
        for x, y, s in self._spots:
            if s is None or scene.same_map(s, sid):
                return (x, y)
        return None

    def _supply_tick(self) -> None:
        """補給那一趟的狀態機。5 秒一拍，每一拍只往前推一步。

        使用者指定的流程：
            壞裝 → 用天使之翼回程 → **5 秒後**關自動採集 → 等 **2 分鐘**
            → 天使趴趴GO 回原地圖 → 走到定位點 → 重新設定＋開自動採集
        ⚠ 整趟有 SUPPLY_MAX_SECS 的兜底：卡住就放棄回到採集，並**大聲說**，
          不要無聲無息地停在半路（frozen-tick-state-machines 那類坑）。
        """
        s = self._sup
        lasted = time.monotonic() - s["t0"]
        if lasted > SUPPLY_MAX_SECS:
            self._sup = None
            self._note(f"⚠ 補給超過 {SUPPLY_MAX_SECS / 60:.0f} 分鐘還沒回到位，"
                       "放棄這一趟、直接繼續採集", warn=True)
            return

        step = s["step"]
        if step == "recall":
            if self._escape_gather(s):
                return
            # ⚠ `bag.head()` 回的是 (表頭, 格數)，do_recall 要的是**表頭**。
            #   讀不到＝這一拍讀不到（載入中／換地圖），不是「沒有背包」——
            #   等下一拍再試，不要拿 0 去送。
            h = bag.head(self.sc)
            if h is None:
                self._note("讀不到背包（載入中？）—— 等一下再回程")
                return
            ok, why = robot.do_recall(self._mover, self.sc, h[0])
            if ok is False:
                self._sup = None
                self._note(f"⚠ 回程失敗（{why}）→ 這趟取消，繼續採集", warn=True)
                return
            if ok is None:                        # 暫時性失敗，下一拍再試
                self._note(f"回程重試中…（{why}）")
                return
            s["step"], s["recalled"] = "off", time.monotonic()
            self._note("已用天使之翼回程，5 秒後關掉自動採集")

        elif step == "off":
            if time.monotonic() - s["recalled"] < RECALL_OFF_SECS:
                return
            robot.end_gather(self._mover, self.sc)
            s["step"], s["off_t"] = "wait", time.monotonic()
            self._note(f"已關掉自動採集，等 {SUPPLY_WAIT_SECS / 60:.0f} 分鐘"
                       "讓精靈修裝補貨")

        elif step == "wait":
            left = SUPPLY_WAIT_SECS - (time.monotonic() - s["off_t"])
            if left > 0:
                self._note(f"補給中…還要等 {left:.0f} 秒")
                return
            s["step"] = "jump"

        elif step == "jump":
            here = scene.current_id(self.sc)
            if here is None:
                self._note("讀不到目前地圖（載入中？）—— 等一下再傳送")
                return
            if scene.same_map(here, s["scene"]):
                s["step"] = "walk"                # 已經在原圖了
                return
            if time.monotonic() - s["jumped"] < JUMP_LAND_SECS:
                self._note("✈ 趴趴GO 已送出，等著陸…")
                return
            spot = s["spot"] or (None, None)
            e = jumpmap.nearest(s["scene"], spot[0], spot[1])
            if e is None:
                self._sup = None
                self._note(f"⚠ 趴趴GO 沒有回{scene.scene_name(s['scene'])}"
                           "的傳送點 → 這趟到此為止", warn=True)
                return
            ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
            s["jumped"] = time.monotonic()
            self._note(f"✈ 趴趴GO 回{scene.scene_name(s['scene'])}"
                       + ("" if ok else f"（送不出去：{msg}，下一拍再試）"))

        elif step == "walk":
            me = self._my_pos()
            spot = s["spot"]
            if me is None:
                return
            if spot is None:                      # 沒設定位點 → 就地繼續
                s["step"] = "resume"
                return
            d = ((me[0] - spot[0]) ** 2 + (me[1] - spot[1]) ** 2) ** 0.5
            if d <= WALK_DONE:
                self._nav.reset()
                s["step"] = "resume"
                return
            # ★ 最後一段自己貼上去：Navigator 離目標 3 格（ARRIVE）就判定「到了」
            #   不再走，若只靠它就會**卡在 1 格外一直說「還有 1 格」**（使用者
            #   2026-08-12 回報：補給飛回來後卡在「走回定位…還有 1 格」）。定位點
            #   是使用者站過的可走格，walk_exact 直接踩上去（跟 walk_spot 同一招）。
            if d <= navigate.ARRIVE:
                pf = move.pathfinder_this(self.sc)
                if pf:
                    self._mover.walk_exact(self.sc, pf + 8, spot[0], spot[1])
                self._note(f"走回定位點…還有 {d:.1f} 格")
                return
            # ⚠ 跟 _trip 那邊同一個坑：遠距離**不能**用 mover.walk_route
            #   （對目標本身尋路，遠了就回 0 個點＝一步都不走）。
            note = self._walk_to(spot[0], spot[1])
            if self._nav.stuck:
                self._nav.reset()
                s["step"] = "resume"
                self._note("⛔ 走不到定位點，就地繼續採集", warn=True)
                return
            self._note(f"走回定位點…還有 {d:.0f} 格　{note}")

        elif step == "resume":
            # ★ 重新「設定」＋開採集 —— 就是生產面板那顆「設定」做的事，
            #   但我們**不叫那支 UI handler**：它最後會 CreateStageMapWnd
            #   把大地圖叫出來（使用者提醒過）。begin_gather 已經把它做的
            #   事（中心點、地圖 ID、清 USED_TP_ITEM）全做了，地圖也就不會跳。
            me = self._my_pos()
            tgt = self.target()
            center = (tgt.x, tgt.y) if tgt else (me or (0.0, 0.0))
            notes = robot.begin_gather(self._mover, self.sc, center,
                                       scene.current_id(self.sc),
                                       self.wanted())
            self._sup = None
            self._watch_reset()
            self._note("補給完成，回來繼續採集　" + "、".join(notes))

    # ------------------------------------------------------------------
    # -- 負重滿了那一趟（流程是使用者定的）--------------------------------
    #
    #   負重 95% → 關主精靈 → 天使之翼回程 → 走到製作檯 → 身上資源全做成
    #   半成品 → 全捐公會（或做完就停）→ 回標記點 → 設定＋開自動採集
    #   → 開主精靈
    #
    #   剛勾選時的分支（也是使用者定的）：
    #     已經 95% → 先試捐一次 → 還是 95% → 走上面那一整趟
    #                            → 降下來了 → 直接去標記點開工
    #     沒有 95% → 直接去標記點開工
    #
    # ⚠ 一拍只往前推一步（跟壞裝補給那個狀態機同一個寫法）。每一步都可能
    #   讀不到東西 —— 讀不到就**原地等下一拍**，不要往前推、更不要當成完成。
    # ⚠ 整趟有 TRIP_MAX_SECS 兜底：卡住就大聲說並收尾，不要無聲無息停住
    #   （見 memory 的 frozen-tick-state-machines）。
    # ------------------------------------------------------------------
    def _home_spot(self) -> tuple[float, float, int | None] | None:
        """採集要回去的那個定位點 (x, y, 場景編號)；一個都沒設就 None。

        先挑**現在這張圖**的（掛機中負重滿了時，那就是原來在採的地方）；
        這張圖沒有就用第一個 —— 剛開工時人常常在城裡，這樣才回得去。
        ⚠ 沒標記地圖的舊點一律當「可以用」，不然使用者原本設好的點會突然
          全部失效（跟掛機同一個處理）。
        """
        sid = scene.current_id(self.sc)
        for x, y, s in self._spots:
            if s is None or (sid is not None and scene.same_map(s, sid)):
                return (x, y, s if s is not None else sid)
        return self._spots[0] if self._spots else None

    def _trip_start(self, step: str, why: str) -> None:
        home = self._home_spot()
        self._trip = {"step": step, "t0": time.monotonic(), "note": why,
                      # ★ 要回去的是**定位點那張圖**，不是現在站的這張 ——
                      #   剛開工時人可能在城裡，用現在這張就永遠不會傳送。
                      "scene": home[2] if home else scene.current_id(self.sc),
                      "spot": (home[0], home[1]) if home else None,
                      "jumped": 0.0, "craft": None}
        self._trip_timer.start(CRAFT_TICK_MS)
        self._note(why)

    def _trip_end(self, msg: str, warn: bool = False, bad: bool = False) -> None:
        self._trip = None
        self._trip_timer.stop()
        self._note(msg, warn=warn, bad=bad)

    # ------------------------------------------------------------------
    # -- 測試鈕（使用者自己按，不用等真的觸發）----------------------------
    def _test_ready(self) -> bool:
        """測試鈕只有在「開始自動生產」勾著時才有意義 —— 心跳沒在跑，
        旗標放了也沒有人來吃，那就會變成「按了沒反應」。"""
        if self.run_cb.isChecked():
            return True
        self._note("⚠ 要先勾「開始自動生產」，測試鈕才會有作用", warn=True)
        return False

    def _test_full(self) -> None:
        if not self._test_ready():
            return
        self._fake_full = True
        self._note("測試：下一拍會當成負重 96% → 準備回去做半成品")

    def _test_broken(self) -> None:
        if not self._test_ready():
            return
        self._fake_broken = True
        self._note("測試：下一拍會當成裝備壞掉 → 準備回程補給")

    def _weight_pct(self) -> float | None:
        """負重百分比；**讀不到回 None**（不是 0）。

        ★ 測試鈕按下後**只有下一次**會回 96%（見 `_fake_full`）：流程一旦
          被觸發，後面每一次量到的都是真負重 —— 例如「捐完還滿不滿」那一步
          就會照實回答，不會被假值帶著跑。
        """
        if self._fake_full:
            self._fake_full = False
            return 96.0
        try:
            pf = move.pathfinder_this(self.sc)
            wt = entity.weight(self.sc, pf + 8) if pf else None
        except Exception:                          # noqa: BLE001
            return None
        return None if wt is None else wt[0] * 100.0 / wt[1]

    def _donatable(self) -> list[tuple[int, int]] | None:
        """背包裡「湊得成整組」的貢獻品 → [(貢獻編號, 組數), …]。

        讀不到整袋背包或貢獻品表就回 **None**（不是空清單）—— 那是
        「這一拍讀不到」，當成「沒東西可捐」會讓整個流程安靜地跳過捐獻。
        """
        have = self._have()
        if have is None:
            return None
        cmap = recipes.contrib_by_item(self.sc)
        if cmap is None:
            return None
        out = []
        for tid, n in have.items():
            c = cmap.get(tid)
            if c and c.group > 0 and n >= c.group:
                out.append((c.cid, n // c.group))
        return out

    # ------------------------------------------------------------------
    # -- 捐獻歷史紀錄（捐了什麼、加多少名聲）------------------------------
    def _log_donation(self, rows: list[tuple[int, int]]) -> None:
        """把這次捐獻記進歷史。`rows` = 剛送出去的 [(貢獻編號, 組數), …]。

        ★ 名聲＝照遊戲自己的貢獻品表算：每組點數(`Contrib.points`) × 捐的組數。
          物品名字、每組幾個也都從那張表查（不猜）。表讀不到就不記這筆。
        ⚠ 公會貢獻沒有上限、隨時可捐（介面說明），送出去等於捐成功，所以拿
          「送出去的 rows」當紀錄；表查不到的編號跳過（不硬湊）。
        """
        contribs = recipes.contribs(self.sc)
        if not contribs:
            return
        by_cid = {c.cid: c for c in contribs}
        items, total = [], 0
        for cid, groups in rows:
            c = by_cid.get(int(cid))
            if not c or groups <= 0:
                continue
            pts = int(groups) * c.points
            items.append([itemname.of(c.item), int(groups) * c.group, pts])
            total += pts
        if not items:
            return
        rec = {"t": datetime.datetime.now().strftime("%m-%d %H:%M"),
               "items": items, "total": total}
        log = config.get(self._key("donate_log"), []) or []
        log.append(rec)
        if len(log) > DONATE_LOG_MAX:
            log = log[-DONATE_LOG_MAX:]
        config.set(self._key("donate_log"), log)

    def _show_donate_log(self) -> None:
        """捐獻歷史視窗：每次捐了什麼＋加多少名聲，新的在上面＋累計名聲。"""
        log = config.get(self._key("donate_log"), []) or []
        dlg = QDialog(self)
        dlg.setWindowTitle(f"捐獻紀錄 — {self.char_name}")
        dlg.resize(480, 500)
        lay = QVBoxLayout(dlg)

        grand = sum(int(r.get("total", 0)) for r in log)
        head = QLabel(f"共 {len(log)} 次捐獻，累計名聲 {grand}")
        head.setStyleSheet("font-weight: bold; color: #d0d4e0;")
        lay.addWidget(head)

        lst = QListWidget()
        no_elide(lst)
        for r in reversed(log):                        # 新的在上面
            what = "、".join(f"{n}×{cnt}" for n, cnt, _p in r.get("items", []))
            lst.addItem(f"{r.get('t','')}　{what}　+{int(r.get('total',0))} 名聲")
        if not log:
            lst.addItem("還沒有捐獻紀錄")
        lay.addWidget(lst, 1)

        btns = QHBoxLayout()
        clr = QPushButton("清除紀錄")
        clr.setToolTip("清空捐獻歷史（只清紀錄，不影響遊戲）。")
        clr.clicked.connect(lambda: self._clear_donate_log(dlg))
        btns.addWidget(clr)
        btns.addStretch(1)
        close = QPushButton("關閉")
        close.clicked.connect(dlg.accept)
        btns.addWidget(close)
        lay.addLayout(btns)
        dlg.exec()

    def _clear_donate_log(self, dlg: QDialog) -> None:
        config.set(self._key("donate_log"), [])
        dlg.accept()
        self._note("已清空捐獻紀錄")

    def _trip_tick(self) -> None:
        """負重那一趟的狀態機。CRAFT_TICK_MS 一拍。"""
        if self._trip is None or self._loading:
            return
        try:
            self._trip_step()
        except Exception as exc:                   # noqa: BLE001
            self._trip = None
            self._trip_timer.stop()
            self._fail("負重處理", exc, self.run_cb)

    def _trip_step(self) -> None:                  # noqa: C901 —— 狀態機本來就長
        s = self._trip
        # ⚠ s["t0"] 是「上一次有進展」的時間，不是這一趟開始的時間 ——
        #   製作那一段每做出一個就會把它往後推（一批 999 個要做快 1 小時，
        #   拿總時長判會把好好在做的整趟砍掉，2026-08-13 使用者踩到）。
        if time.monotonic() - s["t0"] > TRIP_MAX_SECS:
            self._trip_end(
                f"⚠ 這一趟超過 {TRIP_MAX_SECS / 60:.0f} 分鐘沒有任何進展，"
                "停在這裡不再自己動 —— 請看一下角色卡在哪", bad=True)
            self._halt(self.run_cb, self.status.text())
            return
        step = s["step"]

        # ── 開頭就 95%：先試捐一次，看能不能不用跑這一趟 ──────
        if step == "donate_first":
            rows = self._donatable()
            if rows is None:
                self._note("讀不到背包／貢獻品表 —— 等一下再試著捐")
                return
            if rows:
                ok, msg = produce.donate(self._mover, self.sc, rows)
                if ok:
                    self._log_donation(rows)       # 記進捐獻歷史
                self._note(("先捐一次：" + msg) if ok else f"⚠ 捐獻沒送出：{msg}",
                           warn=not ok)
            else:
                self._note("身上沒有湊得成整組的貢獻品 —— 直接去做半成品")
            s["step"], s["donated_at"] = "donate_check", time.monotonic()
            return

        if step == "donate_check":
            # 捐出去到伺服器扣背包有延遲，給它幾拍再量負重。
            if time.monotonic() - s["donated_at"] < 3.0:
                return
            pct = self._weight_pct()
            if pct is None:
                self._note("讀不到負重 —— 等一下再看")
                return
            if pct < FULL_PCT:
                s["step"] = "back"
                self._note(f"捐完負重降到 {pct:.0f}%，回標記點繼續採")
            else:
                s["step"] = "robot_off"
                self._note(f"捐完還是 {pct:.0f}% → 回程去做半成品")
            return

        # ── 回程做半成品 ────────────────────────────────────
        if step == "robot_off":
            # ★ 沒記過製作檯**不再是停止的理由**（2026-08-12 改全自動）：
            #   回程落地之後就地由近而遠點點看，找到了自己記起來。
            robot.end_gather(self._mover, self.sc)
            ok, why = robot.set_run(self._mover, self.sc, False)
            if not ok:
                self._note(f"關主精靈重試中…（{why}）")
                return
            s["step"] = "recall"
            self._note("已關掉天使守護精靈主開關，準備回程")
            return

        if step == "recall":
            if self._escape_gather(s):
                return
            h = bag.head(self.sc)
            if h is None:
                self._note("讀不到背包（載入中？）—— 等一下再回程")
                return
            # ⚠ need_robot=False：上一步才剛把主精靈關掉（使用者指定的
            #   順序），這裡再要求「精靈要在執行中」就是自相矛盾 ——
            #   2026-08-12 實測卡在「天使精靈不在執行中，這趟取消」。
            #   我們只是自己用掉一張翼，不靠精靈帶回城。
            ok, why = robot.do_recall(self._mover, self.sc, h[0],
                                      need_robot=False)
            if ok is False:
                self._trip_end(f"⚠ 回程失敗（{why}）—— 這趟取消", bad=True)
                self._halt(self.run_cb, self.status.text())
                return
            if ok is None:
                self._note(f"回程重試中…（{why}）")
                return
            s["step"], s["recalled"] = "land", time.monotonic()
            self._note("已用天使之翼回程，等著陸…")
            return

        if step == "land":
            if time.monotonic() - s["recalled"] < RECALL_OFF_SECS:
                return
            here = scene.current_id(self.sc)
            if here is None:
                self._note("讀不到目前地圖（載入中？）")
                return
            # ⚠ 記住的製作檯是哪張圖的，就只能在哪張圖用。天使之翼的落點不見得
            #   是那一張 —— 不比對就會在別的城裡走到一組不相干的座標。
            #   ★ 對不上**不停下來**：就地找（落地點附近點點看），找到再記新的。
            bsid = self._bench[2] if self._bench else None
            if self._bench and bsid is not None and not scene.same_map(here,
                                                                       bsid):
                self._note(f"⚠ 天使之翼落在{scene.scene_name(here)}，"
                           f"但記住的製作檯在{scene.scene_name(bsid)}"
                           " —— 就地找找看", warn=True)
                s["find_here"] = True
            s["step"] = "to_bench"
            return

        if step == "to_bench":
            me = self._my_pos()
            if me is None:
                return
            # 沒記過製作檯（或記的是別張圖）→ 就地開工：製作那一段會從腳邊
            # 由近而遠點點看，面板開起來的那個就是檯子。
            if self._bench is None or s.get("find_here"):
                self._nav.reset()
                s["step"] = "craft"
                s["craft"] = self.new_craft_state()
                self._note("還不知道製作檯在哪 —— 就從腳邊開始一個一個找")
                return
            bx, by = self._bench[0], self._bench[1]
            d = ((me[0] - bx) ** 2 + (me[1] - by) ** 2) ** 0.5
            if d <= BENCH_DONE:
                self._nav.reset()
                s["step"] = "craft"
                s["craft"] = self.new_craft_state()
                # ★ 把「記的」跟「實際站的」都講出來 —— 差多少一眼看得到，
                #   不必猜（使用者回報過「走過去跟我定的位置不同」）。
                self._note(f"到製作檯了（記的 {bx:.0f},{by:.0f}／"
                           f"實際 {me[0]:.0f},{me[1]:.0f}／差 {d:.1f} 格），"
                           "開始做半成品")
                return
            # ★ 最後 3 格 Navigator 會判定「到了」而不再走（ARRIVE=3.0），
            #   所以近距離改用 walk_near 直接貼過去（不尋路 —— 近距離尋路
            #   必回 0，見 memory 的 near-walk-and-lua-crash）。
            if d <= navigate.ARRIVE:
                # ★ Navigator 離目標 3 格就判定「到了」而不再走（ARRIVE），
                #   最後這段自己送目的地 —— walk_exact 不留距離，直接站上去。
                pf = move.pathfinder_this(self.sc)
                if pf:
                    self._mover.walk_exact(self.sc, pf + 8, bx, by)
                self._note(f"走到製作檯上…還有 {d:.1f} 格")
                return
            note = self._walk_to(bx, by)
            if self._nav.stuck:
                # ⛔ 地形圖說走不到記住的那個位置（地圖改了？落在障礙物裡？）。
                #   不放棄整趟：就地找一次（腳邊也可能就有檯子），找不到那一段
                #   自己會大聲說。
                self._nav.reset()
                s["find_here"] = True
                self._note(f"⛔ 走不到記住的製作檯（{bx:.0f},{by:.0f}）"
                           " —— 改成就地找找看", warn=True)
                return
            self._note(f"走去製作檯…還有 {d:.0f} 格　{note}")
            return

        if step == "craft":
            self._craft_step(s)
            return

        # ── 做完了 → 捐 or 停 ───────────────────────────────
        if step == "dispose":
            if self.dispose.currentData() != DISPOSE_GUILD:
                self._trip_end("半成品做完了。處理方式是「做完就停著」—— "
                               "東西留在背包，循環到此結束", warn=True)
                self._halt(self.run_cb, self.status.text())
                return
            rows = self._donatable()
            if rows is None:
                self._note("讀不到背包／貢獻品表 —— 等一下再捐")
                return
            if not rows:
                self._note("做完了，但沒有湊得成整組的貢獻品可捐")
                s["step"] = "back"
                return
            ok, msg = produce.donate(self._mover, self.sc, rows)
            if not ok:
                self._note(f"捐獻重試中…（{msg}）")
                return
            self._log_donation(rows)               # 記進捐獻歷史（捐了什麼＋名聲）
            s["step"] = "back"
            self._note(msg + "　→ 回標記點")
            return

        # ── 回標記點 → 重新設定 → 開採集 → 開主精靈 ──────────
        if step == "back":
            here = scene.current_id(self.sc)
            if here is None:
                self._note("讀不到目前地圖（載入中？）")
                return
            if s["scene"] is None or scene.same_map(here, s["scene"]):
                s["step"] = "walk_spot"
                return
            if time.monotonic() - s["jumped"] < JUMP_LAND_SECS:
                self._note("✈ 趴趴GO 已送出，等著陸…")
                return
            spot = s["spot"] or (None, None)
            e = jumpmap.nearest(s["scene"], spot[0], spot[1])
            if e is None:
                self._trip_end(
                    f"⚠ 趴趴GO 沒有回{scene.scene_name(s['scene'])}的傳送點"
                    " —— 這趟到此為止", bad=True)
                self._halt(self.run_cb, self.status.text())
                return
            ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
            s["jumped"] = time.monotonic()
            self._note(f"✈ 趴趴GO 回{scene.scene_name(s['scene'])}"
                       + ("" if ok else f"（送不出去：{msg}，下一拍再試）"))
            return

        if step == "walk_spot":
            spot = s["spot"]
            if spot is None:                       # 沒設定位點 → 就地開工
                s["step"] = "resume"
                return
            me = self._my_pos()
            if me is None:
                return
            d = ((me[0] - spot[0]) ** 2 + (me[1] - spot[1]) ** 2) ** 0.5
            if d <= WALK_DONE:
                self._nav.reset()
                s["step"] = "resume"
                return
            if d <= navigate.ARRIVE:            # 最後一段自己貼上去
                pf = move.pathfinder_this(self.sc)
                if pf:
                    self._mover.walk_exact(self.sc, pf + 8, spot[0], spot[1])
                self._note(f"走到標記點上…還有 {d:.1f} 格")
                return
            note = self._walk_to(spot[0], spot[1])
            if self._nav.stuck:
                # ⛔ 真的到不了（地形圖說沒有路）→ **不要站到天亮**：
                #   就地開工比杵在原地好，而且要大聲說出來。
                self._nav.reset()
                s["step"] = "resume"
                self._note(f"⛔ 走不到標記點（{spot[0]:.0f},{spot[1]:.0f}）"
                           f"，就地開始採集", warn=True)
                return
            self._note(f"走回標記點…還有 {d:.0f} 格　{note}")
            return

        if step == "resume":
            # ★ 順序照使用者說的：**設定 → 開自動採集 → 最後才開主精靈**。
            #   所以這裡的 apply_prefs 不帶 main_switch（False ＝ 完全不碰它），
            #   等採集設定好了再單獨把主開關打開 —— 精靈一開就會照設定跑，
            #   先開它等於讓它拿舊設定動起來。
            notes = robot.apply_prefs(self._mover, self.sc, supply=True)
            me = self._my_pos()
            tgt = self.target()
            center = (tgt.x, tgt.y) if tgt else (me or (0.0, 0.0))
            notes += robot.begin_gather(self._mover, self.sc, center,
                                        scene.current_id(self.sc),
                                        self.wanted())
            ok, why = robot.set_run(self._mover, self.sc, True)
            if not ok:
                # ⚠ 主開關沒開起來就**不要結束** —— 結束＝以為在跑其實沒跑。
                self._note(f"開主精靈重試中…（{why}）")
                return
            self._watch_reset()
            self._trip_end("回到標記點，已重新設定、開採集、開主精靈　"
                           + "、".join(notes))
            return

    # ------------------------------------------------------------------
    @staticmethod
    def new_craft_state() -> dict:
        """製作那一段的狀態。**只在這裡建**，離線模擬也用這支 ——
        兩邊各建一份的話，加了欄位就會有一邊漏掉（然後只在跑到一半時炸）。

            idx    現在做 plans 的第幾個配方
            sig    上一次看到的材料總數（**備援**訊號：清單讀不到才用它計數）
            last_drop 上次看到進度的時間（判斷這一批停了沒）
            made   已經做出幾個
            t0     **上一次有進展**的時間（看門狗起算點；⚠ 不是開始時間 ——
                   拿總時長判會把幾小時的大批製作砍在半路，2026-08-13 踩到）
            plans  這一輪的配方清單（材料用完就重算）
            total  總共要做幾個（給人看的進度；**每開一批就拿背包重算**）
            first  第一個做好的時間（拿來估「一個要多久」）
            props  這附近可點的**站台**（scenery.nearby 回的互動物；None=還沒掃）
            pi     現在點到第幾個候選
            poked  最後點中的那個候選（面板開起來就是它 → 記起來）
            wnd    製作面板 WND_MAKE 的值（開著才非 0）
            batch  這一批的狀態（None=還沒開批；開了＝{"added":遊戲排進去的
                   數量, "mk":製作清單控制項指標, "left":清單還剩幾個}）
            scene  開始做的時候在哪張圖（點到門被帶走時看得出來）
        """
        return {"idx": 0, "sig": None, "last_drop": 0.0, "made": 0,
                "t0": time.monotonic(), "plans": None,
                "total": None, "first": None,
                "props": None, "pi": 0, "poked": None,
                "wnd": None, "batch": None, "scene": None}

    # ------------------------------------------------------------------
    def _poke_bench(self, c: dict, advance: bool = False) -> str:
        """點一個製作站台候選，讓伺服器把製作面板開起來。回狀態字串。

        ★ `scenery.nearby` 現在回的是**真的互動站台**（vtable 0x7D87B4／kind0／
          有選定 id，見 scenery.py），不是舊的 model685 純裝飾。哪個是「烹飪」檯
          客戶端不知道 → 由近而遠一個一個點，**面板開起來（WND_MAKE 非0）的
          那個就是它**（呼叫端下一拍判斷）。點到別的站台/門不會壞事。
        ⚠ 讀不到面板狀態時照點不誤（把「讀不到」當「開著」會卡死 ——
          bag-false-empty-guards）。
        """
        if not (self._mover and self._mover.active):
            return ""
        me = self._my_pos()
        if me is None:
            return "讀不到角色位置"
        if c["props"] is None or advance:
            props = scenery.nearby(self.sc, me, BENCH_SCAN_R)
            if props is None:
                return "⚠ 讀不到附近的物件（載入中？）—— 等一下再找製作檯"
            if c["props"] is None:
                c["props"], c["pi"] = props, 0
            else:
                # 換下一個候選；繞完一圈就重掃一次（走動過、視野也會變）
                c["pi"] += 1
                if c["pi"] >= len(props):
                    c["pi"] = 0
                c["props"] = props
        here = scene.current_id(self.sc)
        props = [p for p in (c["props"] or [])
                 if (here, p.model) not in self._bad_props]
        c["props"] = props
        if not props:
            return (f"⛔ {BENCH_SCAN_R:.0f} 格內沒有可點的製作站台 —— "
                    "這裡沒有檯子（角色停在這，不會亂走）")
        # ★ 記住的檯子長什麼樣就先點它（位置對得上＋外觀編號一樣）
        if not advance and self._bench and self._bench[3]:
            bx, by, _sid, model = self._bench
            for k, p in enumerate(props):
                if p.model == model and p.dist((bx, by)) <= BENCH_SAME:
                    c["pi"] = k
                    break
        p = props[min(c["pi"], len(props) - 1)]
        ok, msg = produce.click(self._mover, self.sc, p)
        c["poked"] = p if ok else None
        if not ok:
            return f"⚠ 點製作檯沒送出（{msg}）"
        return (f"點了第 {c['pi'] + 1}/{len(props)} 個候選"
                f"（{p.dist(me):.1f} 格），等面板開…")

    def _craft_step(self, s: dict) -> None:
        """一拍推一格製作。**開一整批交給客戶端自己做完，工具箱只監看。**

        流程（2026-08-12 傍晚實機定案，見 memory craft-donate-storage-packets）：
          點站台開面板(WND_MAKE 非0) → `produce.make_batch(配方,數量)` 選配方+設
          數量+makeadd+makestart → 客戶端每 ~2.9 秒自己做一個直到清單空。
        ★★ 進度**以遊戲自己的製作清單為準**（`produce.make_left` 純讀那列
          「還剩幾個」，2026-08-13 改）：遊戲排得比要求少（負重／背包上限）、
          吃了製作加倍、被打斷 —— 全都反映在清單上，不再自己預計然後跟現實
          脫鉤。清單讀不到才退回「材料變少」備援；清單空了**馬上**開下一批。
          「總共要做幾個」每開一批也拿當下的背包重算一次。
        ⚠ **不再自送 0x36**（舊寫法只做得出第一個就被伺服器拒、跳「製作物品
          失敗」還卡住訊息迴圈）。一批停了 BATCH_STALL_SECS 沒進度就收掉重算：
          材料還有就再開一批，換配方或做完就往下走。
        ⚠ 站錯地方／伺服器忙／背包滿都可能讓一批沒生效 → 收掉重來，**不設上限**
          （使用者的規矩），出口是取消勾選；CRAFT_MAX_SECS 是「沒有進展」的
          兜底，**不是總時長** —— 一趟做上萬個、好幾個小時是正常的。
        """
        c = s["craft"]
        now = time.monotonic()
        # ⚠ 點東西是有風險的一步：萬一點到的是門／傳送點，人會被帶走。
        #   場景一變就**立刻停手**、把那個外觀記成「不要再點」，回去採集。
        here = scene.current_id(self.sc)
        if here is not None:
            if c.get("scene") is None:
                c["scene"] = here
            elif not scene.same_map(here, c["scene"]):
                if c.get("poked") is not None:
                    self._bad_props.add((c["scene"], c["poked"].model))
                s["step"] = "back"
                self._note(f"⚠ 點了東西之後人被帶到{scene.scene_name(here)}"
                           " —— 那個不是製作檯，已記住不再點它，這趟先回去採集",
                           warn=True)
                return
        # ⚠ c["t0"] 也是「上一次有進展」的時間（有做出東西就重設）——
        #   這是「卡死一小時」的兜底，不是總時長上限。
        if now - c["t0"] > CRAFT_MAX_SECS:
            self._note(f"⚠ 製作超過 {CRAFT_MAX_SECS / 60:.0f} 分鐘沒有進展，"
                       f"先收尾（已做 {c['made']} 個）", warn=True)
            s["step"] = "dispose"
            return

        have = self._have()
        if have is None:
            return                                 # 這一拍讀不到背包，等下一拍

        # 一開始先算「總共要做幾個」給人看。⚠ 一定要用 schedule() 不是 plan()
        # —— plan() 的數字是各配方單獨算的，加起來會灌水（見那支的說明）。
        if c["total"] is None:
            sched = recipes.schedule(self.sc, have)
            if sched is None:
                return                             # 配方表讀不到，等下一拍
            c["total"] = sum(n for _r, n in sched)
            if c["total"]:
                what = "、".join(f"{itemname.of(r.product)}×{n}"
                                 for r, n in sched[:3])
                more = "…" if len(sched) > 3 else ""
                self._note(f"要做 {c['total']} 個：{what}{more}")
                return          # 讓這句留在狀態列一拍，不要同一拍就被蓋掉

        # 還沒挑配方、或上一個配方材料用完了 → 重新算一次做得出什麼。
        if c["plans"] is None:
            plans = recipes.plan(self.sc, have)
            if plans is None:
                return                             # 配方表讀不到，等下一拍
            c["plans"], c["idx"] = plans, 0
            c["sig"], c["batch"] = None, None
        if c["idx"] >= len(c["plans"]):
            c["plans"] = None
            if not recipes.plan(self.sc, have):
                s["step"] = "dispose"
                self._note(f"半成品做完了，一共 {c['made']} 個")
            return

        cur = c["plans"][c["idx"]]
        # 這個配方現在還做得出幾個（拿背包當唯一真相，不信先前算的）
        makeable = min((have.get(mid, 0) // num for mid, num in cur.recipe.mats),
                       default=0)
        if makeable <= 0:                          # 這個配方材料用完 → 下一個
            # ★ 走人之前先把**最後一件**記上：材料歸零的這一拍還沒輪到下面的
            #   批次計數就 return 了，少補這一下每個配方都會少算一個
            #   （trip_sim 情境⑭抓到的「一共 9 個」）。
            if c["batch"] is not None and c["sig"] is not None:
                sig0 = sum(have.get(mid, 0) for mid, _n in cur.recipe.mats)
                if sig0 < c["sig"]:
                    step = sum(num for _m, num in cur.recipe.mats) or 1
                    c["made"] += max(1, (c["sig"] - sig0) // step)
                    s["t0"] = c["t0"] = now
            c["idx"] += 1
            c["sig"], c["batch"] = None, None
            return

        # ── 確保製作面板開著（點站台；開起來的那個就記成製作檯）──────────
        po = produce.panel_open(self.sc)
        if po is False:
            c["wnd"], c["batch"] = None, None
            # 點過了就等面板開（CRAFT_RETRY_SECS）；等不到才換下一個候選點
            waited = now - c.get("poke_t", 0)
            if c.get("poked") is not None and waited < CRAFT_RETRY_SECS:
                self._note("點了製作檯，等面板開…")
                return
            poke = self._poke_bench(c, advance=bool(c.get("poked")))
            c["poke_t"] = now
            self._note(poke or "找製作檯…", warn=poke.startswith(("⛔", "⚠")))
            return
        # po is None＝**讀不到**面板狀態，不是「沒開」（panel_open 的說明就
        # 這麼寫）：不重置批次也不去亂點站台 —— 進行中的批次退回材料訊號監看。

        # 面板開著 → 記下 WND_MAKE，並把「剛剛點開它的那個站台」學起來
        if c["wnd"] is None:
            g = lua.globals_of(self.sc, ("WND_MAKE",)) or {}
            c["wnd"] = g.get("WND_MAKE")
            if c.get("poked") is not None:
                me = self._my_pos()
                if me is not None:
                    self._learn_bench(me, c["poked"].model)
            if not c["wnd"]:
                return                             # 讀不到 WND_MAKE，等下一拍

        sig = sum(have.get(mid, 0) for mid, _n in cur.recipe.mats)

        # ── 沒有進行中的批次 → 開一批（選配方+設數量+makeadd+makestart）──────
        if c["batch"] is None:
            if po is not True:
                return                 # 面板狀態不明，先不開新批（等下一拍）
            # ★ 開批前先跟現實對一次帳：「總共要做幾個」拿當下背包重算。
            #   一開始那個數字會失真 —— 負重／背包會讓遊戲少排、製作加倍會
            #   讓產出變多，不重算畫面就跟遊戲脫鉤（2026-08-13 使用者回報）。
            sched = recipes.schedule(self.sc, have)
            if sched is not None:
                c["total"] = c["made"] + sum(n for _r, n in sched)
            qty = min(makeable, BATCH_MAX)
            ok, added, mk, msg = produce.make_batch(
                self._mover, self.sc, c["wnd"], cur.recipe.rid, qty)
            if not ok:
                if msg == produce.WRONG_PANEL:
                    # ★ 點到**別種生產**的檯子（面板裡沒有這個配方）→ 關掉它、
                    #   把這個外觀記成「這台不是」，下一拍換下一個站台點。
                    #   這樣「附近有對的台子」就找得到，不會卡在錯面板重試。
                    produce.close_panel(self._mover, self.sc)
                    if c.get("poked") is not None:
                        self._bad_props.add((here, c["poked"].model))
                    c["wnd"], c["batch"], c["poked"] = None, None, None
                    self._note(f"這個檯子{msg} → 換下一個站台", warn=True)
                    return
                # 其他失敗（面板剛開沒好/槽忙/材料檢查沒過）→ 重開面板再試
                c["wnd"] = None
                self._note(f"⚠ 開製作批次失敗（{msg}）→ 重試", warn=True)
                return
            c["batch"] = {"added": added, "mk": mk, "left": added}
            c["sig"], c["last_drop"] = sig, now
            s["t0"] = c["t0"] = now                # 有進展 → 看門狗重新起算
            if added < qty:
                # ★ 遊戲排得比我們要的少（多半是負重／背包上限）——**大聲說**。
                #   安靜吞掉就是「工具說 9000、遊戲做 999」那種脫鉤。
                self._note(f"⚠ 要排 {qty} 個、遊戲只收了 {added} 個"
                           "（負重／背包上限？）—— 先做這批，做完再排下一批",
                           warn=True)
            else:
                self._note(self._craft_note(c, cur))
            return

        # ── 監看這一批：**以遊戲自己的製作清單為準** ─────────────────────
        # 清單那列「還剩幾個」做好一個 −1 —— 這才是遊戲的進度（吃了製作加倍、
        # 被打斷都反映在上面）；清單讀不到（面板狀態不明等）才退回數材料。
        b = c["batch"]
        left = (produce.make_left(self.sc, b.get("mk") or 0, cur.recipe.rid)
                if po is True else None)
        if left is not None:
            if left < b["left"]:                   # 遊戲做掉了幾個
                c["made"] += b["left"] - left
                b["left"] = left
                c["sig"], c["last_drop"] = sig, now
                if c["first"] is None:
                    c["first"] = now               # 從第一個做好才開始估時
                s["t0"] = c["t0"] = now            # 有進展 → 看門狗重新起算
                self._note(self._craft_note(c, cur))
                return
            if left > b["left"]:                   # 變多＝有人手動加排 → 照著追
                b["left"], c["last_drop"] = left, now
                return
            if left <= 0:
                # 清單裡沒有這個配方了＝這一批做完 → **馬上**開下一批，
                # 不必等 8 秒停滯判定（材料還有就續排，沒了換配方／收尾）。
                c["batch"], c["sig"] = None, None
                s["t0"] = c["t0"] = now
                return
        elif c["sig"] is not None and sig < c["sig"]:
            # 備援：材料變少＝真的在做。⚠ 清單讀得到時**不走這條** ——
            #   兩條都計數會把同一個算兩次。
            step = sum(num for _m, num in cur.recipe.mats) or 1
            n = max(1, (c["sig"] - sig) // step)
            c["made"] += n
            # ★ 清單模型也要跟著扣：不同步的話，等清單又讀得到時
            #   left < b["left"] 會把備援期間算過的又算一次（灌水）。
            b["left"] = max(0, b["left"] - n)
            c["sig"], c["last_drop"] = sig, now
            if c["first"] is None:
                c["first"] = now
            s["t0"] = c["t0"] = now
            self._note(self._craft_note(c, cur))
            return
        # 停了 BATCH_STALL_SECS 沒進度＝這批卡住（或清單讀不到又沒材料訊號）
        # → 收掉重算：材料還有 → 下一拍再開一批；材料用完 → 換配方；都做完
        # → dispose。（清單讀得到時「做完」走上面 left==0 那條，不會等到這裡。）
        if now - c["last_drop"] > BATCH_STALL_SECS:
            c["batch"], c["sig"] = None, None
            return

    @staticmethod
    def _craft_note(c: dict, cur) -> str:
        """製作中的狀態列：做什麼、第幾個／共幾個、大概還要多久。

        ⚠ 「還要多久」是**量出來的**（拿已完成幾個除以花掉的時間），不是猜的；
          量不到（還沒做完第二個）就老實說「估算中」，不要編一個數字。
        ★ 「這批遊戲還排著 N 個」是**遊戲清單上的數字**（make_left 讀的），
          不是我們預計的 —— 使用者要的就是看得到遊戲自己打算做幾個。
        """
        total = c["total"] or 0
        done = c["made"]
        head = f"做 {itemname.of(cur.recipe.product)}"
        # total 每一批會重算（可能變小），done 追過它時別顯示「第 1001/999 個」
        pos = (f"　第 {done + 1}/{total} 個" if total and done < total
               else f"　已完成 {done} 個")
        b = c.get("batch")
        if b and b.get("left"):
            pos += f"（這批遊戲還排著 {b['left']} 個）"
        eta = "　剩餘時間估算中…"
        if c["first"] is not None and done >= 2 and total > done:
            per = (time.monotonic() - c["first"]) / (done - 1)
            left_s = per * (total - done)
            if left_s >= 60:
                eta = (f"　一個約 {per:.1f} 秒、大約還要 "
                       f"{int(left_s // 60)} 分 {int(left_s % 60)} 秒")
            else:
                eta = f"　一個約 {per:.1f} 秒、大約還要 {left_s:.0f} 秒"
        elif total and done >= total:
            eta = ""
        return head + pos + eta

    # ------------------------------------------------------------------
    def _ensure_mover(self) -> bool:
        """需要叫遊戲函式時才裝跳板 —— 沒用到就不要在遊戲裡放程式碼。

        ⚠ 一定要走 `move.acquire()`：同一個遊戲行程只能有一份跳板，
          自己 new 一份會把別的分頁（掛機／能量晶化）正在用的那份拆掉。
        ★ 精靈設定現在幾乎都是純記憶體讀寫，跳板只有「記錄不存在要退回
          Lua」時才用得上；但那條路隨時可能被走到，所以先備好。
        """
        if self._mover is not None and self._mover.active:
            return True
        if self._mover_failed:
            self._halt(None, "⚠ 跳板裝不起來，這台沒辦法設定精靈")
            return False
        try:
            self._mover = move.acquire(
                self.pid, injector.process_path(self.pid), self)
            return True
        except Exception as exc:                   # noqa: BLE001
            self._mover = None
            self._mover_failed = True
            self._halt(None, f"⚠ 跳板裝不起來：{exc}")
            return False

    def _fail(self, what: str, exc: Exception, box: QCheckBox) -> None:
        path = crashlog.record(f"自動生產分頁 {what}（PID {self.pid}）", exc)
        self._halt(box, f"⚠ {what}時發生錯誤，已停止：{exc}"
                        + (f"　紀錄檔：{path}" if path else ""))

    def _halt(self, box: QCheckBox | None, msg: str) -> None:
        """大聲停用：勾勾放掉、狀態列寫紅字。"""
        for cb in ([box] if box is not None else [self.run_cb]):
            cb.blockSignals(True)                  # 免得又跑一次停止流程
            cb.setChecked(False)
            cb.blockSignals(False)
        self._note(msg, bad=True)

    def halt(self, msg: str) -> None:
        self._halt(None, msg)

    def _note(self, msg: str, warn: bool = False, bad: bool = False) -> None:
        self.status.setText(msg)
        self.status.setToolTip(msg)                # 太長被截掉時滑鼠看全文
        color = "#e06060" if bad else ("#e0b040" if warn else "#9aa2b8")
        self.status.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------
    # -- 定位點 ----------------------------------------------------------
    def _walk_to(self, gx: float, gy: float) -> str:
        """走去 (gx, gy)。回傳給狀態列看的說明；到不了時 `self._nav.stuck` 為 True。

        ★ 走地形圖算出來的最短路（`navigate.Navigator`），一個轉折點一個
          轉折點推進，**純讀記憶體、不問遊戲的尋路**。
        ⚠ 不要改回 `mover.walk_route()`：那是對目標本身尋路，遠距離會回
          0 個路徑點 → 一步都不走（就是「卡在還有 60 格」那個 bug）。
        """
        pf = move.pathfinder_this(self.sc)
        if not pf:
            return "讀不到角色"
        return self._nav.step(self.sc, self._mover, pf + 8, gx, gy)

    # ⚠⚠ 採集狀態中**用不了天使之翼**（2026-08-12 使用者實測：要先按一下
    #   ESC 把採集狀態取消掉）。所以回程前一定要先送 ESC，而且**隔一拍**
    #   再用道具 —— 同一拍送完就用，遊戲還沒退出採集狀態。
    #   ★ 送鍵走 win.send_key（它會按住 40ms；不按住只有 26.7% 會中，
    #     見 memory 的 key-send-hold）。背景視窗也吃得到，不必搶焦點。
    VK_ESCAPE = 0x1B

    def _escape_gather(self, s: dict) -> bool:
        """回程前先按 ESC 退出採集狀態。回傳 True＝這一拍先讓路，下一拍再回程。"""
        if s.get("esc"):
            return False
        s["esc"] = True
        try:
            win.send_key(self.hwnd, self.VK_ESCAPE)
        except Exception:                          # noqa: BLE001
            pass                                   # 送不出去就照原路試回程
        self._note("先按 ESC 退出採集狀態（不然用不了天使之翼）…")
        return True

    def _my_pos(self) -> tuple[float, float] | None:
        """玩家目前的格子座標；讀不到或**驗不過**回 None。

        ⚠⚠ 玩家物件**不快取、每次重算**：它會搬家（換地圖、傳送、重連），
          而舊位址照樣讀得到 —— 讀出來是別人的資料卻長得像一組合法座標，
          存成定位點、拿去當採集中心點就是「安靜地弄錯地方」。
          `player_pos()` 會順便比對物件開頭的 vtable（同一次讀取，免費）。
        """
        try:
            pf = move.pathfinder_this(self.sc)
        except Exception:                          # noqa: BLE001
            return None
        return entity.player_pos(self.sc, pf + 8) if pf else None

    def _add_spot(self) -> None:
        pos = self._my_pos()
        if pos is None:
            self._note("讀不到角色位置（還沒進到地圖？）—— 這次沒有記下來",
                       warn=True)
            return
        sid = scene.current_id(self.sc)
        self._spots.append((pos[0], pos[1], sid))
        self._refresh_spots()
        self._save_settings()
        where = scene.scene_name(sid) if sid is not None else "未知地圖"
        self._note(
            f"已加入定位點 {len(self._spots)}："
            f"{where} ({pos[0]:.0f}, {pos[1]:.0f})"
            + ("" if sid is not None else "　⚠ 讀不到地圖，這個點不做地圖比對"))

    def _remove_spots(self) -> None:
        for row in sorted((self.spot_list.row(i)
                           for i in self.spot_list.selectedItems()),
                          reverse=True):
            del self._spots[row]
        self._refresh_spots()
        self._save_settings()

    def _refresh_spots(self) -> None:
        self.spot_list.clear()
        for n, (x, y, sid) in enumerate(self._spots):
            where = scene.scene_name(sid) if sid is not None else "未標記"
            self.spot_list.addItem(f"{n + 1}. {where} ({x:.0f}, {y:.0f})")
            self.spot_list.item(n).setToolTip(
                f"{where}　格子 ({x:.1f}, {y:.1f})\n"
                + (f"場景編號 {sid}" if sid is not None else
                   "沒有記到地圖 —— 不會做地圖比對，想要的話刪掉重加。"))

    # ------------------------------------------------------------------
    # -- 設定 ------------------------------------------------------------
    def _key(self, field: str) -> str:
        return f"{SETTINGS_PREFIX}.{self.account}.{field}"

    def _load_settings(self) -> None:
        self._spots = []
        for p in config.get(self._key("spots"), []) or []:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue                           # 設定檔被改壞就跳過那一列
            sid = int(p[2]) if len(p) > 2 and p[2] is not None else None
            self._spots.append((float(p[0]), float(p[1]), sid))
        self._refresh_spots()
        for name in config.get(self._key("resources"), []) or []:
            self.picked.addItem(str(name))
        want = config.get(self._key("dispose"), DISPOSE_GUILD)
        want = DISPOSE_RETIRED.get(want, want)     # 已刪掉的選項要安全退化
        idx = self.dispose.findData(want)
        self.dispose.setCurrentIndex(idx if idx >= 0 else 0)
        b = config.get(self._key("bench"), None)
        if isinstance(b, (list, tuple)) and len(b) >= 2:   # 設定檔壞掉就當沒記
            # ⚠ 舊版設定只有三格（x, y, 場景）—— 沒有外觀編號就補 0，
            #   意思是「位置知道、長相不知道」，到現場照樣由近而遠點點看。
            self._bench = (float(b[0]), float(b[1]),
                           int(b[2]) if len(b) > 2 and b[2] is not None else None,
                           int(b[3]) if len(b) > 3 and b[3] is not None else 0)
        self._refresh_bench()

    def _save_settings(self) -> None:
        if self._loading:
            return
        config.set(self._key("spots"),
                   [[x, y, sid] for x, y, sid in self._spots])
        config.set(self._key("resources"), self.wanted())
        config.set(self._key("dispose"), self.dispose.currentData())
        config.set(self._key("bench"),
                   list(self._bench) if self._bench else None)
        config.save()

    # ------------------------------------------------------------------
    def close_page(self) -> None:
        """收拾。⚠ 跳板走 release 不要直接 stop：那是同一個 PID 共用的。"""
        self._timer.stop()
        self._trip_timer.stop()
        self._trip = None
        if self._mover is not None:
            try:
                move.release(self.pid, self)
            except Exception:                      # noqa: BLE001
                pass
            self._mover = None


class ProduceTab(BaseTab):
    """自動生產。"""

    TAB_TITLE = "自動生產"
    ORDER = 6                        # 緊接在自動掛機（5）後面

    def build_ui(self) -> None:
        self._pages: dict[int, CharProducePage] = {}
        self._scanners: list[MemoryScanner] = []

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.rescan_btn = QPushButton("重新偵測分身")
        # ★ 按鈕要 force_names=True：同一台登出換角色 pid 不變，
        #   不強制重掃的話分頁標籤永遠是舊角色名（見 app/core/preload.py）。
        self.rescan_btn.clicked.connect(
            lambda: self.reload_instances(force_names=True))
        bar.addWidget(self.rescan_btn)
        self.found = QLabel("尚未偵測")
        self.found.setStyleSheet("color: #9aa2b8;")
        bar.addWidget(self.found)
        # AOB 自動定位的結果。平常是空的；改版讓位址位移時要在這裡說出來。
        self.locate_lbl = QLabel("")
        bar.addWidget(self.locate_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._pages:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        """重建分身子分頁。

        force_names：只有按「重新偵測分身」才 True。自動載入走預讀快取，
        不然第一次切過來又要等好幾秒的全記憶體掃描（見 app/core/preload.py）。
        """
        self._teardown()
        wins = preload.windows()
        if not wins:
            self.found.setText("找不到分身")
            return
        for w in wins:
            acct = charname.account_from_title(w.title) or ""
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                      # noqa: BLE001
                continue
            # ★ 接上就用 AOB 掃一次，把寫死的遊戲位址換成當下正確的
            #   —— 遊戲改版會讓它們整批位移（見 app/game/locate.py）。
            #   只會真的做一次（五台載的是同一份 angel.dat）。
            try:
                locate.warm(sc)
            except Exception:                      # noqa: BLE001
                pass                               # 定位是加分項，不能擋住分頁
            self._scanners.append(sc)
            nm = preload.name_of(w.pid, sc, acct, force=force_names)
            page = CharProducePage(w.pid, w.hwnd, w.title, sc, acct, nm)
            self._pages[w.pid] = page
            self.tabs.addTab(page, nm)
        self._show_locate()
        self.found.setText(f"偵測到 {len(self._pages)} 個分身")

    def _show_locate(self) -> None:
        """AOB 定位失敗要**大聲**：不講的話使用者只會看到「怪怪的」。"""
        moved, failed = locate.moved(), locate.failed()
        text, color = "", ""
        if failed:
            text = (f"⚠ 有 {len(failed)} 個遊戲位址定位失敗（相關功能已停用）："
                    + "、".join(failed[:3]))
            color = "#e0b040"
        elif moved:
            text = f"偵測到遊戲改版，已自動重新定位 {len(moved)} 個位址"
            color = "#7fc97f"
        self.locate_lbl.setText(text)
        self.locate_lbl.setToolTip(text)
        self.locate_lbl.setStyleSheet(f"color: {color};" if color else "")

    # ------------------------------------------------------------------
    def _teardown(self) -> None:
        for page in self._pages.values():
            page.run_cb.setChecked(False)
            page.close_page()
        self.tabs.clear()
        self._pages = {}
        for sc in self._scanners:
            try:
                sc.close()
            except Exception:                      # noqa: BLE001
                pass
        self._scanners = []

    def on_close(self) -> None:
        self._teardown()
