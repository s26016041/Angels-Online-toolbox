"""自動掛機分頁：列出附近怪物，選一隻，然後持續送攻擊按鍵。

運作原理（見 app/game/entity.py 的說明）
----------------------------------------
遊戲攻擊時會**自己重讀**「客戶端目前選定的目標」那個欄位，所以只要：
    ① 把目標的實體 ID 寫進狀態物件 +0x2D8
    ② 送一個攻擊按鍵（預設 F2，介面上可改成 F1～F12）給該視窗
角色就會打那隻怪。不必注入會執行的程式碼、不必自己組封包、不必搶視窗焦點
（背景視窗吃得到鍵盤訊息，吃不到滑鼠點擊 —— 所以選目標非走記憶體不可）。

打哪一隻：**離玩家最近的那一隻**。實體串列沒有順序，直接拿第一隻常常是天邊那隻
——先前「有時打得到、有時打不到」就是這個原因。現在怪和玩家都讀得到座標，
排序取最近的即可；而且遊戲的攻擊指令內建自動接近，選最近的只是為了少走路。

介面
----
每個分身一個子分頁（標題就是角色名）。各自可以掃描周圍怪物、選要打哪幾種，
勾「開始掛機」才會開始，取消勾選就停。

三件事各跑各的執行緒，互不干擾（這個拆法是使用者提的，實測有效）
------------------------------------------------------------------
    ScanWorker    背景持續刷新怪物清單（熱區掃描，約 28ms）
    TargetWorker  50 Hz 把目標寫回遊戲，並偵測牠死了沒
    KeyWorker     依設定的頻率狂送技能鍵，什麼都不判斷

為什麼要拆：三件事的需求完全不同 —— 掃描量大但不急、寫入要夠即時、
送鍵只要準時。綁在一起的話任何一邊變慢都會拖到另外兩邊，
症狀就是「殺完一批怪會原地發呆」「施放速度偶爾卡卡的」。
"""
from __future__ import annotations

import ctypes
import math
import struct
import os
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from app.config import config
from app.core import charname, crashlog, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.core.notifier import Notifier
from app.game import (aob, attack, bag, balls, buff, castwatch, channel, entity,
                      inventory, itemname, jumpmap, locate, mall, monsters, move,
                      navigate, player, quickbar, recall, revive, robot, scene,
                      skillcost, skills, summon, supply,
                      tablestamp, terrain)
from app.tabs.base_tab import (BaseTab, ClientWatchMixin, fit_list, fit_spin,
                               no_elide)

# 設 AO_FARM_LOG=1 就會把每一秒的決策寫進 farm_debug_<帳號>.log。
# 平常是關的 —— 從外面看不到「為什麼不走」，只能靠這個。
_FARM_LOG = os.environ.get("AO_FARM_LOG") == "1"
SKILL_KEYS = [(f"F{i}", 0x6F + i) for i in range(1, 13)]   # F1=0x70 … F12=0x7B
DEFAULT_KEY = 0x71              # F2
# ★★ 出手間隔（**固定，不給使用者改**，使用者要求）。
# 為什麼是 0.1 秒：
#   · 攻擊技能一輪最快就是 0.5 秒（黑狐在用的技能 257：前置 0、後置 500ms；
#     全部法術裡「前置+後置」最多的一級就是 500ms，共 3223 個）。
#     再密也沒用，伺服器不收。
#   · 0.1 秒等於每個 0.5 秒的窗口試 5 下，封包排隊有抖動也不會錯過。
# ⛔ 不要再調到 0.02（黑狐原本的設定，每秒 50 次）：跳板的**指令槽只有一個**，
#    攻擊要跟移動、尋路搶，送太密會把移動指令卡在外面。
ATTACK_GAP = 0.1
DEFAULT_INTERVAL = ATTACK_GAP   # _Paced 的預設節奏
WRITE_INTERVAL = 0.02           # 秒；多久重寫一次目標＋檢查它死了沒（50 Hz）
SEND_TIMEOUT_MS = 60            # 送鍵最多等遊戲多久（正常 3.4ms，卡住就放棄這一拍）
TICK_MS = 10                    # UI 心跳
# 「立刻重掃」用的推值：把 _since_scan 推到比**任何**間隔都大，
# 下一拍心跳（10ms）就會送請求。⚠ 不繞過 `_waiting` 那個閂。
SCAN_NOW = 999.0
# 「沒人在看、也沒在掛機」那幾台的刷新間隔 —— 唯一的慢檔。
# ★★ 刷新節奏**只有兩檔**（使用者要求：不要分掛機／沒掛機，同一份掃描同一個
#   節奏）。掃描本來就只有一份：ScanWorker 掃一次，「挑目標」跟「周圍怪物」
#   清單共用同一個結果，這裡從頭到尾只是在決定「多久要一次」。
#     有人在看這一頁，或這台正在掛機 → REFRESH_GAP（0.15 秒）
#     兩者都不是                     → 這個值
# ⚠ 那一檔慢的**不是為了省效能，是因為刷了沒用**：沒人看畫面、也不必挑目標。
#   但它確實也保護了掃描執行緒 —— 只有一條執行緒服務所有分身，一次熱區掃描
#   約 30ms，0.15 秒一次等於一台吃掉它的 20%。五台全開快檔就滿載，排隊反而
#   讓實際刷新拖到 0.2~0.3 秒，還會拖慢掛機中那幾台挑目標。
IDLE_SCAN_GAP = 1.5
# 掛機時多久刷新一次怪物清單。熱區掃描實測約 28ms，所以可以一直刷。
# ★ 必須「一直刷」而不是「沒怪才刷」：跟別人搶怪時，清單一過期就會去打
#   別人已經殺掉的、或錯過剛生出來的那隻。
# 掛機中、已經有目標時的刷新間隔。熱區掃描實測 34~35ms，所以可以很密。
# ⚠ 刷新變快**不會讓屍體消失** —— 牠們是真的還在遊戲的實體清單裡
#   （中位賴 5 秒、最久 79.8 秒）。屍體是靠 Entity.dead 濾掉的，不是靠刷新。
#   加快只對「早一點看到新生出來的怪」有幫助，也就是搶怪。
# 0.3 → 0.15（使用者要求，為了搶贏別人）：掃一次 35ms，等於那條執行緒
# 約 23% 的時間在掃，它跑在 LowPriority 不會影響畫面。
REFRESH_GAP = 0.15
FULL_EVERY = 30.0               # 多久強制做一次全記憶體掃描當保險
# ★ 「補救全掃」的節流。這是**保險**，不是常態修補 —— 唯讀實測
#   （5 台 × 300 秒，v2/v3 兩輪）：活怪從清單消失幾乎都是遊戲自己回收
#   物件（視野剔除，523 件裡只有 1 件是掃描端漏讀），而新出現的怪 0 件
#   落在熱區外（新物件會重用既有的堆積區塊，熱掃當拍就看得到）。
#   所以補救全掃只在三種「事情不對勁」時要求：目標掃不到但物件還在、
#   整批活怪消失但物件還在、掛機中一直挑不到目標 —— 前兩種罕見，
#   第三種是等重生時順便當保險。全掃一次 0.35 秒、五台共用一條掃描
#   執行緒，同一台最快隔這麼久才准再要求一次。
FULL_HUNT_GAP = 3.0
# ★★★ 掃描看門狗：送出掃描請求之後這麼久還沒拿到結果，就當那一次請求掉了。
#   ⚠⚠ 這是「掃周圍怪物突然壞掉、然後一直找不到怪物」的**根治**：
#     `_waiting` 是個閂 —— 送出請求時舉起、拿到結果時放下。只要有一次沒放下
#     （套用結果的途中丟例外被 Qt 訊號槽默默吞掉、工作執行緒剛好不在、
#     請求被丟掉…），那台分身就**再也不會發出下一次掃描請求**，
#     怪物清單從此定格在那一拍，重開分頁才會好。
#   ★ 有了這個，任何原因造成的「掃描停了」最多卡這麼久就自己回來，
#     而且會在狀態列說一聲（不准安靜地壞著）。
#   ⚠ 要**明顯大於最壞情況的排隊時間**：全掃一台 0.35 秒，五台排隊約 2 秒，
#     再讓爛電腦有幾倍餘裕。誤觸發的代價只是多要一次全掃，但別讓它常發生。
SCAN_STUCK_SECS = 8.0
# 掃描結果不可信時（掃描失敗／狀態物件掃到 0 個或多個），最多連續幾拍沿用
# 上一拍的怪物清單 —— 「這一拍掃壞了」≠「周圍沒有怪」。見 apply_scan。
SCAN_KEEP_BAD = 10
# ★★ 多久確認一次「這個遊戲視窗還在不在」（見 _check_game_gone）。
#   遊戲被關掉之後我們的控制代碼**還是有效的**，記憶體讀取只會安靜地回 None
#   ——看起來就跟「這一拍沒掃到怪」一樣，掛機會對著一個不存在的行程一直跑。
#   一次查詢只花幾微秒（GetExitCodeProcess），一秒一次完全沒有負擔。
GAME_GONE_POLL = 1.0
INV_RELOCATE_GAP = 8.0          # 找不到物品陣列表頭時，多久才重試（要跑 AOB 全掃）
HP_CHECK_GAP = 0.5              # 多久確認一次自己還活著（死了就自動停）
GEAR_CHECK_GAP = 3.0            # 多久看一次裝備耐久（掉得很慢，不必常看）
# ★★ 官方精靈「自動攻擊」的看門狗間隔（見 CharFarmPage._af_tick）。
#   它一開著，精靈就會自己挑怪打，而且**完全不看我們的「選中怪物」名單**。
#   純記憶體讀（紅黑樹 0.3ms 級），是關的就什麼都不做。
AF_WATCH_GAP = 3.0
# ★「自動練技」的心跳間隔（見 CharFarmPage._train_tick）：開關收斂＋藥水
#   見底檢查共用一個計時。開關那半是純記憶體讀（是開的就什麼都不做）；
#   藥水那半跟 GEAR_CHECK_GAP 的補給判斷同一套成本。
TRAIN_GAP = 3.0
# 練習技能的記錄還沒建（角色從沒在遊戲裡勾過那個框）時才需要退 Lua 讓遊戲
# 自己建 —— Lua 呼叫是全專案風險最高的動作（會動遊戲的 Lua 堆疊），
# 看門狗絕不能每 3 秒撞一次，失敗至少隔這麼久才再試。
TRAIN_LUA_GAP = 30.0
# ★「自動換球」的心跳間隔（見 CharFarmPage._ball_tick）。球從空到滿要好幾小時，
#   「滿了晚 8 秒才換」沒有任何損失（滿了就是不再累積，不會溢出漏算），
#   所以刻意放慢 —— 它每一拍都要把飾品欄兩格＋整個背包讀一遍。
BALL_GAP = 8.0
# 「購買紀錄」在記憶體裡最多留幾筆（跟自動回連的事件歷史同一套：session 狀態，
# 工具箱重開清空）。一趟補給通常 2~5 筆，500 筆夠看好幾天。
PURCHASE_CAP = 500
# ★★ 「背包裡找不到回程道具」要**連續確認幾次**才准停機（每次間隔就是
#   GEAR_CHECK_GAP）。⚠ 不可以改回「第一次就停機」：換頻道／傳送後重連時，
#   容器與內容是**分批到齊**的 —— 使用者 2026-08-09 回報「裝備壞掉卻說我
#   沒有回程道具，但一定有」，而且當下剛換過地圖。那一拍身上穿的裝備已經
#   讀得到（所以觸發了「裝備損壞」），整條容器卻還沒填完。
#   `bag.synced()` 擋得掉「整條都還是空的」，但擋不掉「前面到了、後面沒到」。
#   結構上分不出來的事就用時間分：空窗只有零點幾秒到幾秒。
NO_RECALL_TRIES = 3
# ★ 交棒給天使精靈跑補給：多久看一次「回到原地圖了沒」、最多等多久就放棄。
#   一趟補給要回城、找 NPC、修裝、買東西、再走回來，慢的時候好幾分鐘。
SUPPLY_POLL = 5.0               # 使用者指定的間隔
SUPPLY_MAX_SECS = 600.0
# ★ 回程第二段（用道具）暫時送不出去時的重試（跳板重掛／背包表頭剛搬家，
#   InvWorker 幾秒內就會把表頭找回來：AOB 全掃約 2 秒、失效後最多隔
#   INV_RELOCATE_GAP 秒重試）。以前不重試，直接放著吊到 10 分鐘超時。
RECALL_RETRY_SECS = 4.0
RECALL_RETRIES = 4
# ★★ 趴趴GO回程：**封包送出去 ≠ 人到得了**（jumpmap.teleport 的說明就寫了
#   「到不到得看伺服器」）。所以整段是「時間到送一次 → 盯著地圖有沒有真的變
#   → 沒變就再送」，由 _supply_tick 的輪詢驅動（見 _jump_step）。
#   ⚠⚠ 2026-08-09 使用者回報「卡在傳送中」的根因就是舊版**射後不理**：
#     一次性計時器送完一包就再也沒有人確認，那一包沒生效就永遠回不去。
#   ⚠ 不設重試上限（使用者要求：暫時性失敗一律自動重試）；出口是補給總逾時
#     SUPPLY_MAX_SECS 那一聲「已停止掛機」＋通知，不會安靜地卡著。
JUMP_LAND_SECS = 20.0           # 送出後等這麼久還沒到，就當那一包沒生效 → 再送
JUMP_BACK_SECS = 120.0          # ★「用天使趴趴GO回地圖」等多久才傳（使用者指定 2 分鐘，2026-08-12 從 3 分鐘改）
# ★「死亡自己回練功區」：死了倒數幾秒後送「回標記點」封包復活（使用者指定
#   3 秒；等死亡視窗出來就好，不再靠精靈復活＋趴趴GO傳送那條舊路）。
DEATH_REVIVE_SECS = 3.0
DEATH_POLL = 1.0                # 死亡回程模式下多久檢查一次（復活了沒）
# 送了「回標記點」還沒活過來時，隔多久再送一次（暫時性失敗自動重試 ——
# 封包偶爾會排不進去或掉掉，不准叫使用者「再按一次」）。
REVIVE_RETRY = 5.0
# 復活整趟最多等多久。超過就當流程斷了（標記點沒設？封包沒生效？）：
# 停機並通知 —— 使用者是掛網離開的，要叫得動他。
DEATH_WAIT_MAX = 90.0
# 多久可以重下一次移動指令。★ 單次指令只走得到約 15 格（見 app/game/move.py
# 的 MAX_HOP），長距離是靠這裡定期重下、一段一段接力走完的，所以不能太久。
# ★ 下一次移動指令最少隔多久。**而且角色還在走時一律不下**（見 tick）。
# ⚠⚠ 這是「走不回原點」的真正原因：以前每 0.8 秒就重下一次指令，
#   把還沒走完的**多點路徑砍掉**。那條路徑常常是「先繞開再往前」，
#   我們正好在繞開那一段打斷它 → 回到原處又重算 → 無限來回，
#   看起來就像根本沒在算路徑。實測不打斷之後，176 格外的原點 42 秒就走回去了。
WALK_GAP = 0.4
# ★★ **趕路時**的指令冷卻。角色一停下來（動畫狀態變 'Wait'）就馬上下一段，
#   長距離才不會一頓一頓 —— 每段之間空等 0.5 秒，走 5 段就多花 2.5 秒。
# ⚠⚠ 這個值**一定要大於「指令排隊到真的執行」的時間**（實測 107~154ms）。
#   設 0.12 反而更糟：角色還沒開始動、狀態還是 'Wait'，我們就又送一次，
#   把上一個指令重置掉 —— 一路互相打斷，實測卡頓變成 0.47~0.51 秒。
#   0.3 秒留了兩倍餘裕；正常情況根本用不到（停下來的那一拍就直接送了），
#   它只是「送出去卻沒生效」時的重試間隔。
WALK_GAP_FAR = 0.30
# 離目標多遠算「還在趕路」。比這個近就是打鬥中的微調，維持原本 0.4 秒的節奏
# —— 遊戲自己打怪時也是 1 秒 1 包，微調不需要更密（密了反而搶指令槽）。
FAR_ENOUGH = 6.0
# 判斷「有沒有在移動」要隔一段時間再比位置。
# ⚠ 心跳是 10ms，而角色約 9 格/秒 = 每拍才 0.09 格 —— 拿相鄰兩拍比
#   幾乎永遠判定「沒在動」（卡住偵測也一起誤判，走路中照樣累積秒數）。
# 走路的到達容差：距離只超過目標一點點就不要再走了。
# 沒有它的話角色會在定位附近一直被推一小步（「打一打又往前一格」）。
#
# ⚠⚠ 2026-08-08 從 1.5 降到 1.0（實機錄影定案，見 [[ranged-dead-band]]）。
#   舊值讓遠程角色**允許自己待在 11.5 格不動**（keep 10 + 容差 1.5），
#   但實測「真正打得出傷害」的距離只到約 11.0 格：
#       黑狐 139 次掉血中，>11.0 格只剩 4.3%、>11.5 格是 **0%**
#   於是 (11.0, 11.5] 是死區 —— **既不會走近（沒超過 11.5）、也打不到**。
#   挑中的怪一開始就站在那半格裡時（牠站在出生點沒被激怒、座標整格不動），
#   角色一步都不會動，4 秒後被零傷害快篩放棄還冰 20 秒。
#   實錄：黑狐 198.2~202.2s 與 216.2~220.2s，距離 11.37 / 11.39 靜止 3.9 秒，
#   期間怪的位移是 **0.000 格**；對照組 14.29 格的怪會走近到 10.62 立刻掉血。
# ⛔ 「怪只要有移動就重新接近」已評估否決：那五段卡住裡怪的位移全是 0（規則
#   不會觸發），而射程內有 33~43% 的時間怪在移動（規則會一直觸發），
#   又牴觸「一隻怪只送一次移動指令」那條硬規則。
# ★ 只影響遠程：近戰的 reach 只有 2~3，`reach-0.5-keep` 本來就 <0.3，
#   下面那道 max(0.3, …) 會蓋過來，容差維持 0.3 不變（實測雪狐／北極狐）。
# ⚠ 代價（錄影實算）：起步走路的次數會變多一點 ——
#   黑狐每分鐘 +1.2 次（+5.6%）、嵐狐 +0.4 次（+1.4%）、白狐 +0.2 次（+1.1%）。
WALK_SLACK = 1.0
# ⛔ 舊的「隔 MOVE_SAMPLE 秒比一次位置」已經拿掉 —— 改讀遊戲的動畫狀態
#    （entity.is_walking）。比位置最久要 0.3 秒才知道走完了，那 0.3 秒
#    加上指令冷卻就是使用者說的「長距離卡卡的」。
KILL_MEMORY = 60.0              # 打死的實體 ID 記多久（避免又挑到同一具屍體）
# 「一直沒給血量」而跳過的冷卻。那只是推測，牠可能還活著，所以比擊殺短。
# ⚠ 不能設 0：挑目標永遠挑最近的，冷卻 0 的話下一拍又挑到同一具，
#   變成無限迴圈。
# ⚠ 也不能太短：實測搶怪區 90 秒有 41 隻「沒給血量」的實體，其中 12 隻被
#   反覆挑到（從第一次到最後一次**中位相隔 13 秒**、最久 34 秒）——
#   冷卻 5 秒時 64 段裡有 23 段是重複挑同一具屍體。20 秒才擋得住。
NOHP_MEMORY = 20.0
# 三個清單的**最小**高度。★ 只是下限 —— 有空間時清單會自己長高填滿，
#   實際大多顯示 200px 以上。
# ⚠ 不能設太大：主視窗固定 940x700，最小高度加起來超過可視區的話，
#   整頁就會多一條垂直捲軸（130 時實測需要 599px、只有 556px 可用）。
NEAR_HEIGHT = 84
# 清單的上限。⚠ 少了它清單會一路長高，把下面的「手動輸入」欄和
#   「加入位置」按鈕擠出可視區（要捲才看得到）——
#   主視窗是固定 940x700，整頁高度就這麼多。
NEAR_MAX = 108
STUCK_SECS = 10.0               # 沒掉血、玩家也沒前進這麼久 → 這隻走不過去，換一隻
# ★★ **打得到的時候**要等多久才放棄（見 tick 裡的說明）。
#   等級差得多時命中率低、一隻要打十幾秒 —— 實測擊殺耗時 9.7/9.8/10.8 秒，
#   用 10 秒等於在快贏的前一刻放棄。45 秒足夠打完一隻，真的完全打不中
#   （例如等級差太多）也不會卡太久。
#   ⚠ 45 秒是「以為一隻要打十幾秒」時定的；改用快捷鍵出手之後實測
#     **每隻 3.2 秒**，45 秒等於一有狀況就發呆 45 秒。15 秒已是擊殺耗時的
#     四倍餘裕。
STUCK_ENGAGED = 15.0
# ★★ 卡住偵測用「離錨點的**淨位移**」，不是「每 0.3 秒有沒有動」。
#   撞牆時角色會小幅抖動（實測約 0.5~0.6 格），剛好跨過舊的 MOVE_EPS(0.5)
#   門檻，於是每一拍都被當成「正在走路」，_stuck 永遠被歸零 ——
#   唯讀監控實拍**卡了 32 秒**都沒觸發（STUCK_SECS 是 10 秒）。
#   只要人還在這個半徑裡打轉，就一律算沒有前進。
# ⚠ 2.0 太小：實拍到角色在 (88.5,40.5) 與 (90.5,42.5) 之間來回，
#   相距 2.8 格，錨點一直被重設，卡了 35 秒還是沒觸發。
STUCK_EPS = 4.0
# ⛔ 「4 秒零傷害快篩」**2026-08-10 整段刪掉**（使用者要求）。
#   它的條件是「打得到（dist <= reach）＋ 選定已送出 ＋ 沒掉過血 ＋ 超過 3 格」，
#   完全沒看「我是不是正在走過去」—— 而 reach 在交棒／遠程時是 **12 格**，
#   所以從 12 格外一路走近的整段路上計時器都在跑。繞一下路就超過 4 秒，
#   一隻**其實走得到**的怪就被丟掉還冷凍 20 秒（使用者回報：走到一半就放棄）。
#   它原本要抓的「站定了卻被地形擋線」由 STUCK_ENGAGED(15 秒) 接手 ——
#   那條用「離錨點的淨位移」判斷有沒有前進，走路中會自己歸零，不會誤觸。
#   ⚠ 別把 15 秒那條也拿掉：「怪一直刷新去打最近的」救不了擋線呆站，
#     因為那時候**卡住我們的那隻自己就是最近的**，永遠排第一。
# ★★ 「站定了卻零傷害」的正解（2026-08-19 使用者指定）：怪在障礙物對面、
#   數字上在攻擊範圍內 → 要**繞過障礙物過去打**，不是隔著障礙物空放到
#   15 秒換怪。跟被刪的 4 秒快篩差在**只在站定時計時** —— 直接沿用
#   _stuck（離錨點的淨位移，走路中自己歸零），繞遠路、趕路中都不會誤觸；
#   貼身 ≤3 格的空揮也照舊排除（NO_PATH_NEED）。觸發後**不換怪**：
#   keep 壓到 MELEE_RANGE 貼身走過去，邊走邊照打，擋線一消失傷害就進來、
#   一掉血就解除。真的走不進去仍由 STUCK_ENGAGED(15 秒) 收尾換怪。
PUSH_IN_SECS = 3.0
# ★★ 「趕路途中冒出更近的怪就改打牠」（使用者要求 2026-08-10）。
#   周圍怪物本來就每 0.15 秒刷新一次（見 [[farm-scan-refresh-tiers]]），
#   所以不必另外掃描，拿現成的清單照同一套規則重排一次就好。
# ⚠⚠ **打傷過的怪絕不換**（使用者明確指定：打到之後一定要確定打死才能換）。
#   換掉等於留一隻結仇的怪在後面追著咬，以前就是這樣被圍毆致死的。
SWITCH_GAIN = 3.0     # 新目標要近這麼多格才值得換（少了這道會兩隻互相取代乒乓）
SWITCH_GAP = 1.0      # 多久評估一次（每 0.1 秒重挑一次純粹是浪費）
# ★★★ 「近」一律指**我們自己 A* 算出來的路徑長度**，不是直線距離（使用者指定）。
#   直線近但要繞一大圈的怪不算近 —— 河對岸、牆後面那種就是這樣騙人的。
# ⚠ 路徑長度用「幾何成本」（直走 1、斜走 √2），跟直線距離同單位才比得起來，
#   而且**直線距離永遠 ≤ 路徑長度**：這條下限讓我們可以照直線排序後提早收手，
#   一次評估通常只要算兩三次 A*（每次 0.2~0.8ms），不必每隻都算。
# ⛔ PATH_COST_CAP（繞超過直線距離 3 倍就當走不到）2026-08-10 刪除。
#   它讓「牆對面、要繞遠路」的怪整批算不出路徑，於是挑目標退回直線最近、
#   走路退回遊戲的尋路 —— 就是使用者回報的「卡在牆邊直到怪重生」。
#   繞得遠不等於到不了；真的到不了由連通區泛洪（Grid.reachable）回答。
_SQRT2 = math.sqrt(2.0)
# ★★★ 「正在打我」的聯集判定（2026-08-07 唯讀跟拍實錘，見 _fighting_me）：
#   交戰槽在怪**出手的當下**反而是空的（17/17），單靠 entity.attacking()
#   會漏掉正在咬人的怪 —— 解禁／坐下／收尾三道保險全部失靈，這就是
#   「被選中的怪咬死、水還有」的根因。所以再聯集「動畫是攻擊中＋離我很近」。
FOE_NEAR = 3.0
# ★★ 不靠任何欄位的硬保險：**自己的 HP 在掉＝一定有怪在打我**。
#   最近這麼久內掉過血就當作交戰中（讀 HP 每 0.5 秒一拍，怪的攻擊間隔
#   約 2 秒，3 秒能跨過兩次攻擊的空檔不誤斷）。
UNDER_ATTACK_SECS = 3.0
# HP 在掉的期間，這個距離內的「冷卻中活怪」全部解禁 —— 咬人的怪常常就是
# 先前被記成「走不到」冰起來的那隻（貼身滿血也可能被冰，見零傷害快篩）。
UNFREEZE_NEAR = 4.0
# ★★ 「走不到」的冷卻，**會遞增**。有兩種完全不同的走不到，一個數字擋不住：
#
#   · 暫時的 —— 角色站的地方剛好算不出那個方向。換個位置就好了。
#     用 60 秒（跟確定打死同長）的後果實拍到了：近處 12.6 / 13.0 格的怪
#     全部被自己冷凍，只剩 29.4 格外的可挑，追過去又失敗，越陷越深。
#
#   · 永久的 —— **那隻怪就站在尋路走不進去的格子上**。實測（黑狐）：
#     同一個位置對周圍每隻怪各問一次尋路，
#         曼陀羅怪菇 ×9（11.8~28.3 格）全部回 0
#         夜香食人花 ×4（12.8~28.6 格）全部回 1（直線可通）
#     跟距離無關，就是那一種怪站在走不到的地形上。
#     這種用 8 秒的話每 8 秒就白試一次。
#
#   所以：第一次 8 秒，同一隻再失敗就翻倍，上限 UNREACH_MAX。
#   暫時性的很快恢復，永久走不到的自動淡出。
UNREACH_MEMORY = 8.0
UNREACH_MAX = 120.0
# ⛔ 曾經有「正在打我的優先」（FOE_RANGE），已經拿掉 —— 見 _pick_next()。
# ⛔ 不要做「打不到就一步一步走近」那種自動收斂 —— 使用者明講看起來卡卡的，
#    而且根本不需要：實測（黑狐，目標有寫進記憶體）12.2 / 11.6 / 8.9 格
#    都打得死（血量 100 → 47 → 0）。曾經以為 13 格打不到，那是診斷時
#    **忘了寫目標**、讀到的血量根本不是那隻怪，量錯了。
#    另外目標血量欄位會被遊戲清成 0，拿它當「沒打到」的訊號一定誤判。

# ★ 攻擊封包真正有效的距離。遊戲的固定值，不是角色屬性（使用者指出的）。
#
# ⚠⚠ 曾經設成 15.0，那是**錯的**，而且錯得很難看出來：
#   15.7 是「客戶端在多遠會**送出**攻擊封包」量到的最大值 —— 但客戶端送完
#   會自己走過去，**伺服器並不接受那麼遠的施放**。我們自己送封包沒有那段
#   走過去的行為，於是角色就停在「剛好打不到」的地方站著。
#
#   唯讀觀察正在掛機的黑狐 90 秒（完全沒干擾）：
#     13.0 / 13.2 / 13.4 / 13.6 / 13.9 / 14.3 / 14.5 格 → 血量 100 動都不動，
#       8 次發呆全部落在這裡，每次都要等卡住偵測 10 秒才換怪，90 秒只殺 1 隻
#     12.2 / 11.6 / 8.9 / 8.3 / 1.2 格 → 正常掉血、正常擊殺
#   → 真正的界線在 12.2 與 13.0 之間，取 12.0 留餘裕。
ATTACK_PACKET_RANGE = 12.0
# ⛔ WALK_KEEP（停在幾格）已移除：停多遠現在是 `reach - 1` 當場算的，
#    而 reach 來自**技能射程**（skills.range_of）。介面上的「接戰距離」
#    輸入框也一併拿掉了 —— 使用者填的值只會是地雷（雪狐把它留在遠程的
#    預設 11，近戰射程只有 1，於是站 11 格外空打）。
#    留 1 格餘裕的理由不變：怪會動，停在射程邊緣的話牠一走就出界要重走。
# 血量要**連續**讀到 0 這麼久才算死亡。偶爾讀到一次 0 不算
# —— 那會把還活著的怪丟掉。
HP_SETTLE = 0.5
# 鎖定這麼久還**從來沒**看到血量 → 那是屍體（別人先打死的），換一隻。
# ⚠ 門檻是量出來的：活著的目標鎖定後看到血量，中位 0.30 秒、最久 2.22 秒
#   （34 隻樣本），所以 3 秒不會誤殺活的怪。
#   使用者先要求 0.5 秒，實測誤跳過 5% 的活怪、而且「常常真的有那隻怪」，
#   所以放寬到 0.8 秒（活著的目標實測最久 0.52 秒就會顯示血量）。
#   被誤跳過的只冷卻 NOHP_MEMORY 秒，很快會再輪到。
CORPSE_SECS = 0.8
# ★★ 射程其實**每個角色不一樣**（近戰 vs 遠程），上面那個 12 是在黑狐量的。
#   雪狐是近戰：牠自己打怪時會送一堆移動封包（使用者攔到 6 包），
#   靠客戶端走到怪身上才打得到。我們停在 10 格送施放，伺服器完全不理
#   —— 症狀就是「雪狐完全無法打怪」。
#   解法不寫死也不量測：**還沒打中過就一路走到貼臉**，
#   一旦看到怪掉血就把那個距離記起來，之後就不必再靠近。
#   近戰會收斂到 ~2 格、遠程第一隻之後就回到 10 格，兩邊都對。
# 學技能 ID：送一次鍵、隔 LEARN_GAP 讀一次，正常一次就拿到。
# 只有「這個角色登入後還沒放過任何技能」（欄位是 0）才會多試幾次。
LEARN_GAP = 0.25                # 按鍵到遊戲寫入要隔一幀，讀太密只是白讀
# 掛機中多久重讀一次快捷欄（quickbar.Reader）——使用者中途換鍵上的技能，
# 最慢這麼久就跟上。純讀零副作用，一次只有幾個小讀取（頁碼節點有快取）。
QB_REFRESH = 2.0
# ★★ 一輪之間隔多久（使用者指定 **0.1 秒**）。
#   一輪 = 勾選的技能鍵**依序各放一次**（F1→F12），放完隔 ROUND_GAP 再下一輪。
#   一輪之內是逐一等它做完再放下一招（跳板只有一個指令槽，連丟會被擋掉），
#   所以一輪本身要花「招數 × 一幀(約 16ms)」。
#   遊戲自己有冷卻，還沒好的那一招送出去也只是不生效，不會出事。
# ⚠ 執行緒節拍要比這個細，否則實際間隔會被量化成節拍的倍數。
ROUND_GAP = 0.10
STRIKE_TICK = 0.025             # KeyWorker 的節拍：要能切出 0.1 秒
# 射程多少以內用「叫遊戲的快捷鍵」，超過就送帶 ID＋座標的施放封包（使用者定的）。
QUICKKEY_RANGE = 8
# ★★ 走快捷鍵那條路時，我們只走到這個距離就停手，剩下讓**遊戲自己走過去**
#   （實測：站 9.8 格只呼叫快捷鍵，角色自走 8.5 格貼到 1.4 格並擊殺）。
#   取 12 = 我們的「打得到」判定門檻，配上 margin 2 格 → 實際停在 10 格
#  （2026-08-07 試過 9，同晚使用者又改回 10）。
HANDOFF_RANGE = 12.0
# 交棒之後這麼久還沒真的接戰（還在技能射程外、也沒掉過血）就收回來自己走。
HANDOFF_WAIT = 3.0
# 攻擊方式。⛔ 以前還有一個 MODE_KEY（「自動掛機（按鍵）」那個分頁），
#   分頁已經拿掉了，常數也跟著移除 —— 現在只剩封包這一種。
#   （KeyWorker 裡「不是封包模式就送鍵」那條路還在，當作封包送不出去時的退路。）
MODE_PACKET = "packet"
# ⛔ 不要用「補按技能鍵」來讓角色接近（試過，使用者實測還是會卡住）。
#    走過去打是客戶端的行為，但補按鍵會讓客戶端和我們的移動指令互相打架，
#    角色反而鎖著遠處的怪站著不動。接近一律用我們自己的尋路（見下面的 tick）。
# ★★ 比這個近就**不算走不到**（放棄與零傷害貼身繞打都拿它當排除門檻）。
#   「貼在身上的怪」當年連尋路都跳過：**遊戲的**尋路對貼身目標一定回 0，
#   0 又被當「走不過去」，近戰每打一隻都貼上去 → 每隻都被冷凍（實拍：
#   周圍 1.2 格有怪，卻鎖著 16.0 格外的那隻）。2026-08-19 起貼身改問
#   **我們自己的地形圖**（它對相鄰格照樣算得出路，起終點同格回單點路徑，
#   不存在「回 0 被當走不到」的問題），才抓得到「怪在薄牆對面 2~3 格」
#   要繞過去的情況；「貼身不算走不到」這半條保留。
#   取 3.0：實拍貼身距離落在 1.0~2.1 格。
NO_PATH_NEED = 3.0
# ★★ 比這個近就**不尋路**，直接朝目標走（見 _walk_toward 的說明）。
#   尋路到貼身的目標一定回 0，於是「只差 0.2 格」也走不過去 ——
#   實拍雪狐站 2.2 格卡了 8.2 秒。取 4.0：比最大的近戰接戰距離
#   （射程 2 → 停 2.0、超過 3.0 才走）再多一格餘裕。
NEAR_WALK = 4.0
# ★ 近戰模式：走到 2 格以內才送攻擊封包（使用者指定）。
#   遠程角色維持原本的判斷（攻擊距離 12、走到 10 格內）。
#   為什麼要分：射程是每個角色不一樣的，近戰在 10 格外送施放伺服器不理
#   —— 實測雪狐就是這樣完全打不到怪。
MELEE_RANGE = 2.0
# ★ 自動分身按哪個鍵（使用者指定 F12）。技能編號不寫死 ——
#   按一次讀「角色屬性 −0x50」就知道，再去 skills.py 查持續時間。
BUFF_KEY = 0x7B                 # VK_F12
SPOT_SLACK = 3.0                # 走到離巡邏點這麼近就算到了，換下一個
# 多久讀一次「目前在哪張地圖」。換圖是很少發生的事，而心跳是 10ms 一拍，
# 每拍都讀等於白花 CPU（雖然一次只要 1.5ms）。
SCENE_SAMPLE = 0.5
# 靜態指標失效時，「全掃找場景物件」最快隔多久才准再做一次。
# ⚠ 那一掃 0.25 秒、跑在 GUI 執行緒上，沒有冷卻的話換地圖／重連期間
#   會每 SCENE_SAMPLE 掃一次，畫面直接卡住。跟 watcher 的重新定位同一個值。
SCENE_RELOCATE_GAP = 5.0


def _mmss(seconds: float) -> str:
    """剩餘時間，精確到秒（使用者要求）。一分鐘以內就只講秒。"""
    s = max(0, int(seconds))
    return f"{s // 60} 分 {s % 60:02d} 秒" if s >= 60 else f"{s} 秒"
# 自動巡迴換頻道：換完之後要停多久才恢復打怪。
# 客戶端重連實測約 1 秒，但重連後所有物件都會搬家，還要等掃描重新定位到，
# 所以留寬一點。這段期間不打怪、不下移動指令。
ROT_SETTLE = 5.0
# ⛔ 這裡以前是「血/魔不足時坐下回復」的一整組常數（VK_INSERT／REST_FULL／
#   REST_HP_DEFAULT／REST_MP_DEFAULT／SIT_GAP／SIT_CONFIRM／REST_SAMPLE）。
#   整個功能 2026-08-09 依使用者要求移除，理由與替代路徑見建構式裡那段說明。
PATH_GAP = 0.2                  # 重算「跟目標之間有沒有地形」的最短間隔
# ★ 規劃路徑最多只准佔這麼一小部分的時間（1/20 = 5%）：算完之後休息
#   「這次花掉的時間 × PATH_BUDGET」再算下一次。近距離的 A* 是 0.1~0.8ms，
#   乘 20 也還在 PATH_GAP 之內＝節奏完全不變；只有極遠的目標
#   （實測整張圖最遠 337 格的路要 48.6ms）才會自動放慢。
PATH_BUDGET = 20.0
PATH_GAP_MAX = 1.0              # 再怎麼貴也至少這麼久重算一次
# ⛔ PATHFIND_RANGE（25 格）2026-08-10 刪除：那是**遊戲的尋路**一次算得出的
#   範圍，用來把它的「回 0」翻譯成「太遠要接力」而不是「到不了」。
#   現在判定走不走得到的是我們自己的 A*（整張圖、沒有距離上限），
#   算不出來就是真的到不了，再拿距離去擋只會讓遠處走不到的怪永遠不被放棄。
# 要連續這麼多次「算不出路徑」才判定走不到。怪會走動，單一次很可能只是
# 牠剛好站到走不進去的格子 —— 每次都信就會變成「打一下就換下一隻」。
UNREACH_HITS = 3
# 目標要連續這麼多次掃不到才當作牠死了／離開視野。
# 熱區掃描偶爾會漏，單次就放棄會在打鬥中間換目標。
GONE_SCANS = 2


class _NoteLabel(QLabel):
    """狀態提示字專用的標籤：**永遠不會把版面撐開**。

    ⚠⚠ 為什麼要專門做一個（2026-08-08，使用者回報「跑版」）：
      主視窗固定 940x700、上半部是兩欄格線，每個方框的寬度是照內容算的。
      直接把「收尾中（3 隻怪在打我）」「開始巡迴：3 → 4 → 5 → 1 → 2 → 3」
      這種會變長的字塞進那一列，方框就跟著變寬 → 右欄被推出視窗、
      整頁多出捲軸。字愈長版面愈歪，而且只有跑到那個狀態才會發生。

    三道防線：
      · 水平 SizePolicy = Ignored —— 文字多長都**不列入**版面寬度計算
      · 放不下就用「…」截斷，完整內容進滑鼠提示（資訊不會消失）
      · 沒訊息時整個隱藏 —— 版面不會多出一條空白列（Qt 會收掉）

    ⚠ 覆寫了 text()：回傳的是**完整內容**不是截斷後的，這樣呼叫端
      「內容沒變就不要 setText」那種比較才不會每一拍都判定成有變
      （心跳 10ms 一拍，每拍重畫會閃）。
    """

    def __init__(self, hide_when_empty: bool = True) -> None:
        super().__init__("")
        self.setStyleSheet("color: #9aa2b8;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._full = ""
        self._hide_empty = hide_when_empty
        if hide_when_empty:
            self.setVisible(False)

    def setText(self, text: str) -> None:            # noqa: N802 (Qt 命名)
        text = text or ""
        if text == self._full:
            return                                   # 沒變就不重畫
        self._full = text
        self.setToolTip(text.strip())
        if self._hide_empty:
            self.setVisible(bool(text.strip()))
        self._paint()

    def text(self) -> str:
        return self._full

    def _paint(self) -> None:
        w = max(self.width() - 2, 24)
        QLabel.setText(self, self.fontMetrics().elidedText(
            self._full, Qt.ElideRight, w))

    def resizeEvent(self, ev) -> None:               # noqa: N802 (Qt 命名)
        super().resizeEvent(ev)
        self._paint()                                # 欄寬變了要重算截斷


def _send_scan(hwnd: int, vk: int = DEFAULT_KEY) -> None:
    """對指定視窗送一次技能鍵。

    走 app/core/window.py 的共用實作：SendMessageTimeout + 帶掃描碼的 lParam。
    ⚠ 實測這個遊戲**只吃 SendMessage**，PostMessage 完全沒反應（不管 lParam）。
    不碰使用者真正的鍵盤、不搶焦點，可以同時掛多個分身。

    ★ 逾時設短（SEND_TIMEOUT_MS）：SendMessage 會等對方處理完才返回，
      遊戲一卡（讀地圖、掉幀）一次按放最多要吃兩倍逾時。實測正常只要 3.4ms，
      但量到過單拍 353ms —— 那就是使用者說的「偶爾施放速度卡卡的」。
      這裡寧可漏送一次，也不要讓整條節奏停住。
    """
    win.send_key(hwnd, vk, SEND_TIMEOUT_MS)


class _NamedKeyBox(QComboBox):
    """按下去才去讀「這個鍵現在放什麼技能」的下拉（首次攻擊用）。

    ★ 只改字（setItemText），**不重建清單**：選單重建會讓順序跳動、
      使用者點不到（見 [[qt-ui-pitfalls]]）。所以項目在建構時就固定，
      展開的當下只更新顯示文字。
    """

    def __init__(self, refresh, parent=None) -> None:
        super().__init__(parent)
        self._refresh = refresh

    def showPopup(self) -> None:                   # noqa: N802（Qt 的命名）
        try:
            self._refresh()
        except Exception:                          # noqa: BLE001
            pass                                   # 讀不到快捷欄就顯示素的 F1~F12
        super().showPopup()


class _Paced(QThread):
    """固定節奏的背景迴圈。子類別實作 step()。

    ⚠ Windows 的 Sleep() 預設精度只有約 15.6ms —— msleep(50) 可能睡成 62ms，
    節奏會忽快忽慢。timeBeginPeriod(1) 把系統計時器解析度調到 1ms；
    這是行程層級的設定，用完一定要成對還原。
    """

    def __init__(self, interval: float) -> None:
        super().__init__()
        self._interval = interval
        self._running = True

    def set_interval(self, secs: float) -> None:
        self._interval = max(0.005, secs)

    def stop(self) -> None:
        self._running = False

    def step(self) -> None:                      # 子類別實作
        raise NotImplementedError

    def run(self) -> None:
        ctypes.windll.winmm.timeBeginPeriod(1)
        try:
            due = time.perf_counter()
            while self._running:
                self.step()
                # 睡「到下一拍為止」，不是「睡一個間隔」—— 否則週期會變成
                # 間隔 + 這一拍的工作時間，忙的時候還會越漂越多。
                due += self._interval
                left = due - time.perf_counter()
                if left > 0:
                    self.msleep(int(left * 1000) or 1)
                else:
                    due = time.perf_counter()    # 落後太多就重新對時
        finally:
            ctypes.windll.winmm.timeEndPeriod(1)


class KeyWorker(_Paced):
    """**只**負責發動攻擊，什麼都不判斷。

    刻意跟「寫入目標」拆開：發動只要準時，而讀寫記憶體偶爾會慢。
    綁在一起的話，任何一次記憶體操作變慢都會拖到節奏（使用者感受到的
    「施放速度卡卡的」）。拆開之後這條執行緒永遠只做一件事：到點就發一次。

    ★★★ 出手＝叫遊戲自己的「用快捷鍵」（quickbar.use，等同按 F2）。
    ⚠⚠ **不要改回自己送施放封包**：那樣只打得出普攻 —— 實測 MP 一格不扣
      （破甲劈擊Ⅳ 該扣 25），血量掉的量是武器普攻；改叫遊戲那支之後 MP
      正好扣 25、兩次呼叫就打死一隻。同場 60 秒：封包 9 隻／快捷鍵 18 隻
      （官方掛 11 隻）。
    退路：快捷欄叫不動（改版位移／視窗還沒建）就送鍵（_send_scan），
    一樣是遊戲自己出手。attack.select 仍要送一次，讓伺服器知道我們選了誰。

    ★ 技能直接讀快捷欄（app/game/quickbar.py）：使用者勾幾個 F 鍵，這裡把
      每個鍵解析成技能 ID、照 F1→F12 的順序**輪流施放**。空格、放物品的格
      **不進循環**（使用者指定 —— 也就不會誤按把藥吃掉）。掛機中每
      QB_REFRESH 秒重讀一次，中途換快捷欄上的技能會自動跟上、每台分身
      各讀各的。快捷欄整個讀不到（改版位移）才退回按鍵：單鍵時還能用
      「清零→按鍵→讀殘留」學回技能 ID（player.read_last_skill），
      多鍵就純輪流按 ——「最近使用」認不出是哪個鍵按出來的。

    ⚠ 送鍵一定是**一次次按放**。曾經想改成「只送 KEYDOWN 模擬按住」，
      方向是錯的 —— 使用者實測這個遊戲按住不放並不會一直放技能。
    """

    def __init__(self, hwnd: int, sc: MemoryScanner) -> None:
        super().__init__(STRIKE_TICK)          # 要切得出 0.1 秒的輪間隔
        self.hwnd = hwnd
        self.sc = sc
        self.vks: list[int] = [DEFAULT_KEY]   # 使用者勾的技能鍵（輪流用）
        self.skills: dict[int, int] = {}      # 鍵碼 → 快捷欄解析出的技能 ID
        self.qb_ok = False          # 上一次快捷欄讀取有沒有成功（UI 提示用）
        self._rot = 0               # 輪到循環裡的第幾個
        self._qb = quickbar.Reader(sc)
        self._next_qb = 0.0
        self.mode = MODE_PACKET     # 這個分頁用哪種攻擊方式
        self.packets = True         # 使用者要不要用封包攻擊（封包模式才有意義）
        self.stats = None           # 角色屬性基準（學技能 ID 用）
        self.mover = None
        # move.pathfinder_this()：**玩家物件 −8**。
        # ⚠ 攻擊時**不用**這份快取（step() 會自己當場重算，見那裡的說明）；
        #   留著只是給自動分身當「玩家物件定位好了沒」的判斷用。
        self.pf = None
        self.eid = None             # 現在要打誰
        # 目標的格子座標，填在施放封包裡 —— 順移那類對地技能沒有座標發不動。
        self.pos: tuple[float, float] = (0.0, 0.0)
        # ★ 出手前自己再量一次距離用的玩家物件位址（見 step()）。
        self.player = None
        # ⚠ `reach` **只有交棒那一輪才有值**（總距離）。平常是 0 ＝ 沒有單一
        #   攻擊距離，每一招在 step() 裡各自比自己的射程（使用者指定）。
        self.reach = 0.0
        # ★★ 這一輪是不是「交棒給客戶端自己走過去」（見掛機那邊的 handoff）。
        #   ⚠⚠ 交棒時**不可以**用單招射程擋出手：交棒的整個作用就是
        #     「在 9.8 格叫快捷鍵，讓遊戲自己走過去打」，用近戰射程 2 格去擋
        #     等於把交棒整個廢掉（角色站在原地不動）。
        self.client_walk = False
        self._sel = None            # 已經送過「選定」的目標（換目標才要再送）
        # ★★ 首次攻擊（使用者要求）：**每一隻怪的第一下一定要是這個鍵上的招**，
        #   它真的放出去之前，其他招一個都不放 —— 那招在冷卻就站著等它好。
        #   0 = 不指定（照舊直接輪流放）。
        self.opener_vk = 0
        self._open_eid = None       # 現在這道鎖是針對哪一隻（換怪就重新上鎖）
        self._opened = False        # 這一隻的首發已經**真的**放出去了（收到廣播）
        # ★★ 首發確認靠「施放廣播監聽」（castwatch，100% 訊號）——
        #   收到「我(srv_id)放出這一招」的伺服器廣播才算數，拒收不會有。
        self.castwatch = None       # CastHook（分頁 acquire 後塞進來；None=退舊行為）
        self._srv_id = None         # 我的施法者伺服器ID＝[玩家實體+0x1D0]（每隻重讀）
        self._cast_since = 0        # 上鎖當下 castwatch 的攔包計數（只看之後的廣播）
        self._open_since = 0.0
        self.open_wait = 0.0        # 已經等了幾秒（GUI 讀：0 = 沒在等）
        self.open_note = ""         # 跳過首發的原因（GUI 取走後自己清掉）
        self._next_round = 0.0      # 下一輪可以開始的時間
        self._page = 0              # 快捷欄目前頁（開始掛機時讀一次）
        self._on = False
        self._learning = False
        self._next_learn = 0.0

    def set_on(self, on: bool) -> None:
        self._on = on

    @property
    def skill(self) -> int | None:
        """循環裡第一個解析到的技能；一個都沒有就 None。

        舊介面：tick 那邊拿它判斷「封包模式到底打不打得出去」
        （self._atk.packets 的條件之一），別的地方不要再用。
        """
        for vk in self.vks:
            sid = self.skills.get(vk)
            if sid:
                return sid
        return None

    def skip_note(self) -> str:
        """勾到的鍵裡有非攻擊技能就提一下（**只是提醒，照樣會放**）。

        位移／補血／buff 這類放進攻擊循環通常沒有意義，但要不要放是使用者的
        決定 —— 我們只負責講清楚，不替他過濾（使用者明確要求）。
        """
        note = []
        for vk in self.vks:
            sid = self.skills.get(vk)
            if sid and not skills.is_attack(sid):
                key = (f"F{vk - quickbar.VK_F1 + 1}"
                       if quickbar.VK_F1 <= vk < quickbar.VK_F1 + quickbar.SLOTS
                       else f"鍵{vk:#x}")
                note.append(f"{key}（{skills.name_of(sid) or sid}）")
        return ("　※ " + "、".join(note) + " 不是攻擊型技能（照樣會放）"
                if note else "")

    def all_keys(self) -> list[int]:
        """輪替的鍵**加上**首發鍵（首發可以是沒勾的鍵，見 _opener_gate）。

        ⚠ 讀快捷欄、算走位距離、判斷能不能交棒都要用這一份 ——
          只看勾選的鍵的話，「只當開場、不進輪替」的那一招會：
          ① 解析不到技能 ID（首發閘門直接失效）
          ② 不列入 min_range → 停在別招的射程外，首發永遠打不到。
        """
        vks = list(self.vks)
        if self.opener_vk and self.opener_vk not in vks:
            vks.append(self.opener_vk)
        return vks

    @property
    def handoff(self) -> bool:
        """這一輪的技能能不能「交給客戶端自己走過去」。

        ★ 只有走**快捷鍵**那條路的技能可以：呼叫遊戲的 usequickkey 時，
          遊戲自己會走到射程內再出手（實測站 9.8 格、我們不下移動指令，
          角色自己走了 8.5 格過去把怪打死）。
        ⚠ 走封包的（射程 > 8、或對地技能）**不會**有這個行為 —— 那是
          快捷鍵函式自己做的事，封包只是把「我要放這招」告訴伺服器。
        ⚠ 只要輪裡有任何一招要走封包，就不能交棒（那招會在遠處空放）。
        """
        got = False
        for vk in self.all_keys():
            sid = self.skills.get(vk)
            if not sid:
                continue
            rng = skills.range_of(sid)
            if skills.is_ground(sid) or (rng is not None and rng > 8):
                return False
            got = True
        return got

    @property
    def min_range(self) -> int | None:
        """輪流施放的技能裡**最短**的射程（格）；一個都查不到回 None。

        ★ 取最短的：走到最短射程之內，這一輪裡每一招才都打得到。
          （雪狐 F2 破甲劈擊射程 1；黑狐 F2 電擊術射程 12。）
        ⚠ **射程 0 的不算**：那是純 buff（對象＝自己、表裡沒有射程也沒有
          範圍，例如單體分身、歃血狂暴）。它們不需要靠近，把 0 算進來會
          把攻擊距離壓成 1 格，怪還沒進範圍就不打了。
        """
        out = [r for r in (skills.range_of(self.skills.get(vk) or 0)
                           for vk in self.all_keys()) if r]
        return min(out) if out else None

    def reach_of(self, sid: int) -> float:
        """**這一招自己**打得到的距離（格）。

        ★★★ 攻擊距離是**每一招各自的事**，不准取全輪的最短或最長
          （使用者 2026-08-10 明確指定）。以前用「輪替裡最短的射程」當
          單一攻擊距離，混合輪替（幻影刺殺 12 格＋破甲劈擊 1 格）就被壓成
          2 格 —— 射程 12 的那招被硬生生拖到臉上才放，走不進去時還會整段
          站著不動（實測 90 秒有 45 秒在發呆）。
        ⚠ 射程 0 ＝ 對自己的 buff：不看距離（回 inf），但**不能**拿它去
          決定「打不打得到」，見 in_range_of_any。
        ⚠ 換算：技能表寫的是格數，斜角相鄰算一格，所以歐氏距離 ≈ 射程 + 1；
          上限 ATTACK_PACKET_RANGE 是封包攻擊實測打得到的最遠距離。
        """
        r = skills.range_of(sid)
        if not r:
            return float("inf")
        return min(ATTACK_PACKET_RANGE, float(r) + 1.0)

    def in_range_of_any(self, dist: float | None) -> bool:
        """這個距離下**有沒有任何一招打得到**（每一招各自比自己的射程）。

        ★ 這取代了舊的「單一攻擊距離」：有一招打得到就該出手，打不到的
          那幾招由 step() 自己跳過。所以不必、也不該先算出一個代表值。
        ⚠ 純 buff（射程 0）不列入判斷 —— 不然站在 50 格外也會被算成
          「打得到」而開打。
        ⚠ 一招都查不到射程（改版新技能／快捷欄讀不到）就退回舊的 12 格，
          不要因此完全不出手。
        """
        if dist is None:
            return True
        known = False
        for vk in self.all_keys():
            sid = self.skills.get(vk)
            r = skills.range_of(sid) if sid else None
            if not r:
                continue
            known = True
            if dist <= min(ATTACK_PACKET_RANGE, float(r) + 1.0):
                return True
        return False if known else dist <= ATTACK_PACKET_RANGE

    @property
    def selected(self) -> bool:
        """「選定」封包已經替現在這個目標送出去了嗎？

        ★ 很重要：遊戲**收到選定封包才會填目標血量**。沒送之前血量一直是 0，
          跟屍體長得一模一樣（實測：只寫記憶體選目標，6 隻活怪全部沒血量；
          補送選定封包後同樣那 6 隻全部顯示血 75）。
          所以「多久沒看到血量就算屍體」一定要從這裡開始算。
        """
        return bool(self.eid) and self._sel == self.eid

    def begin_learning(self) -> None:
        """把「勾選的技能鍵」各自解析成技能 ID —— 直讀快捷欄（quickbar.py）。

        純讀零副作用，當場拿到，不必真的放技能。只收技能格：空格、放物品
        的格**不進攻擊循環**（使用者指定）。掛機中 step() 每 QB_REFRESH 秒
        會重讀，所以這裡只是把第一次讀提前到按下「開始」的當下。

        ② 舊法保底（快捷欄整個讀不到，例如改版位移）：只有單鍵能用 ——
           這裡先清零，後面在 step() 裡按鍵＋每 LEARN_GAP 讀一次殘留。

        ⚠⚠ 舊法**一定要先清零**。單次按鍵不保證會寫入（冷卻／間隔；黑狐在
          沒有目標時甚至完全不寫），不清零就會讀到**上一次殘留的技能 ID** ——
          雪狐就是這樣把 F3 的 0x2E1 當成 F2 的技能，結果完全打不動怪。
        """
        self._rot = 0
        self._next_qb = 0.0
        self._learning = False
        self._open_eid = None          # 重新開始 → 首發重新上鎖
        self.open_wait = 0.0
        try:
            got = self._qb.skills(self.all_keys())
            self._page = self._qb.page()       # 出手要指名頁＋格
        except Exception:                      # noqa: BLE001
            got = None
        self.qb_ok = got is not None
        self.skills = got or {}
        if got is not None:
            return      # 快捷欄讀得到：結果就是答案（沒技能＝不出手）
        # 快捷欄整個讀不到 —— 單鍵才能用舊法：「最近使用的技能」認不出
        # 是哪個鍵按出來的，多鍵輪按會張冠李戴（學到別鍵的技能更糟）。
        if len(self.vks) != 1:
            return
        self._learning = True
        self._next_learn = 0.0
        try:
            if self.stats:
                player.clear_last_skill(self.sc, self.stats)
        except Exception:                      # noqa: BLE001
            pass

    def stop_learning(self) -> None:
        self._learning = False

    def _learn(self, vk: int) -> None:
        """舊法第三步：讀記憶體。欄位已清零，所以非零值就是剛按的鍵的技能。"""
        now = time.perf_counter()
        if now < self._next_learn:
            return                             # 按鍵到寫入要隔一幀，別讀太密
        self._next_learn = now + LEARN_GAP
        if not self.stats:
            return
        sid = player.read_last_skill(self.sc, self.stats)
        if sid:
            self.skills = {**self.skills, vk: sid}
            self._learning = False

    def _sp_blocked(self, sid: int) -> str:
        """首發這一招現在放不出來，而且**等下去也不會好** → 回傳跳過的原因。

        ★★ 使用者指定：吃 SP（能量燈）的技能 SP 不夠時要**跳過**，不要等 ——
          SP 是打怪打出來的，我們為了等首發又不出手，SP 永遠不會回來，
          站在那裡就是死結。冷卻不一樣（時間到就會好），那個要等。

        判斷完全讀遊戲自己的資料（app/game/skillcost.py）：技能範本的
        `消耗SP燈` 與角色目前的 SP，兩邊都當場驗過才採用。

        ⚠ **「不知道」也算跳過**（回原因字串）：讀不到就代表我們分不出
          「在冷卻」跟「SP 不夠」，賭錯一邊就是永久卡死。跳過會在狀態列
          寫清楚，不是安靜地發生。
        """
        stats = player.read(self.sc, self.stats) if self.stats else None
        ok = skillcost.sp_enough(self.sc, self.player or 0,
                                 stats.mp if stats else 0, sid)
        if ok is True:
            return ""
        name = skills.name_of(sid) or f"技能{sid}"
        if ok is False:
            return f"⚡ SP 不夠，這一隻跳過首發「{name}」"
        return f"⚠ 讀不到 SP／技能消耗 → 這一隻跳過首發「{name}」"

    def _opener_gate(self, eid, bykey: dict, now: float) -> int | None:
        """首次攻擊的閘門：回傳「這一輪只准放這個鍵」，None = 沒鎖／已解鎖。

        規格（使用者 2026-08-18）：選了首次攻擊、而且不是空鍵、也不是 SP 不夠
        → **持續發射到收到自己的施放廣播**，收到後轉技能鍵輪迴（首發只放這次、
        輪迴不含它，見 step 的排除）。

        怎麼 100% 確定「這一招真的被伺服器受理」
        --------------------------------------
        ⚠⚠ 不能看 `quickbar.use()`／`cast_at()` 的回傳值（只代表送出了）；
          不能看 SP/MP 扣款（連射時糊在一起分不出）；不能看最近技能欄位
          （對地技能不寫）；不能看 +0x418 清單（邊走邊放會漏）。
        ★★★ 唯一可靠訊號＝**施放廣播**（castwatch，見 app/game/castwatch.py）：
          伺服器對每一次受理的施放回一包「施法者ID＋技能ID」，拒收不回、
          連射逐發、對地也有。上鎖時記下攔包計數 `_cast_since`，之後只要出現
          「施法者==我(`[玩家實體+0x1D0]`)、技能==首發那招」就是確認。

        跳過只有使用者准的三種＋一種硬故障：沒選／空鍵（上面 not sid）、
        SP 不夠（等不到，見 _sp_blocked）、castwatch 裝不起來（退化「送一次
        就算」保險，絕不讓角色永遠站著不出手；狀態列會講）。
        """
        vk = self.opener_vk
        sid = bykey.get(vk) if vk else None
        if not sid:
            # 沒指定首發、或那個鍵上現在沒技能（空格／物品格／快捷欄讀不到）
            # → 不上鎖，照舊輪流放。
            self._open_eid = None
            self.open_wait = 0.0
            return None
        if self._open_eid != eid:                  # 換了一隻 → 重新上鎖
            self._open_eid = eid
            self._opened = False
            self._open_since = now
            # ★ 伺服器施法者ID 每隻重讀（換圖/重連會重建玩家物件，不能快取）。
            self._srv_id = (castwatch.own_server_id(
                self.sc, bag.player_entity(self.sc))
                if self.castwatch else None)
            # 只看「上鎖之後」的廣播（避免上一隻的遲到廣播誤判）。
            self._cast_since = (self.castwatch.write_count()
                                if self.castwatch else 0)
        if self._opened:
            self.open_wait = 0.0
            return None
        # ★★★ 100% 確認：收到「我放出這一招」的施放廣播 → 解鎖接輪迴。
        if (self.castwatch and self._srv_id
                and self.castwatch.fired(self._cast_since, self._srv_id, sid)):
            self._opened = True
            self.open_wait = 0.0
            return None
        # ★ SP 不夠（或分不出來）→ 放行其他招，別站在那裡等一個不會好的條件。
        why = self._sp_blocked(sid)
        if why:
            self._opened = True
            self.open_wait = 0.0
            self.open_note = why
            return None
        # ⚠ castwatch 裝不起來（AOB 對不上／改版／拒裝）→ 沒有可靠確認訊號，
        #   退化成「送一次就算」：**這一拍先送首發**（回 vk）、標記已開，
        #   下一拍就轉輪迴。絕不讓角色永遠站著不出手（大聲講）。
        if not (self.castwatch and self._srv_id):
            self._opened = True
            self.open_wait = 0.0
            self.open_note = ("⚠ 施放廣播監聽不可用（改版？）→ 首發送一次就"
                              "接輪迴（無法逐發確認）")
            return vk
        # ⚠ 下限給一點點：`open_wait > 0` 是掛機那邊「正在等首發」的旗標
        #   （拿來凍住換怪計時器），剛上鎖那一拍差值是 0，不墊高的話那一拍
        #   會被當成「沒在等」。
        self.open_wait = max(now - self._open_since, 0.001)
        return vk

    def step(self) -> None:
        # ⚠⚠ 先把 GUI 執行緒會改的欄位抄成區域變數，整拍只看這份快照。
        #   踩點在 `_sel`：以前是「送 select(self.eid) → 成功 → self._sel =
        #   self.eid」，兩次 self.eid 之間 GUI 剛好換了目標的話，就把**新目標**
        #   記成「已選定」，可是那一包從來沒送出去。接著 `selected` 回 True →
        #   屍體計時開始跑 → 遊戲永遠不會替這隻填血量 → 0.8 秒後把一隻活怪
        #   當屍體丟掉。每殺一隻換一次目標，這個窗口每一輪都存在。
        eid, mover = self.eid, self.mover
        mode, packets, pos = self.mode, self.packets, self.pos
        vks = list(self.vks) or [DEFAULT_KEY]
        try:
            if eid is None:
                self._sel = None       # 沒目標了，下一隻要重新送「選定」
                self._open_eid = None  # 首發：下一隻重新上鎖
                self.open_wait = 0.0
                # ★★★ **沒有目標就絕對不出手。**
                #   底下那條「快捷欄叫不動就送鍵」的退路（`_send_scan`）
                #   不看 eid，而按 F 鍵放技能時**遊戲會自己挑一隻最近的敵人
                #   打**（客戶端本來就有這個行為，見 [[client-auto-approach]]）。
                # ⚠⚠ 這個窗口每換一次目標就出現一次，跟換不換頻道無關：
                #   `_on_died()`／「走不到」／「零傷害換怪」都是
                #     ① self._keys.eid = None
                #     ② if not self._pick_next(): set_on(False)
                #   而 ② 要走一整趟挑目標（每隻怪重讀死活座標、算距離），
                #   這條執行緒在那零點幾秒裡照樣在跑 —— eid 已經是 None、
                #   `_on` 還是 True，於是送出去的那一鍵打的是**遊戲替我們挑的
                #   怪**：可能是名單外的，也可能是旁邊那隻王
                #   （使用者回報「會打我沒選的怪、還會去打王打到死」）。
                #   ⛔ 不可以改成「在 UI 那邊調換 ①② 的順序」：`_pick_next()`
                #     本來就要先看到 `_cur is None` 才會挑下一隻。
                #     由這裡自己擋才是唯一真相來源。
                return
            if not self._on:
                # ⚠⚠ 打不到（怪跑出射程、正在走過去）時**一定要把「等首發」
                #   歸零** —— 掛機那邊拿它凍住「沒進展就換一隻」的計時器，
                #   不歸零的話一隻走不過去的怪會被無限期追下去
                #   （被 early return 餓死的狀態機，見 [[frozen-tick-state-machines]]）。
                #   鎖本身（_open_eid/_opened）留著，回到射程內接著等就好。
                self.open_wait = 0.0
                return
            # ★★★ 換圖／死亡復活／重連的實體重建空窗：這一拍**整輪不出手**
            #   （2026-08-16，崩潰 dump 8/13＋8/15 定案）。遊戲的 usequickkey
            #   拿「自己實體 id」查表、查不到不驗 NULL 就寫入 → 當場崩潰；
            #   真人按不到鍵（載圖時吃不到），只有跳板照按。在這裡擋而不是
            #   只靠 quickbar.use 裡那道閘 —— use 回 False 會落到「送鍵退路」
            #   （_send_scan），等於換條路踩同一個地雷。純讀四個 u32，10Hz
            #   下成本可忽略；空窗過了下一拍自然恢復。
            if not quickbar.self_entity_ok(self.sc):
                self.open_wait = 0.0
                return
            # ◎ 每 QB_REFRESH 秒重讀快捷欄：使用者中途換鍵上的技能會自動
            #   跟上（純讀零副作用）。讀失敗**保留舊結果**——改版位移那種
            #   持續性失敗有 qb_ok 記著，暫時性讀失敗不該把打法歸零。
            now = time.perf_counter()
            if now >= self._next_qb:
                self._next_qb = now + QB_REFRESH
                try:
                    # ⚠ 要連首發鍵一起讀（它可以是沒勾的鍵）—— 見 all_keys()
                    got = self._qb.skills(self.all_keys())
                    self._page = self._qb.page()   # 使用者中途翻頁也要跟上
                except Exception:              # noqa: BLE001
                    got = None
                self.qb_ok = got is not None
                if got is not None:
                    self.skills = got
            # ⚠ 這份快照**不可以**叫 `skills` —— 那會把 app.game.skills 模組
            #   遮住，底下 `skills.is_ground()` 之類就會丟 AttributeError，
            #   而整個 step() 被 try 包著、例外被吞掉 = 完全不出手也沒訊息。
            bykey = self.skills                # 快照（GUI 執行緒也會換掉它）
            # ① 換目標才送一次「選定」封包 —— 我們是直接寫記憶體選怪的，
            #    遊戲不會自己送這一包。兩種模式都要送。
            if mover is not None and eid and self._sel != eid:
                if not attack.select(mover, eid):
                    return                     # 這一拍先不打，下一拍再試
                self._sel = eid
            # ② 攻擊：叫遊戲自己的「用快捷鍵」（見下面 quickbar.use 那段）。
            # ⛔ 這裡以前要先算 `pf`（玩家物件 −8）當裸指標傳給施放函式 ——
            #   現在整條路都由遊戲自己走，**不再把任何裸指標交給遊戲**，
            #   那個最容易造成當機的破口（見 [[game-crash-root-causes]] 第②條）
            #   就此消失。quickbar.use 用的 this 是快捷欄物件，它自己當場重驗。
            # ◎ 這一拍輪到哪個鍵：**只輪有技能的鍵**（空格／物品格不進循環，
            #   使用者指定 —— 也就不會誤按把藥吃掉）。
            #   快捷欄讀得到、但勾的鍵上一個技能都沒有 → 不出手（開始掛機時
            #   狀態列會提示）。整個讀不到（改版位移）→ 退回把勾的鍵輪流按，
            #   放什麼遊戲自己決定。
            # ◎ 使用者勾了哪些鍵就放哪些鍵 —— **不要自作主張過濾**。
            #   ⛔ 曾經加過「只收攻擊型技能」，被使用者否決：瞬移術那類
            #     位移技能他要能正常用。非攻擊技能只在選單與狀態列標示
            #     （skills.is_attack），要不要放由使用者決定。
            usable = [k for k in vks if bykey.get(k)]
            if not usable and self.qb_ok:
                return                    # 快捷欄讀得到、但勾的鍵上沒技能
            # ★★ 首次攻擊（使用者要求）：這一隻的第一下**一定要是**指定的那招。
            #   還沒真的放出去之前，這一輪就只試它一個 —— 那招在冷卻就等，
            #   等多久都等（不設上限，出口是「暫停」；狀態列會顯示等了幾秒）。
            # ★ 首發鍵**不必**在勾選的技能鍵裡：可以拿一招只當開場、不進輪替。
            opener = self._opener_gate(eid, bykey, now)
            if opener is not None:
                usable = [opener]
            elif self.opener_vk in usable and len(usable) > 1:
                # ★★ 修 bug（2026-08-18 使用者回報「首發技能被加進輪迴一直放」）：
                #   首發**只在開頭放一次**，收到廣播後的輪迴**不含首發鍵** ——
                #   就算它同時勾在技能鍵裡也不再放。
                # ⚠ 它是唯一有技能的鍵時**例外保留**（len>1 這個條件擋住）：
                #   濾掉整輪就空了、開場後角色永遠不出手；那種每隻怪就靠
                #   換怪重新上鎖各放一次首發（等於它自己就是輪迴），可接受。
                usable = [k for k in usable if k != self.opener_vk]
            # ★★★ 出手方式**依射程分流**（使用者定的）：
            #   射程 ≤ QUICKKEY_RANGE(8) → 叫遊戲的快捷鍵（quickbar.use，
            #     等同按 F2）。⚠⚠ 自己送施放封包**只打得出普攻**：實測 MP
            #     一格不扣（破甲劈擊該扣 25）；改叫這支之後 MP 正好扣 25、
            #     兩次呼叫就打死一隻。射程、冷卻、動作、封包全由遊戲處理。
            #   射程 > 8 → 送帶「目標 ID + 格子座標」的施放封包（attack.cast_at）。
            #   ⚠ **對地技能（對象＝地面）一律走封包**，跟射程無關 ——
            #     按鍵會跳出「選範圍」的游標等人點地板，角色就站著不動
            #     （使用者實際遇到）。射程 8 以內的對地技能有 74 個
            #     （衝鋒Ⅰ~Ⅲ、末日反噬…），所以這道覆寫不能省。
            # ★★ 節奏（使用者指定）：**一輪＝勾選的技能鍵依序各放一次**，
            #   放完整輪之後隔 ROUND_GAP 秒再放下一輪。
            #   空格／物品格／沒學到的鍵不進循環（usable 已經濾掉）。
            #   ⚠ 一輪之內是**逐一等它做完**再放下一個（call_sync）：跳板只有
            #     一個指令槽，連續丟只有第一個進得去，後面全被擋掉 ——
            #     那就變成「一輪只放到第一招」。
            # ★★ **出手前自己再量一次距離**，不要只信 UI 執行緒設的 `_on`。
            #   `_on` 是 UI 每一拍算好推過來的，但 UI 會被尋路（單次最久等
            #   0.15 秒）和掃描結果處理卡住，那段期間怪已經跑出射程、旗標卻
            #   還是舊的 —— 就會出現「超出射程還在放技能」（使用者指出的）。
            #   這裡只多讀一次玩家座標（微秒級），目標座標用 UI 上一拍給的。
            # ★ 順便把「現在離目標多遠」留下來：底下每一招要各自驗自己的射程。
            dist_now = None
            if self.player and pos != (0.0, 0.0):
                me_now = entity.read_pos(self.sc, self.player)
                if me_now:
                    dist_now = math.hypot(pos[0] - me_now[0],
                                          pos[1] - me_now[1])
            in_reach = not (self.reach and dist_now is not None
                            and dist_now > self.reach)
            if not (in_reach and now >= self._next_round):
                return
            # ⚠ 先排下一輪再開始放：中間任何一招失敗都不影響節奏，
            #   也不會因為某次例外就再也不出手。
            self._next_round = now + ROUND_GAP
            struck = False
            if mode == MODE_PACKET and packets and eid and mover is not None:
                for k in usable:
                    sid = bykey.get(k)
                    if not sid:
                        continue
                    # ★★★ **每一招各自比自己的射程**（使用者 2026-08-10 指定：
                    #   攻擊距離不准取全輪的最短或最長）。輪替裡混著近戰與遠程
                    #   時，射程 12 的那招 12 格就丟出去，近戰那幾招這時候放
                    #   只是空放、白花 MP／SP，直接跳過這一輪。
                    # ⚠ 射程 0 ＝ 對自己的 buff，reach_of 回 inf ＝ 不看距離。
                    # ⚠⚠ 交棒給客戶端走的那一輪**不擋**：那時候本來就是
                    #   「站得遠、叫快捷鍵讓遊戲自己走過去」（見 client_walk）。
                    if (not self.client_walk and dist_now is not None
                            and dist_now > self.reach_of(sid)):
                        continue
                    # 依射程分流（使用者定的）：≤ QUICKKEY_RANGE 叫遊戲的
                    # 快捷鍵，超過就送帶 ID＋座標的施放封包；對地技能一律封包。
                    # ⛔ 首發**不例外**：2026-08-10 我一度讓首發一律走快捷鍵，
                    #   被使用者退回 ——「射程 < 8 送鍵、> 8 打封包」是他定的
                    #   規則，不准偷改。首發的等待改用欄位驗證（見 _opener_gate），
                    #   兩條路都驗得到，本來就不必動這個分流。
                    rng = skills.range_of(sid)
                    by_packet = (skills.is_ground(sid)
                                 or (rng is not None
                                     and rng > QUICKKEY_RANGE))
                    if by_packet:
                        ok = attack.cast_at(mover, sid, eid, *pos)
                    elif (quickbar.VK_F1 <= k
                            < quickbar.VK_F1 + quickbar.SLOTS):
                        ok = quickbar.use(mover, self.sc,
                                          k - quickbar.VK_F1, self._page)
                    else:
                        ok = False
                    struck = struck or ok
            if not struck:
                # 退路：快捷欄叫不動（改版位移／物件還沒建）就送鍵。
                # ⚠ 送鍵一次要按住 40ms，一輪送一個就好（見 [[key-send-hold]]）。
                vk = (usable or vks)[self._rot % len(usable or vks)]
                _send_scan(self.hwnd, vk)
                self._rot += 1
                if self._learning:
                    self._learn(vk)            # 剛按過鍵，順手讀一下
            # ⚠ 首發的「已放出去」現在只由 _opener_gate 靠施放廣播判定
            #   （送出≠受理，絕不在這裡把送出當完成 —— 那正是舊「一直放」的病根）。
        except Exception:                      # noqa: BLE001
            pass


class TargetWorker(_Paced):
    """**只**負責「現在該打誰」：持續把目標寫回遊戲，並偵測牠死了沒。

    跑得比送鍵快（預設 50 Hz），所以送鍵那條永遠打得到最新的目標；
    別人搶走怪、或怪倒下時也能在幾十毫秒內發現。
    """

    # ⚠ 用 Signal(object) 不能用 Signal(int)：實體 ID 是**無號** 32 位元
    # （實測有 0x8E8D04DA = 23 億這種值），而 PySide6 的 int 對應 C++ 的
    # 有號 int，emit 超過 21 億的值會丟 OverflowError，死亡偵測就斷了。
    # 目標沒了（實體 ID, 是不是確定死了）→ UI 執行緒去挑下一隻。
    # 第二個參數 False = 只是「一直沒給血量」的推測，那隻可能還活著，
    # 所以只短暫跳過、不要封鎖一分鐘。
    died = Signal(object, bool)
    failed = Signal(str)        # 讀寫記憶體失敗
    # 我們手上的狀態物件位址已經不是狀態物件了（換地圖／重生／重連之後
    # 物件搬家）。UI 收到就把所有快取位址作廢並重新定位。
    stale = Signal()

    def __init__(self, sc: MemoryScanner) -> None:
        super().__init__(WRITE_INTERVAL)
        self.sc = sc
        self.hp = 0                 # 最近讀到的目標血量，給 UI 顯示
        # 用封包攻擊時 True：只寫目標 ID、**不碰血量欄位**，
        # 這樣血量才是遊戲寫的真值，0 就真的代表死了。見 entity.set_target_id。
        self.packets = False
        self._job: tuple[int, entity.Entity] | None = None
        self._wrote = False
        self._saw_hp = False        # 這隻有沒有讀到過 > 0 的血量
        self._zero_at = 0.0         # 血量開始連續讀到 0 的時間
        self._since = 0.0           # **選定封包送出**之後過了多久（判斷屍體用）
        # 「選定」封包送出去了沒。⚠ 遊戲收到那一包才會填血量，
        #   所以沒送之前不能開始算屍體 —— 否則走過去的路上會把活怪全丟掉。
        self.engaged = False

    def attack(self, state: int, ent: entity.Entity) -> None:
        self._wrote = False
        self._saw_hp = False
        self._zero_at = 0.0
        self._since = time.monotonic()
        self._job = (state, ent)

    def hold_off(self) -> None:
        self._job = None

    def step(self) -> None:
        job = self._job
        if job is None:
            return
        state, ent = job
        # ⚠⚠ 這幾個欄位是 GUI 執行緒隨時會改的（換目標、切模式）。
        #   一開始就抄成區域變數，整個 step() 只看這份快照 ——
        #   否則同一輪裡前後讀到不同的值，會算出前後矛盾的結論。
        packets, engaged = self.packets, self.engaged
        try:
            # ★ 先讀後判斷、最後才寫 —— 順序不能換。
            # 目標死掉時遊戲會把 +0x2D8 清成 0，那是我們唯一的死亡訊號；
            # 先寫回去就把訊號蓋掉了。
            # ★★ 同一次讀取順便確認「這個位址還是狀態物件」（比對 vtable）。
            #   物件搬家後這裡若不停手，就是每秒 50 次往別人的記憶體寫 4 bytes
            #   —— 遊戲會在很久以後莫名其妙掛掉。見 entity.read_target_checked。
            ok, cur, self.hp = entity.read_target_checked(self.sc, state)
            if not ok:
                # ⚠ 只清「自己這一步的」job —— GUI 執行緒可能在本步進行中
                #   剛派了新目標（attack()），無條件清會把新目標丟掉。下同。
                if self._job is job:
                    self._job = None
                self._wrote = False
                self.stale.emit()          # 叫 UI 那邊重新定位
                return
            now = time.monotonic()
            # ★★ 最快也最準的死亡訊號：怪自己的動畫狀態變成 'Dead'。
            #   50Hz 就讀得到，不必等 HP_SETTLE(0.5s) 或 CORPSE_SECS(0.8s)。
            #   別人搶先殺掉我們鎖定的那隻時，這條會立刻放手去挑下一隻 ——
            #   搶怪的地方「對著空氣卡個零點幾秒」就是這樣來的。
            #   ⚠ confirmed 用 _saw_hp 而不是 True：沒看過血量代表我們根本
            #     沒交戰過（那是別人的屍體），算進擊殺數會灌水。
            #   ⚠ 不需要 self._wrote —— 我們有沒有寫過目標，跟牠死了沒無關。
            # ★ 死活與動畫狀態一次讀回來（相鄰欄位）：以前狀態一次、
            #   下面的 is_alive 又兩次，這是 50Hz 的迴圈。
            alive, st, _p = entity.read_live(self.sc, ent)
            if st == "Dead":
                if self._job is job:
                    self._job = None
                self._wrote = False
                self.died.emit(ent.eid, self._saw_hp)
                return
            if self.hp > 0:
                self._saw_hp = True
                self._zero_at = 0.0
            elif not self._zero_at:
                self._zero_at = now
            # ★ 用封包攻擊時，血量歸零就是**遊戲告訴我們這隻死了**。
            #   這才是可靠的死亡訊號 —— 屍體不會馬上從實體清單消失，
            #   `is_alive()`（vtable + 實體 ID）也分不出死活。
            # ⚠⚠ 兩個條件缺一不可，**都是實測踩出來的**：
            #   ① 要先看過血量 > 0 —— 遊戲對「還沒交戰」的目標本來就回報 0，
            #      少了這條會剛鎖定就判死（實測 150 秒誤判放棄 22 次、
            #      真正擊殺只有 4 次）。
            #   ② 0 要**連續**持續 HP_SETTLE 秒 —— 中途偶爾讀到一次 0
            #      不算，免得把還活著的怪丟掉。
            dead_by_hp = (packets and self._saw_hp and self.hp == 0
                          and now - self._zero_at >= HP_SETTLE)
            # ★ 屍體：鎖定這麼久了還**從來沒有**看到血量 —— 那是別人先打死的。
            #   搶怪嚴重的地方這種很多，實測 120 秒有 26 隻，白白鎖住 52 秒
            #   ＝ 44% 的時間在對屍體發呆。
            #   ⚠ 門檻是量出來的：活著的目標鎖定後看到血量的時間，
            #     中位 0.30 秒、**最久 2.22 秒**（34 隻），所以 3 秒很安全。
            #   ⚠ 只有封包模式能用：按鍵模式我們自己會把血量寫成 100，
            #     `_saw_hp` 一定是 True（見下面寫入那段）。
            # ⚠ 還沒送出選定封包就一直重設計時 —— 遊戲收到那一包才會填血量。
            #   少了這條，走過去的路上（還沒進攻擊範圍、還沒送選定）會把
            #   活著的怪全部當成屍體丟掉（使用者回報「明明有怪，過去看一下就跑」）。
            if not engaged:
                self._since = now
            corpse = (packets and not self._saw_hp
                      and now - self._since >= CORPSE_SECS)
            if self._wrote and (cur == 0 or dead_by_hp or corpse or not alive):
                if self._job is job:
                    self._job = None
                self._wrote = False
                self.died.emit(ent.eid, not corpse)
                return

            if packets:
                # 只寫 ID。**不要寫血量** —— 那是遊戲用來回報死活的欄位，
                # 寫下去就把死亡訊號蓋掉了（見 entity.set_target_id）。
                entity.set_target_id(self.sc, state, ent.eid)
            else:
                # 按鍵攻擊才需要餵血量：遊戲攻擊前會檢查 +0x2DC > 0
                # （`cmp [esi+0x2dc],0 / jle 跳過`），而它每輪都會清回 0，
                # 所以**每一圈都要重寫**，不能「已經是這隻就跳過」。
                entity.set_target(self.sc, state, ent.eid)
            self._wrote = True
        except Exception as exc:               # noqa: BLE001
            if self._job is job:
                self._job = None
            self.failed.emit(str(exc))


@dataclass
class Scan:
    """一次掃描的結果。欄位一直在增加，包成一個物件比一直加訊號參數清楚。"""

    pid: int
    state: int | None = None      # 狀態物件（寫目標用）
    player: int | None = None     # 玩家實體物件（讀自己的座標用）
    stats: int | None = None      # 角色屬性基準（讀 HP 用；見 app/game/player.py）
    inv: int | None = None        # 物品指標陣列表頭（數藥水／找回程道具用）
    mons: list = field(default_factory=list)
    # kind=4 的實體（召喚物、擺攤玩家…）。自動召喚認養剛召出來的那隻要用；
    # is_monster 會把它們濾掉，所以另外帶一份。⚠ 掃描本來就一次拿齊，零成本。
    pets: list = field(default_factory=list)
    err: str = ""


class ScanWorker(QThread):
    """背景掃描：一次拿齊狀態物件、玩家物件、角色屬性、附近怪物。

    不能放在 UI 執行緒（全掃約 0.4 秒）。用一條常駐執行緒處理所有分身的請求，
    比每次開新執行緒好收尾。三種物件走 entity.snapshot() 合併成一遍讀取。

    ★ **熱區掃描**：實測這三種物件在每個分身裡都只出現在**單一一塊**記憶體
      （佔全部的 0.1%～1.9%）。所以只有第一次做全掃，之後記住命中的區塊，
      往後只掃那一塊。這讓「刷新怪物清單」便宜到可以每秒做一次，於是：
        · 打完一批怪不必停下來等掃描（原本會原地發呆一兩秒）
        · 掃描不再跟攻擊執行緒搶記憶體頻寬（原本 F2 節奏會被拖到）

    ⚠ 熱區會失效（換地圖、重連、堆積重配置），所以：
        · 每 FULL_EVERY 秒強制做一次全掃當保險
        · 熱區掃描只要找不到狀態或玩家物件，立刻退回全掃重新定位
    """

    done = Signal(object)                        # 一個 Scan

    def __init__(self) -> None:
        super().__init__()
        self._queue: list[tuple[int, MemoryScanner]] = []
        self._hot: dict[int, list] = {}          # pid → 上次命中的區塊
        self._full_at: dict[int, float] = {}     # pid → 上次全掃的時間
        self._inv: dict[int, int] = {}           # pid → 物品陣列表頭
        self._running = True

    def _inventory_head(self, pid: int, sc: MemoryScanner):
        """物品陣列表頭。★ 只讀快取，**絕不在這裡重找**。

        重找要跑 AOB 全掃，實測 1.7～2.1 秒／台。這條執行緒是怪物清單的關鍵路徑，
        五台排隊就是十秒的空窗（使用者回報的「搜尋會卡住」）。
        重找交給 InvWorker 那條獨立執行緒，找到再放進這個快取。
        """
        head = self._inv.get(pid)
        return head if head and inventory.is_valid(sc, head) else None

    def set_inventory_head(self, pid: int, head: int) -> None:
        """InvWorker 找到表頭後放進來，之後每輪掃描就直接帶出去。"""
        self._inv[pid] = head

    def request(self, pid: int, sc: MemoryScanner) -> None:
        if not any(p == pid for p, _ in self._queue):
            self._queue.append((pid, sc))

    def force_full(self, pid: int) -> None:
        """下一次掃這台時做全掃。

        熱區清單只在全掃時重建，堆積一變（怪重生＝新配置）就可能整塊漏掉；
        呼叫端發現「掃不到但物件還在」或「沒目標可挑」時用這個補救。
        呼叫端要自己節流（FULL_HUNT_GAP）—— 這裡照單全收。
        """
        self._full_at[pid] = 0.0

    def run(self) -> None:
        while self._running:
            if not self._queue:
                self.msleep(20)
                continue
            pid, sc = self._queue.pop(0)
            out = Scan(pid)
            try:
                now = time.monotonic()
                stats_vt = player.vtable_value(sc)   # 角色屬性物件，順便一起掃
                hot = self._hot.get(pid)
                if now - self._full_at.get(pid, 0.0) >= FULL_EVERY:
                    hot = None                   # 到期了，做一次全掃保險

                def scan(regions):
                    return entity.snapshot(
                        sc, should_stop=lambda: not self._running,
                        regions=regions, extra_vts=(stats_vt,))

                state, pobj, ents, found, extra = scan(hot)
                if hot is not None and (state is None or pobj is None):
                    # 熱區裡找不到本體 → 物件搬家了，立刻退回全掃
                    state, pobj, ents, found, extra = scan(None)
                    hot = None
                # ⚠ 只有全掃的結果可以拿來「重設」熱區。熱區掃描找到的區塊
                # 必然是熱區的子集，拿它覆寫的話熱區只會越縮越小 ——
                # 某一刻剛好沒怪，那塊就被丟掉，之後再也掃不到（直到下次全掃）。
                if hot is None:
                    self._full_at[pid] = now
                    if found:
                        self._hot[pid] = found
                out.state, out.player = state, pobj
                out.stats = player.pick(sc, extra.get(stats_vt, []))
                out.inv = self._inventory_head(pid, sc)
                # ★ is_monster 現在看實體的「種類」欄位（見 entity.OFF_KIND），
                #   NPC、別人的寵物、召喚物都會被排掉。
                # ⛔ 曾經改用「遊戲的怪物名稱表」過濾 —— 不可靠，別再試：
                #    那個 vtable 是通用的字串清單容器（同一個 vtable 底下還有
                #    購買紀錄、裝備名稱、角色名），而且**不會跟著換地圖更新**
                #    （換到沼澤之後，表裡還是前一張圖的名字）。
                out.mons = [e for e in ents if e.is_monster]
                out.pets = [e for e in ents if e.kind == 4]
                if state is None:
                    out.err = "找不到狀態物件（掃到 0 個或多個）"
                elif pobj is None:
                    out.err = "找不到玩家物件（掃到 0 個或多個）"
            except Exception as exc:               # noqa: BLE001
                out.err = f"掃描失敗：{exc}"
            if self._running:
                self.done.emit(out)

    def stop(self) -> None:
        self._running = False


class InvWorker(QThread):
    """專門找「物品指標陣列表頭」的執行緒（數藥水、找回程道具要用）。

    ★ 為什麼要獨立一條：找表頭要跑 AOB 全記憶體掃描，實測 **1.7～2.1 秒／台**。
      放在怪物清單那條執行緒上，五台排隊就是十秒的空窗 —— 使用者回報的
      「殺怪殺一殺有時候搜尋會卡住」就是這個。
      （跟 watcher.py 把定位拆成 _Locator 是同一個道理，那次也是最有效的一招。）

    表頭本身很穩：實測 40 秒、五台，0 次失效。所以這條執行緒平時幾乎沒事做，
    只有開場各找一次，之後失效才重找。
    """

    found = Signal(int, object)                  # pid, head

    def __init__(self) -> None:
        super().__init__()
        self._want: dict[int, MemoryScanner] = {}
        self._at: dict[int, float] = {}
        self._running = True

    def request(self, pid: int, sc: MemoryScanner) -> None:
        """請它找（或重找）某台的表頭。有冷卻，不會一直重跑全掃。"""
        if time.monotonic() - self._at.get(pid, -99.0) >= INV_RELOCATE_GAP:
            self._want[pid] = sc

    def run(self) -> None:
        while self._running:
            # ⚠ 用 popitem() 而不是「先檢查再 next(iter(...))」：`stop()` 是
            #   GUI 執行緒呼叫的，它 clear() 的瞬間如果卡在 iter 與 next 之間，
            #   會丟 StopIteration／RuntimeError 把這條執行緒炸掉 ——
            #   關程式時 wait() 就白等了。
            try:
                pid, sc = self._want.popitem()
            except KeyError:
                self.msleep(200)
                continue
            self._at[pid] = time.monotonic()
            head = None
            try:
                hits = aob.scan(sc, aob.SKILL_EXP_BALL, limit=4096,
                                should_stop=lambda: not self._running)
                head = inventory.locate(
                    sc, {a - inventory.ITEM_BALL_OFF for a in hits})
            except Exception:                    # noqa: BLE001
                head = None
            if self._running and head:
                self.found.emit(pid, head)

    def stop(self) -> None:
        self._running = False
        self._want.clear()


class CharFarmPage(QWidget):
    """單一分身的掛機介面。"""

    def __init__(self, pid: int, hwnd: int, title: str, sc: MemoryScanner,
                 on_scan, tgt: TargetWorker, keys: KeyWorker,
                 notifier: Notifier | None = None,
                 account: str = "", char_name: str = "",
                 mode: str = MODE_PACKET, prefix: str = "farm") -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        self.account = account
        self.char_name = char_name
        # 兩個分頁共用這個類別，只差攻擊方式；設定也要分開存（各自一份）。
        self.mode = mode
        self._prefix = prefix
        keys.mode = mode
        self._loading = True          # 載入設定期間不要反過來又存一次
        # 通知器每個分身一個，設定讀自己頁面上的那一列（使用者要求各自獨立）
        self._notifier = notifier or Notifier(
            self, "⚠ 自動掛機警報",
            lambda: ("telegram" if self.rb_tg.isChecked() else "sound",
                     self.tg_id.text()))
        # 三件事各自跑自己的執行緒，互不干擾：
        #   掃描更新清單（ScanWorker，全分身共用）
        #   寫入目標＋偵測死亡（TargetWorker，50 Hz）
        #   狂送技能鍵（KeyWorker，使用者設定的頻率）
        # 本頁只負責「挑要打誰」跟畫面。
        self._atk = tgt
        self._keys = keys
        tgt.died.connect(self._on_died)
        tgt.failed.connect(lambda msg: self._stop_with(f"⚠ 記憶體存取失敗：{msg}"))
        tgt.stale.connect(self._on_state_stale)
        self.state: int | None = None
        self.player: int | None = None           # 玩家物件位址（拿來讀自己的座標）
        self.stats: int | None = None            # 角色屬性基準（拿來讀 HP）
        self.inv: int | None = None              # 物品陣列表頭（藥水／回程道具用）
        self.mons: list[entity.Entity] = []
        self.pets: list[entity.Entity] = []      # kind=4（自動召喚認養用）
        self._on_scan = on_scan
        self._cur: entity.Entity | None = None   # 正在打的那隻
        self._kills = 0
        self._waiting = False      # 正在等重新掃描的結果
        self._wait_t = 0.0         # 等了多久（看門狗用，見 SCAN_STUCK_SECS）
        self._scan_lost = 0        # 掃描結果沒回來、被看門狗救回來幾次
        # 這台已經停用了（遊戲關掉／心跳丟例外）＝停用的原因字串，見 halt()。
        # ⚠ 停用之後 tick 什麼都不做 —— 一個已經不存在的行程沒什麼好讀的，
        #   而且會讓同一個例外每 10ms 重演一次。
        self._halted = ""
        self._gone_t = 0.0         # 距離上次確認「遊戲還在不在」過了多久
        self._scan_err = 0         # 套用掃描結果時丟例外幾次
        self._bad_scans = 0        # 連續幾拍掃描結果不可信（見 apply_scan）
        self._since_scan = 0.0     # 距離上次自動重掃過了多久
        self._stuck = 0.0          # 打不到也走不到的時間（卡住偵測）
        self._anchor = None        # 卡住偵測的錨點（淨位移超過就重設）
        self._dbg_t = 0.0          # 診斷紀錄的計時（見 _FARM_LOG）
        self._dbg_empty_t = 0.0    # 「挑不到目標」紀錄的節流（見 _pick_next）
        self._want_full = False    # 下一次掃描要求全掃（見 _ask_full）
        self._full_req_t = 0.0     # 補救全掃的節流（FULL_HUNT_GAP）
        self._path_pts = -1        # 尋路點數（-1=還沒算、1=直線通、>1=有地形）
        # ★ 「地形圖**這一拍親口說**跟目標之間直線可通」。只有它是 True 才敢
        #   把「走直線到目標」當成自己算好的路徑交給遊戲（見 _walk_toward）。
        #   讀不到地形圖時永遠是 False → **這一拍不走路**（不再退回遊戲的尋路）。
        self._line_clear = False
        # 地形圖讀不到的原因（空字串 = 有圖）。沒有圖就不走路，所以要看得到。
        self._no_grid = ""
        self._path_t = 0.0         # 距離上次重算路徑過了多久
        # 下一次重算要等多久（會依上一次 A* 的實際成本自我調節，見 tick）
        self._path_gap = PATH_GAP
        self._way: list[tuple[float, float]] = []   # 上次算出的繞路路徑點
        self._unreach = 0          # 連續幾次尋路算不出路徑
        self._hurt = False         # 這隻有沒有被我們打傷過（打傷了就不准換目標）
        self._push_in = False      # 打得到卻零傷害 → 正在貼身繞打（見 PUSH_IN_SECS）
        self._switch_t = 0.0       # 下一次可以評估「有沒有更近的怪」的時間
        # ★★ 我站的這一塊連通區有哪些格（terrain.Grid.reachable 泛洪的結果）。
        #   挑目標時用它把「對岸／島上／牆裡」的怪整個排除掉。
        #   算一次約 33ms，所以要快取：只有換地圖或自己跳到別的連通區才重算。
        self._reach: set | None = None
        self._reach_grid = None    # 上次泛洪是對哪一張地圖物件做的
        self._handoff_fail = False  # 這隻「交棒給客戶端走」失敗過了嗎
        self._handoff_t = 0.0      # 交棒之後多久沒真的接戰
        self._near_fail = 0        # 近距離直線走連續幾次沒位移（撞牆偵測）
        self._near_from = None     # 上一次直線走出發時的位置
        self._gone = 0             # 連續幾次掃描沒看到目標
        self._walked_ok = True     # 上次下移動指令有沒有成功
        self._moving = False       # 角色是不是正在走路（讀動畫狀態，見 tick）
        self._why = ""             # 沒在攻擊的原因（顯示在狀態列）
        # 看過「怪在幾格外掉血」的最大值 = 這個角色真正打得到的距離。
        # 0 = 還沒看過任何一次掉血 → 先走到貼臉（近戰唯一打得到的距離）。
        self._hp_t = 0.0           # 距離上次檢查自己的 HP 過了多久
        self._hp_prev = -1         # 上一拍的 HP（偵測「正在被打」用）
        self._hp_drop_t = -1e9     # 上次觀測到 HP 下降的時刻（見 _under_attack）
        self._gear_t = 0.0         # 距離上次檢查裝備耐久過了多久
        self._supply = False       # 正在跑「我們自己」的回程補給（背景執行緒，完全讓開）
        self._supply_t = 0.0       # 這一趟補給跑多久了
        self._supply_poll = 0.0    # 距離上次檢查補給進度過了多久
        # ★★★ 回程補給改成跑我們自己的 supply.run_full_supply（存倉庫→修裝→買水→趴趴GO回原地），
        #   不再交給天使精靈。整趟阻塞式，放背景執行緒跑，_supply_tick 每拍輪詢完成。
        self._supply_result = None  # 背景執行緒的結果 (ok, msg)；None = 還在跑
        self._supply_progress = ""  # 背景執行緒的最新進度字串（run_full_supply 的 say 回報）
        self._supply_scene = None  # 回程要跳回的地圖（記錄點；沒記錄點＝出發當下）
        self._supply_left = False  # 已經離開過那張地圖了嗎
        self._supply_pos = None    # 回程要跳回的座標（同上）
        # ★ 記錄點（回程補給／死亡回程最後要飛回的練功點）：(x, y, 場景編號)。
        #   按「開始掛機」時取**巡邏點**（_pick_home，2026-08-18 使用者要求，
        #   以前是記按下當下的地圖）；右鍵巡邏點「飛到這張圖」也會跟著改。
        self._home: tuple[float, float, int] | None = None
        # ★ 補給背景執行緒的把手：還活著就**絕不能**再開第二條（兩條會同時
        #   搶走位、各燒各的翼）。逾時停機只作廢結果，殺不掉它 —— 使用者重開
        #   掛機時要看這裡擋住（跟 produce_tab._sup_thread 同一套）。
        self._supply_thread: threading.Thread | None = None
        # 連續幾趟補給回來裝備**還是壞的**（≥2 就大聲停 —— 那不是暫時性失敗，
        #   多半是該城沒維修商／修裝一直失敗，重試只會每趟燒一張翼）。
        self._broken_trips = 0
        # 連續幾趟補給回來藥水**還是見底**（≥2 就大聲停 —— 買水一直買不進來
        #   多半是金幣不夠／背包滿，重試只會每趟燒一張翼；跟壞裝煞車同一套）。
        self._dry_trips = 0
        # ── 自動練技（_train_tick；跟掛機互斥，見 _on_train_toggle）──
        # 練技的「原地」：(x, y, 場景)。開練技後第一次讀到位置就記下來，
        # 補給那趟回程一律跳回這張圖 —— 不能記「出發當下」：上一趟若失敗把
        # 人留在城裡，下一趟就會把城當成原地（run_full_supply 預設行為的坑）。
        self._train_home: tuple[float, float, int] | None = None
        self._train_t = 0.0          # 練技心跳計時（TRAIN_GAP 一拍）
        self._train_supply = False   # 練技的補給趟進行中（背景執行緒）
        self._train_supply_t = 0.0   # 這趟跑多久了（SUPPLY_MAX_SECS 兜底）
        self._train_result = None    # 背景執行緒的結果 (ok, msg)；None=還在跑
        self._train_progress = ""    # 背景執行緒的最新進度（say 回報）
        self._train_gen = 0          # 第幾趟（作廢晚回來的結果）
        self._train_dry_trips = 0    # 連續幾趟回來藥水還是見底（≥2 大聲停）
        self._train_no_wing = 0      # 連續幾次確認沒回程道具（NO_RECALL_TRIES）
        self._train_lua_t = 0.0      # 上次為了建練習技能記錄退 Lua 的時刻
        # ── 自動換球（_ball_tick；2026-08-21 使用者要求）──
        self._ball_t = 0.0           # 心跳計時（BALL_GAP 一拍）
        self._ball_busy = False      # 換球背景執行緒進行中（換一次要等伺服器）
        self._ball_result = None     # 背景執行緒的結果 (成功?, 訊息, 球名, 左右)
        # 「備球不夠」的門閂。★ 它同時擋兩件事：重複通知、以及**重複花點數**
        #   —— 一個「都滿了」事件最多買一輪。球真的被換下去（不再全滿）
        #   之後自動重新武裝，跟收益監控「情況恢復就重新武裝」同一套。
        self._ball_said = False    # 這次「都滿了」已經花過點數（擋重複購買）
        self._ball_told = False    # 這次「都滿了」已經通知過失敗（擋重複吵）
        self._ball_off = ""          # 大聲停用的原因（有字就不再試，狀態列看得到）
        # ── 購買紀錄（2026-08-20 使用者要求）──
        # 每筆 = (時間戳, 商人標籤, 種類id, 實收數量, 花費或 None)。
        # 補給背景執行緒 append（純資料、不碰 Qt），按「購買紀錄」鈕時才畫表。
        # session 狀態不進 config（跟擊殺數、事件歷史同一類）。
        self._purchases: list[tuple[float, str, int, int, int | None]] = []
        # ⛔ 舊的 self._dry 通知門閂已刪：買得到的藥水見底**不通知**
        #   （2026-08-19 使用者：會自動補給還通知是吵人），只剩 _dry_stop
        #   那種「店裡沒賣、要停機」才通知，而停機本身就不會重複。
        self._supply_gen = 0       # 第幾趟補給（讓上一趟排的計時器自己作廢）
        self._recall_try = 0       # 回程第二段重試了幾次（見 _retry_recall）
        # 連續幾輪說「背包裡沒有回程道具」了（見 NO_RECALL_TRIES）。
        # ⚠ 這是「要停機」的門檻，不是重試上限 —— 讀到就歸零。
        self._no_recall = 0
        # 趴趴GO回程的狀態（見 _jump_step）。⚠ 三個都要在 _start_supply 歸零。
        self._jump_n = 0           # 這一趟送出去幾次了（顯示用，不設上限）
        self._jump_sent = None     # 最後一次送出時的 _supply_t（None = 還沒送過）
        self._jump_off = ""        # 趴趴GO走不通的原因（有字就不再試，標籤顯示它）
        self._robot_ours = False   # 精靈是我們開的（停手時要負責把自動攻擊關掉）
        # 「自動攻擊」看門狗（見 _af_tick）：掛機期間它必須是關的。
        self._af_t = 0.0           # 距離上次確認過了多久
        self._af_free = 0.0        # 這個時間點之前刻意不管（補給那一趟要開著）
        self._af_shut = 0          # 幫忙關掉過幾次（狀態列只講第一次）
        # ⛔ 血魔上限的暫態防護（player.MaxTracker）跟著「坐下休息」一起拿掉 ——
        #   它只是為了算血魔百分比，而百分比只有休息門檻在用。
        #   ⚠ 那個暫態本身是真的（掉血當下最大 HP 會跟著現值跑 1.3 秒，實測），
        #     類別留在 player.py，將來誰要用 max_hp 算百分比先看那裡。
        # 死亡回程模式（勾了「死亡自己回練功區」，角色死掉才會進，見 _death_tick）
        self._death = False        # 進行中（我們完全讓開，等復活）
        self._death_t = 0.0        # 死了多久（倒數 DEATH_REVIVE_SECS、超時判斷共用）
        self._death_poll = 0.0     # 距離上次檢查過了多久
        self._death_scene = None   # 要活回哪張地圖才接回自動戰鬥（記錄點優先）
        self._death_pos = None     # 死在哪（同圖多落點時挑最近的）
        self._death_try = 0.0      # 死亡滿幾秒才（再）送「回標記點」
        self._death_sent = False   # 至少送出去過一次了（狀態列顯示用）
        # 復活後的趴趴GO是什麼時候送出去的（_death_t 的值；None = 還沒送）。
        # ⚠ 不是 bool：等超過 JUMP_LAND_SECS 沒到就要當那一包沒生效、再送一次。
        self._death_jumped = None
        self._death_closed = False # 死亡選擇視窗已經關掉了（一次死亡只關一次）
        self._mover: move.Mover | None = None
        self._mover_failed = False   # 裝過一次失敗了就別每一拍重試
        self._castwatch = None       # 施放廣播監聽（首發＋補分身的 100% 確認用）
        self._cw_failed = False      # 裝失敗過就別每拍狂試（重開掛機會再試一次）
        self._walk_t = 0.0         # 距離上次下移動指令過了多久
        # 巡邏點：沒怪時依序走過去找怪（取代原本的單一「原點」）。
        # 每個點記 (x, y, 場景編號)；場景編號 None = 舊版存的、沒標記地圖。
        # ★ 座標在每張地圖都是從 0 開始算的格子，光看座標分不出是哪張圖 ——
        #   不記地圖就會拿 A 圖的點在 B 圖亂走（使用者要求：不同圖就不要去）。
        self._spots: list[tuple[float, float, int | None]] = []
        self._spot_i = 0           # 現在要去第幾個
        # ★★★ 地形圖（整張地圖的可走格，直接讀遊戲載的資料）。見 terrain.py
        #   走路與打怪的判斷都查它，**不再問遊戲的尋路**：
        #     · 直線檢查 0.003ms、近距離 A* 0.2~0.8ms（遊戲的尋路 5~6ms）
        #     · 而且**不必搶指令槽** —— 尋路要跟攻擊搶那個唯一的槽，
        #       搶不到時回 -1，我們就只能沿用上一次的舊答案（怪早就走掉了）。
        #   一份快取給導航與打怪共用。
        self._maps = terrain.Cache()
        # ★ 走去巡邏點的導航器（會繞路、會判斷到不了），見 navigate.py
        self._nav = navigate.Navigator(self._maps)
        # ⛔ 「走通過的巡邏路線」記憶（_routes）2026-08-10 刪除：它是探索式
        #   繞路的配件，而探索本身已經刪了。地形圖 A* 12~19ms 就給最短路，
        #   記一條走過的路只會更差。
        # ★ 自動分身：時間到了自動補 F12 的 buff（見 app/game/buff.py）
        self._buff = buff.AutoBuff(BUFF_KEY)
        self._buff_note = ""     # 上次顯示過的補 buff 訊息（沒變就不重畫）
        self._buff_read_t = 0.0  # 下一次可以讀快捷欄找分身技能的時間
        # ★ 自動召喚：F11 的召喚物不見了就重放（見 app/game/summon.py）
        self._summon = summon.AutoSummon()
        self._summon_note = ""
        self._summon_read_t = 0.0
        # 目前所在地圖。心跳每 SCENE_SAMPLE 秒更新一次（見 _refresh_scene）。
        self._scene: int | None = None
        self._scene_t = SCENE_SAMPLE    # 第一拍就讀
        self._scene_obj: int | None = None   # 靜態指標失效時掃到的物件，快取起來
        self._scene_scanned = False          # 全掃備援只做一次，不要每次重掃
        self._scene_try = 0.0                # 上次全掃的時間（見 SCENE_RELOCATE_GAP）
        # 自動巡迴換頻道。從目前這一頻出發繞一圈再回來（在 3 頻 → 4,5,1,2,3）。
        self._rot_t = 0.0            # 距離上次開始巡迴過了多久
        self._rot_seq: list[int] = []   # 這一輪還沒去的頻道（依序）
        self._rot_wait = 0.0         # 這一頻還要待多久才換下一個
        self._rot_settle = 0.0       # 剛換完，還在重連／重新定位，先別打怪
        self._rot_home = 0           # 出發時在哪一頻（只給顯示用）
        self._rot_max = 0            # 這台伺服器有幾個分流（開始巡迴時讀）
        self._rot_last = ""          # 上次顯示的巡迴文字（沒變就不重畫）
        # ⛔⛔ 「坐下休息」整組狀態（_rest／_rest_why／_sit_want／_hp_pct…）
        #   2026-08-09 依使用者要求**整個移除**。原話：
        #   「我用這程式是想練等快速，但如果需要休息、水跟不上，
        #     是不是不該打這裡」＋「我發現幾乎所有 BUG 都在這個坐下休息」。
        #   ★ 血魔跟不上有兩條既有的路接住，不必坐下：精靈自己吃藥水、
        #     藥水見底（≤robot.POTION_LOW）就自動跑回程補給。
        #   ⚠ **不要因為「以防萬一」把它加回來**：那條路上實際查到的失效模式
        #     至少三個 —— 收尾階段的正回饋循環（誤判有怪打我 → 不准坐 →
        #     繼續打 → 被反擊 → 更不准坐）、坐著被咬死、血量百分比被
        #     「最大 HP 暫時等於現值」的暫態騙成 100%。
        self._last_hp = -1
        # 已經打死（或判定走不過去）的實體 ID → 記下來的時間。
        # 怪死掉後物件不會馬上被回收，is_alive() 可能還是 true —— 沒這層擋著，
        # 換下一隻時會又挑到同一具屍體。
        # ⚠ 不能在每次重掃時清空：現在每秒都在刷新清單，一清就等於沒擋。
        #   改成保留 KILL_MEMORY 秒後自動淘汰（實體 ID 久了才可能被重用）。
        self._killed: dict[int, float] = {}
        # 「走不到」失敗過幾次（每隻怪各自算）—— 冷卻時間照這個翻倍。
        self._unreach_n: dict[int, int] = {}

        # ★ 內容放進可捲動區。**主視窗是固定 940x700**（見 main_window.py），
        #   分頁塞不下時 Qt 會硬把控制項壓到比最小尺寸還小 —— 那就是使用者
        #   看到的「字被切掉一半」。有捲軸就永遠不會壓，頂多要捲一下。
        #   其他分頁（記憶體掃描、收益監控、封包）本來就是這樣做的。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # ★ 垂直捲軸**永遠顯示**（2026-08-19 使用者回報「抖動」）：內容高度
        #   剛好卡在「要不要捲軸」的邊界時（狀態字一長、提示列忽隱忽現），
        #   捲軸出現→視口變窄→重新排版→捲軸消失→視口變寬→…來回振盪，
        #   看起來就是整頁在抖。捲軸固定住，視口寬度恆定，回路就斷了。
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setSpacing(6)

        # 通知列（最上面）。★ 每個分身各自一份設定，不共用
        # —— 使用者要求：不同分身可能要通知到不同的地方。
        nbar = QHBoxLayout()
        # ★ 通知總開關（使用者要求）。關掉之後所有掛機通知都不送 ——
        #   下面那些「藥水沒了」「裝備壞了」都受它管。
        # ⚠ 角色死亡本身**不通知**（使用者 2026-08-07 要求：這頁只通知
        #   藥水用完和裝備壞掉；死亡回程卡住停機那種「掛機停了」才通知）。
        self.notify_cb = QCheckBox("啟用通知")
        self.notify_cb.setChecked(True)
        # ⚠ tooltip 一律短（2026-08-19 使用者：太長太繁瑣，改簡單明瞭）。
        #   設計理由寫在程式註解／memory，不放進提示。
        self.notify_cb.setToolTip(
            "通知總開關：裝備壞掉、藥水用完、掛機出狀況才通知。\n"
            "關掉只是不通知，掛機該停還是會停。")
        self.notify_cb.toggled.connect(self._save_settings)
        nbar.addWidget(self.notify_cb)
        nbar.addSpacing(10)
        nbar.addWidget(QLabel("通知方式"))
        self.rb_sound = QRadioButton("音效警報")
        self.rb_tg = QRadioButton("Telegram")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_sound)
        grp.addButton(self.rb_tg)
        self.rb_sound.setChecked(True)
        nbar.addWidget(self.rb_sound)
        nbar.addWidget(self.rb_tg)
        self.tg_id = QLineEdit()
        self.tg_id.setPlaceholderText("Telegram 群組/房間 ID")
        self.tg_id.setFixedWidth(220)
        nbar.addWidget(self.tg_id)
        self.test_btn = QPushButton("測試通知")
        self.test_btn.setToolTip("立刻送一則測試通知，確認設定會不會通。")
        self.test_btn.clicked.connect(
            lambda: self.notify("這是一則測試通知。"))
        nbar.addWidget(self.test_btn)
        nbar.addStretch(1)
        root.addLayout(nbar)

        # ★ 主開關一列放在所有分類方框**上面** —— 它管的是整個頁面，
        #   塞進任何一個分類裡都不對，而且要一眼就找得到。
        #   同一列右邊掛「已擊殺 N 隻」＋小歸零鈕（使用者要求）。
        self.run_cb = QCheckBox("開始掛機")
        self.run_cb.setStyleSheet("font-weight: bold;")
        self.run_cb.setToolTip(
            "自動打「選中怪物」裡的怪，打死接下一隻。\n"
            "取消勾選立刻停止。")
        self.run_cb.toggled.connect(self._on_toggle)
        run_bar = QHBoxLayout()
        run_bar.addWidget(self.run_cb)
        # ★ 擊殺數放在主開關旁邊（使用者要求）。**不存設定** —— 開程式
        #   歸零；停止／再開始掛機**不**歸零，只有旁邊的小按鈕會歸零。
        self.kills_lbl = QLabel("已擊殺 0 隻")
        run_bar.addSpacing(16)
        run_bar.addWidget(self.kills_lbl)
        self.kills_reset_btn = QPushButton("歸零")
        self.kills_reset_btn.setToolTip("把擊殺數歸零（不影響掛機）。")
        # 小顆就好（使用者要求），但要照字型量寬高 —— 寫死 44x22 會把字
        # 砍半。⚠ 內距 +20 實測還是被切（Windows 原生按鈕左右各吃 ~10px
        # 邊框＋焦點框），使用者要求再加長一點 → +32。
        fm = self.kills_reset_btn.fontMetrics()
        self.kills_reset_btn.setFixedSize(
            fm.horizontalAdvance("歸零") + 32, fm.height() + 8)
        self.kills_reset_btn.clicked.connect(self._reset_kills)
        run_bar.addSpacing(4)
        run_bar.addWidget(self.kills_reset_btn)
        # ★ 自動練技（2026-08-19 使用者要求）：練技本體交給官方精靈的
        #   「原地重複練習技能」，我們只看藥水、跑補給、把開關顧好。
        #   跟「開始掛機」互斥（都要指揮同一隻角色）；放主開關同一列 ——
        #   它跟掛機一樣是整頁層級的模式，而且這一列有現成的空位（不加高）。
        run_bar.addSpacing(24)
        self.train_cb = QCheckBox("自動練技")
        self.train_cb.setToolTip(
            "開精靈主開關＋輔助頁「原地重複練習技能」原地練技。\n"
            f"藥水剩 ≤{robot.POTION_LOW} 顆自動回城買水（只找補給商），"
            "買完飛回原地圖續練。\n"
            "精靈頁放的是商店沒賣的藥水：通知並自動關閉。")
        self.train_cb.toggled.connect(self._on_train_toggle)
        run_bar.addWidget(self.train_cb)
        # ★ 購買紀錄（2026-08-20 使用者要求）：回程補給跟商人買了什麼的流水帳，
        #   按了開一張表（時間／商人／物品／數量／花費＋總額）。
        run_bar.addSpacing(24)
        self.buy_log_btn = QPushButton("購買紀錄")
        self.buy_log_btn.setToolTip(
            "回程補給跟商人買了什麼：時間、物品、數量、花費與總額。\n"
            "只記這次開著工具箱期間的（重開清空）。")
        # 跟「歸零」同一套量法：照字型量寬高，別讓原生按鈕邊框把字切掉。
        fm2 = self.buy_log_btn.fontMetrics()
        self.buy_log_btn.setFixedSize(
            fm2.horizontalAdvance("購買紀錄") + 32, fm2.height() + 8)
        self.buy_log_btn.clicked.connect(self._show_purchases)
        run_bar.addWidget(self.buy_log_btn)
        # ★ 自動換球（2026-08-21 使用者要求）：飾品欄的經驗球滿了就自動換上
        #   背包裡的備球，沒備球就通知一次。判斷全走遊戲自己的資料
        #   （範本分類＋上限），三族 32 種球都認得 —— 見 app/game/balls.py。
        run_bar.addSpacing(24)
        self.ball_cb = QCheckBox("自動換球")
        self.ball_cb.setToolTip(
            "飾品欄兩顆經驗球都滿了才一起換（只有一顆滿不動作）。\n"
            "背包備球不夠會去天使商城買，花點數，每次都通知。\n"
            "買不到只通知一次，掛機照常繼續。")
        self.ball_cb.toggled.connect(self._on_ball_toggle)
        run_bar.addWidget(self.ball_cb)
        # ★ 臨時測試鈕（memory 的 test-via-button）：換球封包還沒實機驗過，
        #   給使用者當場按一下確認。驗過就可以拆掉這顆。
        self.ball_test_btn = QPushButton("測試換球")
        self.ball_test_btn.setToolTip(
            "當場試一次換球，結果直接顯示。\n"
            "背包有備球就真的換上；沒有就把左右兩顆對調（再按一次會換回來）。")
        fm3 = self.ball_test_btn.fontMetrics()
        self.ball_test_btn.setFixedSize(
            fm3.horizontalAdvance("測試換球") + 32, fm3.height() + 8)
        self.ball_test_btn.clicked.connect(self._test_ball_swap)
        run_bar.addSpacing(4)
        run_bar.addWidget(self.ball_test_btn)
        run_bar.addStretch(1)
        root.addLayout(run_bar)

        # ★ 依分類分組（使用者要求）：原本 5 條平鋪的橫列很難一眼看懂哪個是
        #   哪個，改成有標題的方框、排成兩欄，垂直空間也省下來給下面的清單。
        # ★ 「周圍怪物」下面有「掃描周圍怪物」鈕（2026-08-11 使用者要求）：
        #   沒在掛機時清單不會自己更新，要按才掃；掛機中照舊自動刷新。
        #   ⚠ 不必先掃過才能開始掛機 —— 按下開始的當下就會排一次掃描。
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        # ── 攻擊 ────────────────────────────────────────────
        g_atk = QGroupBox("攻擊")
        a = QHBoxLayout(g_atk)
        a.addWidget(QLabel("技能鍵"))
        # ★ 可多選（使用者要求）：勾幾個 F 鍵，攻擊照 F1→F12 順序輪流放。
        #   鍵上放什麼是直讀快捷欄的（quickbar.py）：空格／物品格自動略過
        #   不進循環、掛機中換技能幾秒內跟上。
        #   用按鈕＋下拉選單裝 12 個勾選框 —— 一整排 12 個會把這條列撐爆
        #   （主視窗固定 940 寬）。
        #   ⚠ 勾選框要包在 QWidgetAction 裡：點了選單不會關，才能一次勾多個。
        self.key_btn = QToolButton()
        self.key_btn.setPopupMode(QToolButton.InstantPopup)
        self.key_btn.setToolTip(
            "勾要輪流放的技能鍵（可多選），照 F1→F12 順序施放。\n"
            "放什麼直接讀遊戲快捷欄：空格、物品自動略過。")
        km = QMenu(self.key_btn)
        self._key_cbs: list[tuple[QCheckBox, int, str]] = []
        for label, vk in SKILL_KEYS:
            cb = QCheckBox(label)
            cb.setChecked(vk == DEFAULT_KEY)
            cb.setStyleSheet("padding: 4px 10px;")
            act = QWidgetAction(km)
            act.setDefaultWidget(cb)
            km.addAction(act)
            self._key_cbs.append((cb, vk, label))
        # ⚠ 先設好初始勾選、再接訊號 —— 建構期間就觸發 _keys_changed 的話，
        #   它用到的 run_cb 等元件都還沒生出來。
        for cb, _, _ in self._key_cbs:
            cb.toggled.connect(self._keys_changed)
        # 點開選單的當下把每個鍵標上「現在放什麼」：F1（電擊術Ⅳ）。
        # GUI 執行緒自己開一個 Reader，不跟攻擊執行緒共用（省得搶快取）。
        self._qb_ui = quickbar.Reader(self.sc)
        km.aboutToShow.connect(self._label_keys)
        self.key_btn.setMenu(km)
        self._sync_key_btn()
        a.addWidget(self.key_btn)
        a.addSpacing(10)
        # ★ 首次攻擊（使用者要求）：每一隻怪的第一下一定要是這一招。
        a.addWidget(QLabel("首次攻擊"))
        self.open_box = _NamedKeyBox(self._label_keys)
        self.open_box.setToolTip(
            "每隻怪的第一下固定先放這個鍵，確認放出去才輪其他招。\n"
            "只當開場用，之後的輪迴不含它；SP 不夠會自動跳過。")
        self.open_box.addItem("不指定", 0)
        for label, vk in SKILL_KEYS:
            self.open_box.addItem(label, vk)
        # 字會變長（F3（破甲劈擊Ⅳ）），寬度**固定住**才不會把整條列撐開；
        # 展開的清單另外放寬，長名字才看得完整（見 [[qt-ui-pitfalls]]）。
        self.open_box.setFixedWidth(150)
        self.open_box.view().setMinimumWidth(240)
        self.open_box.currentIndexChanged.connect(self._opener_changed)
        a.addWidget(self.open_box)
        # ⛔ 「每隔幾秒」的輸入框拿掉了（使用者要求固定，不給輸入）。
        #    出手節奏是 ROUND_GAP（一輪放完隔 0.1 秒），這裡設的是執行緒節拍，
        #    要比它細才切得出來。
        self._keys.set_interval(STRIKE_TICK)
        a.addSpacing(10)
        # ★ 只打王：勾起來就**完全不看「選中怪物」的名字**，改成看種類 ID
        #   是不是王（見 app/game/monsters.py）。名字比對分不出來 ——
        #   有 20 種怪同名卻一個是王一個不是（哥布林幹部 78 / 663王…）。
        self.boss_cb = QCheckBox("只打王")
        self.boss_cb.setToolTip(
            "只打周圍的「王」，完全不看「選中怪物」名單。\n"
            "⚠ 不分等級，多硬的王都會打。")
        a.addWidget(self.boss_cb)
        a.addSpacing(10)
        # ★ 自動分身：時間快到了自動補 F12
        self.buff_cb = QCheckBox("自動分身")
        self.buff_cb.setToolTip(
            "把分身技能放在遊戲快捷欄的 F12（只認 F12）。\n"
            "時間快到自動補放，並確認伺服器真的有受理。\n"
            "不用開掛機，單獨勾也會動。")
        self.buff_cb.toggled.connect(self._save_settings)
        a.addWidget(self.buff_cb)
        a.addSpacing(10)
        # ★ 自動召喚：F11 的召喚物不見了／死了就自動重放（見 app/game/summon.py）
        self.summon_cb = QCheckBox("自動召喚")
        self.summon_cb.setToolTip(
            "把召喚技能放在遊戲快捷欄的 F11（只認 F11）。\n"
            "召喚物真的沒了才自動重召（走遠、換地圖不會誤判）。\n"
            "不用開掛機，單獨勾也會動。")
        self.summon_cb.toggled.connect(self._save_settings)
        a.addWidget(self.summon_cb)
        a.addStretch(1)
        # ★ 攻擊群組跨滿一列 ——「移動與巡邏」整個群組已移除（見下）。
        grid.addWidget(g_atk, 0, 0, 1, 2)

        # ⛔ 「移動與巡邏」群組已移除（2026-08-13 使用者指定，兩個勾都多餘）：
        #    ·「自動走過去」→ 走位**永遠開**。當初獨立成開關的理由是「沒勾就
        #      不在遊戲裡放程式碼」（2026-08-02 移動是第一個要跳板的功能）——
        #      現在封包攻擊/分身/召喚/補給全都要跳板，理由名存實亡；而且挑目標
        #      本來就要對每隻怪跑地形尋路（走不到的不挑），走位是掛機的地基。
        #    ·「沒怪去巡邏點」→ **有設巡邏點就巡，沒設就不巡**（使用者原話：
        #      「不想巡邏就別設定巡邏點」）。行為說明搬去「巡邏點」清單的提示。
        #    舊設定檔裡的 "move" / "patrol" / "back" 鍵直接忽略，不必清掉。
        # ⛔ 「接戰距離」輸入框更早移除（使用者要求）——停多遠照技能射程自動算
        #    （skills.range_of）。雪狐踩過：留在遠程預設 11，近戰射程 1，
        #    站在 11 格外空打（見 [[melee-attack-range]]）。舊 "range" 鍵忽略。

        # ⛔ 這裡以前是「坐下休息」（血/魔低於門檻就坐下回滿再繼續）。
        #   **2026-08-09 使用者要求整個移除**，原話：「我用這程式是想練等快速，
        #   但如果需要休息、水跟不上，是不是不該打這裡」。
        #   ★ 血魔真的跟不上有兩條既有的路接住，不需要坐下：
        #     · 精靈自己會吃補血／補魔藥水（「天使輔助精靈」那頁設的）
        #     · 藥水見底（≤robot.POTION_LOW）→ 自動回程補給（2026-08-19 起
        #       全自動，沒有 HP／MP 勾選）
        #   ⚠ 使用者回報「幾乎所有 BUG 都在坐下休息」，實際查到的至少三個都在
        #     那條路上（收尾正回饋循環、坐著被咬、血量百分比被上限暫態騙），
        #     移除等於一次收掉一整類失效模式。

        # ── 巡迴換頻道 ─────────────────────────────────────
        # ★ 從目前這一頻出發繞一圈再回到原本那一頻。
        #   在 3 頻就是 3 → 4 → 5 → 1 → 2 → 3。切換靠 app/game/channel.py
        #   （跟選定怪物同一個遊戲函式，只差種類碼）。
        g_rot = QGroupBox("巡迴換頻道")
        rot_v = QVBoxLayout(g_rot)
        r = QHBoxLayout()
        self.rot_cb = QCheckBox("自動換頻")
        self.rot_cb.setToolTip(
            "依序換過每一頻再回到原本那一頻，停留期間照常掛機。")
        r.addWidget(self.rot_cb)
        r.addWidget(QLabel("每"))
        self.rot_every = QDoubleSpinBox()
        self.rot_every.setRange(1.0, 600.0)
        self.rot_every.setSingleStep(5.0)
        self.rot_every.setDecimals(0)
        self.rot_every.setValue(30.0)
        fit_spin(self.rot_every)
        self.rot_every.setToolTip("隔多久開始新的一輪巡迴（從上一輪開始算）。")
        r.addWidget(self.rot_every)
        r.addWidget(QLabel("分一輪，每頻"))
        self.rot_stay = QDoubleSpinBox()
        self.rot_stay.setRange(5.0, 3600.0)
        self.rot_stay.setSingleStep(10.0)
        self.rot_stay.setDecimals(0)
        self.rot_stay.setValue(60.0)
        fit_spin(self.rot_stay)
        self.rot_stay.setToolTip("換到一頻之後待多久才換下一頻（照常打怪）。")
        r.addWidget(self.rot_stay)
        r.addWidget(QLabel("秒"))
        r.addStretch(1)
        self.rot_lbl = _NoteLabel()
        rot_v.addLayout(r)
        rot_v.addWidget(self.rot_lbl)
        # ⚠ 「坐下休息」拆掉之後 (1,0) 空了出來 —— 這裡改成**跨兩欄**，
        #   而不是留一個空格子（左邊空一大塊很難看，右欄的寬度又是
        #   「移動」那個方框撐出來的，收不掉）。跟底下的「回程補給」一致。
        grid.addWidget(g_rot, 1, 0, 1, 2)

        # ── 回程補給（整列）─────────────────────────────────
        # ★ 條件到了就跑我們自己的 supply.run_full_supply（回城→存倉→修裝→
        #   買水→回戰場），補完再自己接回來。打怪還是我們的掛機在做。
        # ★ 觸發開關**不看遊戲裡精靈自己的回城勾選**（使用者要求），
        #   這樣我們的觸發跟官方互相獨立。
        g_sup = QGroupBox("回程補給")
        sup_v = QVBoxLayout(g_sup)
        c = QHBoxLayout()
        self.sup_gear_cb = QCheckBox("裝備壞掉")
        self.sup_gear_cb.setToolTip(
            "身上穿的任何一件裝備耐久剩 1 就自動回城補給，"
            "補完飛回記錄點接著打。")
        # ★ 這幾個開關走 _on_robot_pref（不是單純 _save_settings）——
        #   勾了就要把精靈補給頁的設定推到位（見 robot.apply_prefs
        #   的 supply），掛機中才勾也要立刻生效。
        self.sup_gear_cb.toggled.connect(self._on_robot_pref)
        c.addWidget(self.sup_gear_cb)
        c.addSpacing(10)
        # ★ 藥水補給**全自動、沒有開關**（2026-08-19 使用者要求；原本的
        #   「HP 藥水沒了」「MP 藥水沒了」兩個勾選框已刪）：精靈頁放的藥水
        #   剩 ≤robot.POTION_LOW 顆就回城，買到負重 95%、HP/MP 數量對齊；
        #   補給店沒賣的藥水見底 → 通知＋天使之翼回城＋停止掛機（_dry_stop）。
        pot_lbl = QLabel(f"藥水剩 ≤{robot.POTION_LOW} 顆自動補給")
        pot_lbl.setToolTip(
            f"精靈頁放的藥水剩 {robot.POTION_LOW} 顆以下就自動回城補給。\n"
            "買到負重 95%，HP/MP 數量差不多。\n"
            "商店沒賣的藥水（如活動藥水）：通知＋天使之翼回城＋停止掛機。")
        c.addWidget(pot_lbl)
        c.addSpacing(10)
        # ★ 不等精靈自己走回來，時間到就用遊戲的「天使趴趴GO」直接傳回去。
        self.sup_jump_cb = QCheckBox("用天使趴趴GO回地圖")
        self.sup_jump_cb.setToolTip(
            f"補給後 {JUMP_BACK_SECS / 60:.0f} 分鐘直接用天使趴趴GO"
            "飛回記錄點，不等走路。\n"
            "傳送費用、等級限制跟手動用趴趴GO一樣。")
        self.sup_jump_cb.toggled.connect(self._on_robot_pref)
        c.addWidget(self.sup_jump_cb)
        c.addSpacing(10)
        # ★ 死亡自己回練功區：死了等 3 秒送「回標記點」封包復活
        #   （app/game/revive.py，跟手點死亡視窗那顆按鈕完全一樣）→
        #   復活在標記點（城裡）→ 趴趴GO傳回死掉那張地圖 → 接回打怪。
        #   開始掛機時把精靈「陣亡時自動復活」**關掉** —— 精靈搶先把人
        #   復活回城的話，「回標記點」就沒得點了。
        self.sup_revive_cb = QCheckBox("死亡自己回練功區")
        self.sup_revive_cb.setToolTip(
            "死了自動復活（回標記點）再飛回記錄點接著打。\n"
            "沒勾的話角色死亡就停止掛機。")
        self.sup_revive_cb.toggled.connect(self._on_robot_pref)
        c.addWidget(self.sup_revive_cb)
        c.addStretch(1)
        # ★ 趴趴GO 的倒數：補給觸發之後還有多久會傳回原地圖；死亡回程時
        #   也用同一個位置顯示。平常是空的（整列收起來，不佔高度）。
        # ⚠ 本來擺在這一列最右邊，但它一有字方框就變寬 → 跑版。改放第二列，
        #   而且用 _NoteLabel（不列入寬度計算、過長截斷）。
        self.jump_lbl = _NoteLabel()
        sup_v.addLayout(c)
        sup_v.addWidget(self.jump_lbl)
        grid.addWidget(g_sup, 2, 0, 1, 2)

        # ⛔ 不要把兩欄硬拉成一樣寬（setColumnStretch(0,1)+(1,1)）——
        #    當年「坐下休息」需要 499px、「攻擊」只要 413px，硬均分會讓前者被
        #    切掉半個字。那個方框雖然已經移除，規則照舊：讓每一欄照自己的
        #    內容決定寬度就好。
        # 每個方框的上下留白縮一點 —— 主視窗固定 940x700，整頁高度很吃緊，
        # 預設留白會讓整頁多出一條垂直捲軸。
        a.setContentsMargins(12, 6, 12, 6)
        # 有第二列提示字的三個方框：留白一律交給外層那個 VBox，內層那列歸零，
        # 兩列之間只留 2px —— 不然外層預設留白 + 內層 12/6 疊起來，
        # 光是留白就多出快 20px（整頁高度很吃緊，會冒出垂直捲軸）。
        for _outer, _inner in ((rot_v, r), (sup_v, c)):
            _outer.setContentsMargins(12, 6, 12, 6)
            _outer.setSpacing(2)
            _inner.setContentsMargins(0, 0, 0, 0)
        root.addLayout(grid)

        # 三個清單。★ 寬度一律照「內容需要多少」給，不寫死 ——
        # 使用者要求所有文字都要完整顯示，不能被切掉。
        # 一律只顯示中文名字 —— 比對也是用名字，所以手動打字才會通。
        panes = QHBoxLayout()

        left = QGroupBox("選中怪物")
        lv = QVBoxLayout(left)
        self.picked = QListWidget()
        self.picked.setMinimumHeight(NEAR_HEIGHT)
        self.picked.setMaximumHeight(NEAR_MAX)
        self.picked.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.picked)
        lv.addWidget(self.picked)
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("手動輸入後按 Enter")
        self.manual.returnPressed.connect(self._add_manual)
        lv.addWidget(self.manual)
        # 「選中怪物」放得下最長的怪物名（實際資料裡最長 8 個字）
        fit_list(left, self.picked, "曼陀羅怪菇菌絲體")
        panes.addWidget(left)

        # 中間的刪除鈕：把「選中怪物」裡選起來的移除
        midw = QWidget()
        mid = QVBoxLayout(midw)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.addStretch(1)
        # 用 ASCII 的 X，不要用 ✕ —— 部分中文字型沒有那個字形，會變成豆腐。
        # ⚠ 還要覆寫 padding：主題給 QPushButton 的是 `padding: 6px 14px`，
        #   左右各 14px 就吃掉 28px，小按鈕會完全看不到文字（踩過）。
        self.del_btn = QPushButton("X")
        self.del_btn.setFixedSize(32, 32)
        self.del_btn.setStyleSheet("padding: 0;")
        self.del_btn.setToolTip("把「選中怪物」裡選起來的項目刪掉")
        self.del_btn.clicked.connect(self._remove_picked)
        mid.addWidget(self.del_btn)
        mid.addStretch(1)
        panes.addWidget(midw)

        right = QGroupBox("周圍怪物")
        rv = QVBoxLayout(right)
        self.near = QListWidget()
        self.near.setMinimumHeight(NEAR_HEIGHT)
        self.near.setMaximumHeight(NEAR_MAX)
        self.near.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.near)
        # ⚠ 用 UserRole 的名字，不要用顯示文字 —— 王的前面有「👑 」字首。
        self.near.itemClicked.connect(
            lambda it: self._add_name(it.data(Qt.UserRole) or it.text()))
        rv.addWidget(self.near)
        # ★ 使用者要求（2026-08-11）：**沒在掛機時改成按按鈕才掃**，
        #   不要一直刷。掛機中仍然自動刷新 —— 見 tick 裡那段說明。
        self.scan_btn = QPushButton("掃描周圍怪物")
        self.scan_btn.setToolTip(
            "掃一次周圍有哪些怪。掛機中會自動刷新，不用按。")
        self.scan_btn.clicked.connect(self._scan_now)
        rv.addWidget(self.scan_btn)
        # 「周圍怪物」多一個「【王】」字首
        fit_list(right, self.near, "【王】曼陀羅怪菇菌絲體")
        panes.addWidget(right, 1)

        # 巡邏點：沒怪時依序走過去找怪（取代原本只有一個的「原點」）
        spot = QGroupBox("巡邏點")
        # ★ 沒有開關（2026-08-13）：**有設點就會巡、不想巡就別設**。
        #   行為說明放在群組的滑鼠提示（原本在「沒怪去巡邏點」勾選框上）。
        spot.setToolTip(
            "沒怪打時依序走這些點找怪；不想巡邏就把點全部刪掉。\n"
            "只走目前這張地圖的點。在點上按右鍵可用趴趴GO飛過去。")
        # 每一列是「編號. 地圖名 (x, y)」，最長的地圖名有 7 個字
        # （專家級遺落之地／史萊姆晴空牧場）—— 太窄會被切掉。
        sv = QVBoxLayout(spot)
        self.spot_list = QListWidget()
        self.spot_list.setMinimumHeight(NEAR_HEIGHT)
        self.spot_list.setMaximumHeight(NEAR_MAX)
        self.spot_list.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.spot_list)
        # ★ 右鍵選單：「用天使趴趴GO飛到這張圖」（2026-08-18 使用者要求）。
        self.spot_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.spot_list.customContextMenuRequested.connect(self._spot_menu)
        sv.addWidget(self.spot_list)
        srow = QHBoxLayout()
        # ⚠ 字別太長：主題給按鈕的 padding 是左右各 14px，
        #   「加入目前位置」要 108px，加上 X 鈕就超出區塊寬度被切掉（踩過）。
        add_btn = QPushButton("加入位置")
        add_btn.setToolTip("把角色現在站的位置加進巡邏點。")
        add_btn.clicked.connect(self._add_spot)
        srow.addWidget(add_btn)
        # 用 ASCII 的 X，不要用 ✕（部分中文字型沒有那個字形會變豆腐）
        rm_btn = QPushButton("X")
        rm_btn.setFixedSize(32, 32)
        rm_btn.setStyleSheet("padding: 0;")
        rm_btn.setToolTip("刪掉選起來的巡邏點")
        rm_btn.clicked.connect(self._remove_spots)
        srow.addWidget(rm_btn)
        sv.addLayout(srow)
        # ★ 記錄點顯示（2026-08-18 使用者要求）：回程補給／死亡回程飛回這裡。
        self.home_lab = QLabel("記錄點：－")
        self.home_lab.setToolTip(
            "回程補給／死亡回程最後會飛回這個點的地圖。\n"
            "開始掛機時取巡邏點；右鍵「飛到這張圖」也會更新它。")
        sv.addWidget(self.home_lab)
        # 「巡邏點」每列是「編號. 地圖名 (x, y)」，地圖名最長 7 個字
        # （專家級遺落之地／史萊姆晴空牧場），座標最多各 3 位數
        fit_list(spot, self.spot_list, "10. 專家級遺落之地 (242, 178)")
        panes.addWidget(spot)
        root.addLayout(panes)

        # ⚠ 這一行本來是 setWordWrap(True)：「掛機中：只打「A、B、C」　精靈：…
        #   　⚠ 勾的鍵上沒有技能」這種長訊息會折成兩三行，整頁就跟著變高
        #   （固定 700 的視窗立刻多一條捲軸，捲軸又把寬度吃掉 → 跑版）。
        #   改成單行截斷，完整內容在滑鼠提示裡。**不隱藏**（它一直有字，
        #   收起來會讓下面的東西上下跳）。
        self.status = _NoteLabel(hide_when_empty=False)
        self.status.setText("尚未掃描")
        root.addWidget(self.status)
        root.addStretch(1)

        self._load_settings()
        self._loading = False
        self._wire_saving()

    # ------------------------------------------------------------------
    # -- 選中怪物清單 --------------------------------------------------
    def wanted(self) -> list[str]:
        return [self.picked.item(i).text() for i in range(self.picked.count())]

    def _add_name(self, name: str) -> None:
        name = name.strip()
        if name and name not in self.wanted():
            self.picked.addItem(name)

    def _add_manual(self) -> None:
        self._add_name(self.manual.text())
        self.manual.clear()

    def _remove_picked(self) -> None:
        for it in self.picked.selectedItems():
            self.picked.takeItem(self.picked.row(it))

    # ------------------------------------------------------------------
    # -- 自動巡迴換頻道 --------------------------------------------------
    def _switch_channel(self, n: int) -> bool:
        """換到第 n 頻，並把所有快取的位址作廢。

        ⚠⚠ **作廢位址是必要的，不是保險**：換頻道會斷線重連，狀態物件／玩家
          物件／角色屬性／物品陣列全部搬家。舊位址還留著的話，寫入執行緒每
          20ms 就會往一塊已經是別人的記憶體寫目標 ID —— 那是在亂改遊戲的堆積。
        """
        if not self._ensure_mover():
            return False
        self._keys.set_on(False)          # 先停手，別對著要消失的目標送封包
        self._atk.hold_off()
        self._cur = None
        if not channel.switch(self._mover, n, self._rot_max):
            return False
        self._drop_cached_addrs()
        return True

    def _drop_cached_addrs(self) -> None:
        """傳送／重連之後把所有快取的位址作廢（換頻道、回程都要）。

        ⚠⚠ **這是必要的，不是保險**：斷線重連或換地圖之後，狀態物件／玩家
          物件／角色屬性／物品陣列全部搬家。舊位址還留著的話，寫入執行緒每
          20ms 就會往一塊已經是別人的記憶體寫目標 ID —— 那是在亂改遊戲的堆積。
        """
        # ⚠ 先叫寫入執行緒停手，再把位址清掉 —— 順序不能反。
        #   目前每個呼叫端都有自己先 hold_off()，但那是「剛好都有做」，
        #   不是這支保證的。放這裡才是真的保證。
        self._atk.hold_off()
        self._keys.set_on(False)
        self.state = self.player = self.stats = self.inv = None
        # ★ 怪物清單整份過期（換頻道／傳送之後都是新場景的怪）。
        #   ⚠ 不是為了防打錯怪 —— 那條由 `entity.read_live()` 的 vtable＋eid
        #   驗證擋著（見 _apply_scan 裡同一段說明）。清掉是免得挑目標白跑
        #   一趟死清單、以及「周圍怪物」停在舊圖的名字上。
        self.mons = []
        self.pets = []
        self._keys.eid = None
        self._keys.stats = None
        self._keys.pf = None
        self._killed.clear()              # 換頻＝整批怪都換了，冷卻名單沒意義
        self._unreach_n.clear()
        self._scene_obj = None            # 場景物件也會搬家
        self._scene_scanned = False
        self._scene_try = 0.0             # 剛傳送完，允許立刻重新定位一次
        self._since_scan = SCAN_NOW       # 重連完立刻重掃

    def _on_state_stale(self) -> None:
        """寫入執行緒發現手上的狀態物件位址已經失效（vtable 對不上）。

        ★ 這是**最後一道防線**：`apply_scan()` 那邊已經會在位址一變就叫它停手，
          但物件搬家到下一次掃描之間還是有空窗，而寫入執行緒每 20ms 跑一次。
          它自己驗出來就自己停，這裡只負責把其餘快取一起作廢並馬上重掃。
        """
        self._keys.set_on(False)
        self._cur = None
        self._drop_cached_addrs()
        self.status.setText("位址失效（換地圖／重生／重連？）→ 重新定位中…")

    def _fire_recall(self) -> None:
        """觸發的第二段：真的把回程道具用掉，然後排定「關自動攻擊」。

        ★★ 送之前先看**地圖是不是已經自己變了**：開了自動攻擊之後，官方
          客戶端可能已經按它自己的設定把人傳回城了。那時我們再送一次回程
          只會多花一個道具、而且跟官方的流程打架 —— 直接跳過就好。
        """
        say = self.status.setText

        if not (self._mover is not None and self._mover.active) or not self.inv:
            # ★ 跳板重掛中／背包表頭剛搬家都是幾秒內會好的事 → 重試，
            #   別像以前一樣放著不管（補給會吊到 10 分鐘超時才停）。
            if self._retry_recall("⚠ 回程暫時送不出去（跳板或背包位置變了）"):
                return
            self._end_supply("⚠ 回程送不出去（跳板或背包位置一直讀不到）",
                             stop=True)
            return
        now = scene.current(self.sc, allow_scan=False)
        here = now.id if now else None
        if (self._supply_scene is not None and here is not None
                and not scene.same_map(here, self._supply_scene)):
            self._supply_left = True        # 已經在外面了，等它回來就好
            self._drop_cached_addrs()
            self._schedule_af_off()
            say(f"★ 客戶端已自己回程（現在在 {now}）→ 略過我們的回程")
            return
        ok, msg = robot.do_recall(self._mover, self.sc, self.inv)
        if ok is None and self._retry_recall(f"⚠ {msg}"):
            return                          # 暫時性失敗 → 已排重試
        if not ok:
            self._end_supply(f"⚠ 交棒失敗：{msg}", stop=True)
            return
        self._drop_cached_addrs()
        self._schedule_af_off()
        say("🔧 已交給天使精靈跑補給　" + msg)

    def _retry_recall(self, note: str) -> bool:
        """回程第二段暫時送不出去 → 過幾秒再試一次；重試額度用完回 False。

        ★ 這一類是暫時性失敗（使用者要求：暫時性失敗自動重試）：
          InvWorker 幾秒內就會把搬家的表頭找回來、跳板重掛也只要幾秒。
        ⚠ 計時器帶著這一趟的號碼（_supply_gen），補給提早結束就自己作廢。
        """
        if self._recall_try >= RECALL_RETRIES:
            return False
        self._recall_try += 1
        gen = self._supply_gen
        QTimer.singleShot(
            int(RECALL_RETRY_SECS * 1000),
            lambda: gen == self._supply_gen and self._fire_recall())
        self.status.setText(f"{note}　→ {RECALL_RETRY_SECS:.0f} 秒後重試"
                            f"（第 {self._recall_try}/{RECALL_RETRIES} 次）")
        return True

    def _schedule_af_off(self) -> None:
        """**回程送出之後**隔幾秒把「自動攻擊」關回去（見 robot.AF_HOLD_SECS）。

        ★ 這樣使用者的「自動攻擊」幾乎全程都是關的，精靈不會跟我們搶怪，
          但補給那一趟照樣跑完。
        ⚠ 太早關會讓精靈只走到城裡就停住（0 秒實測失敗），所以這個秒數
          是實測調出來的，不要為了「快一點」去縮。

        ⚠⚠ 2026-08-09 改掉「排一次 QTimer 去關」的舊寫法。舊的長這樣：

            QTimer.singleShot(5 秒, lambda: mover 還在 and autofight_off(...))

          條件不成立就**整個跳過，而且沒有任何人會再關** —— 偏偏那一拍正是
          「回程道具剛用掉、人在換地圖／重連」，跳板最容易掛不上的時候。
          自動攻擊於是一路開著，精靈就自己挑怪打，牠**完全不看我們的
          「選中怪物」名單**（使用者回報「不知道為什麼會打我沒選的怪物」）。
          這裡現在只負責**解除看管期限**，真正去關的是 `_af_tick()` 的看門狗，
          關不掉就下一輪再關、不設上限（見 [[transient-failure-auto-retry]]）。
        """
        self._af_free = time.monotonic() + robot.AF_HOLD_SECS

    def _af_tick(self, dt: float) -> None:
        """看門狗：掛機期間官方精靈的「自動攻擊」必須是關的。

        ⚠⚠ **關掉是一個要一直維持的狀態，不是一個做過就算的動作。**
          它一開著，精靈就會自己挑怪打，而且不看「選中怪物」名單、也不看
          「只打王」—— 使用者看到的就是「跑去打我沒選的怪」。會自己變開的
          途徑至少三條：補給那趟是我們自己開的（`begin_supply`）、使用者在
          遊戲面板上勾了、換頻道／換地圖重連之後遊戲重新載設定。
          所以不能靠「該關的時候關一次」，要每隔幾秒收斂。
        ★ 補給那一趟刻意讓它開著（回城、修裝、買東西是精靈在跑），
          `self._af_free` 之前完全不管。
        ★ 讀是純記憶體（`robot.autofight_on`），關也是（不必跳板）——
          是關的就什麼都不做，平常成本趨近於零。
        ⚠ 讀不到回 None：**不猜、不動作**（樹還沒載好／位址失效）。
        """
        self._af_t += dt
        if self._af_t < AF_WATCH_GAP:
            return
        self._af_t = 0.0
        if time.monotonic() < self._af_free:
            return                          # 補給那一趟，刻意讓它開著
        if robot.autofight_on(self.sc) is not True:
            return                          # 關著／讀不到 → 什麼都不做
        if not robot.force_autofight_off(self.sc):
            self._dbg("精靈的「自動攻擊」是開的，這一拍關不掉 → 下一輪再關")
            return
        self._af_shut += 1
        self._dbg(f"關掉精靈的「自動攻擊」（累計第 {self._af_shut} 次）")
        if self._af_shut == 1:
            # 只講第一次：會反覆被打開的話狀態列每 3 秒閃一次沒有意義，
            # 次數留在診斷紀錄裡（AO_FARM_LOG=1）。
            self.status.setText("★ 精靈的「自動攻擊」是開的 → 已關掉"
                                "（開著牠會自己挑怪打，不看選中怪物）")

    def _jump_step(self, here: int | None) -> str:
        """趴趴GO回程的一步：時間到就送、送了沒到就再送。回傳要接在狀態列後
        面的短句（沒事就空字串）。由 `_supply_tick` 每 `SUPPLY_POLL` 秒叫一次。

        ⚠⚠⚠ **這支的存在理由**（2026-08-09 使用者回報「卡在傳送中…」）：
          `jumpmap.teleport()` 只保證「封包送出去了」，到不到得看伺服器。
          舊版是一次性計時器 —— 180 秒到了送一包，然後把「傳送中…」掛在畫面上
          就再也沒有人管。那一包只要沒生效（伺服器不收、正在載地圖、指令槽忙、
          跳板剛好不在），人就永遠留在城裡，畫面卻一直寫著「傳送中…」。
          現在改成**盯著地圖有沒有真的變**，沒變就再送 —— 不設重試上限
          （使用者要求），出口是補給總逾時那一聲停機＋通知。

        ⚠ 「到了沒」一律用 same_map 比對 —— 分流的場景編號不一樣（天使學園
          41/141/241），趴趴GO 的落點都在本流，拿編號硬比會把「已經站在同一
          張圖」看成「還沒回去」。
        """
        if self._jump_off or not self.sup_jump_cb.isChecked():
            return ""
        if self._supply_scene is None:
            self._jump_off = "不知道出發地圖"
            return ""
        if self._supply_t < JUMP_BACK_SECS:
            return ""                       # 還在倒數
        # ⚠ 讀不到現在在哪張圖就**不要送**：那多半正在載地圖（本來就該等），
        #   也可能人其實已經到了。CLAUDE.md 的鐵則：驗不過就不要動作。
        if here is None:
            return "　⚠ 讀不到目前地圖，趴趴GO先等一下"
        if scene.same_map(here, self._supply_scene):
            return ""                       # 已經在原圖了，上面就會判定補完
        if (self._jump_sent is not None
                and self._supply_t - self._jump_sent < JUMP_LAND_SECS):
            return "　✈ 趴趴GO已送出，等著陸…"
        pos = self._supply_pos or (None, None)
        e = jumpmap.nearest(self._supply_scene, pos[0], pos[1])
        if e is None:
            # 表裡真的沒這張圖的落點 → 再送幾次也變不出來。停掉趴趴GO、
            # 讓精靈自己走回來（SUPPLY_MAX_SECS 當兜底），並且**把原因寫在
            # 標籤上** —— 不准繼續顯示「傳送中…」騙人。
            self._jump_off = (scene.scene_name(self._supply_scene)
                              + "沒有傳送點")
            return f"　⚠ 趴趴GO 沒有回{scene.scene_name(self._supply_scene)}的傳送點"
        # ⚠ 跳板不在就**重裝**（_ensure_mover），不要空等它自己變回來 ——
        #   舊版只是每 20 秒看一次 `.active`，看三次沒好就整個放棄。
        if not self._ensure_mover():
            return "　⚠ 跳板裝不起來，趴趴GO下一輪再試"
        ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
        self._drop_cached_addrs()           # 傳送會換地圖，快取位址全作廢
        if not ok:
            # 暫時性失敗（連線是 0＝正在重連、指令槽忙…）→ 下一輪再送。
            # 不動 _jump_sent，所以是每 SUPPLY_POLL 秒重試，不用等著陸窗。
            return f"　⚠ 趴趴GO 送不出去（{msg}）→ 下一輪再試"
        self._jump_n += 1
        self._jump_sent = self._supply_t
        return (f"　✈ {msg}"
                + (f"（第 {self._jump_n} 次）" if self._jump_n > 1 else ""))

    # ------------------------------------------------------------------
    # -- 死亡回程（勾了「死亡自己回練功區」才會進）-------------------------
    def _start_death_return(self) -> None:
        """角色死了、而且勾了「死亡自己回練功區」→ 進死亡回程模式。

        流程很短：等 `DEATH_REVIVE_SECS` 秒（讓死亡視窗出來）→ 送
        「回標記點」封包（revive.to_mark，跟手點那顆按鈕完全一樣）→
        人活回**記錄點**那張地圖就接回自動戰鬥（沒有記錄點才用死掉那張）。
        精靈的「陣亡時自動復活」在開始掛機時已經被**關掉**
        （見 robot.apply_prefs）—— 它搶先把人復活回城反而壞事。
        """
        self._death = True
        self._death_t = 0.0
        self._death_poll = 0.0
        # 回哪張圖＝記錄點（巡邏點，見 _pick_home）；沒有記錄點才用死掉當下
        home = self._home
        self._death_scene = home[2] if home else self._scene
        # 挑落點用的座標：記錄點的座標；沒有才讀屍體位置（死了還讀得到）
        self._death_pos = (home[0], home[1]) if home else self.my_pos()
        self._death_try = DEATH_REVIVE_SECS
        self._death_sent = False
        self._death_jumped = None
        self._death_closed = False
        # 我們自己完全讓開（跟交棒補給時一樣）：不送技能鍵、不寫目標
        self._keys.set_on(False)
        self._atk.hold_off()
        self._cur = None
        # 極罕見：補給跑到一半死掉 → 補給那趟作廢，讓死亡回程接管。
        #（排著的趴趴GO/關自動攻擊計時器會因 gen 對不上自己作廢。）
        if self._supply:
            self._supply = False
            self._supply_gen += 1
        self.status.setText(f"☠ 角色死亡 → {DEATH_REVIVE_SECS:.0f} 秒後"
                            "送「回標記點」復活…")

    def _death_fail(self, why: str) -> None:
        """死亡回程走不下去 → 停機＋通知。

        ⚠ 「角色死亡」本身不通知（使用者要求），但**這裡照樣通知** ——
          這是「掛機停了、人還躺著」，跟裝備壞掉停機同一類：
          使用者掛網離開，要叫得動他。
        """
        self._death = False
        self._stop_with(f"☠ {why} → 已停止掛機")
        self.notify(f"{why}，掛機已停止。")

    def _death_tick(self, dt: float) -> bool:
        """死亡回程模式。進行中回 True（呼叫端整個 tick 都要讓開）。

        流程：死亡滿 `DEATH_REVIVE_SECS` 秒 → 送「回標記點」封包 → 復活
        → **人不在死掉那張圖就用趴趴GO傳回去** → 回到就接回自動戰鬥。
        ★ 還沒送就先活過來（使用者自己點了）也一樣接回，不再送封包。
        ★ 送了還一直沒活：每 `REVIVE_RETRY` 秒重送一次（暫時性失敗
          自動重試），`DEATH_WAIT_MAX` 兜底 —— 超過就停機＋通知。
        ★★ 復活後那段趴趴GO**不能省**（使用者 2026-08-07 回報）：標記點
          通常在城裡，不傳回去人就一直站在城中央，掛機等於停擺。
        """
        if not self._death:
            return False
        self._death_t += dt
        self._death_poll += dt
        if self._death_t >= DEATH_WAIT_MAX:
            self._death_fail(
                f"死亡回程超過 {DEATH_WAIT_MAX:.0f} 秒沒完成"
                "（「回標記點」沒生效？標記點沒設或在別張地圖？）")
            return True
        if self._death_poll < DEATH_POLL:
            return True
        self._death_poll = 0.0

        # 活過來了沒。stats 在復活時可能搬家，讀不到就當「還沒」，
        # 等掃描重新定位（超時有 DEATH_WAIT_MAX 兜底）。
        alive = False
        if self.stats:
            st = player.read(self.sc, self.stats)
            alive = st is not None and st.hp > 0
        now = scene.current(self.sc, allow_scan=False)
        here = now.id if now else None
        # ⚠ same_map：分流編號不一樣（41/141/241），趴趴GO落點在本流，
        #   拿編號硬比會判成「一直沒回去」→ 白等到 DEATH_WAIT_MAX 停機。
        back = (alive and here is not None and self._death_scene is not None
                and scene.same_map(here, self._death_scene))

        # ★ 活過來了 → 順手把死亡選擇視窗關掉。遊戲自己的確定鈕是
        #   **送包＋關窗**兩件事，我們只送了包，視窗就會一直留在畫面上
        #   （使用者 2026-08-07 回報）。走遊戲自己的 `CloseDeadWnd`。
        # ⚠ 一次死亡只叫一次、失敗也不管：關不掉只是視窗還在，人已經活了。
        # ⚠ **還死著時絕不關** —— 那是使用者自己要選的介面。
        if alive and not self._death_closed:
            self._death_closed = True
            if self._ensure_mover():
                revive.close_window(self._mover, self.sc)

        if back:
            self._death = False
            self._since_scan = SCAN_NOW          # 立刻重掃，馬上接回打怪
            self.status.setText("★ 已回到練功地圖 → 接回自動戰鬥"
                                if self._death_jumped is not None else
                                "★ 復活了 → 接回自動戰鬥")
            return True

        if alive:
            # 復活了、但人在標記點那張圖（通常是城裡）→ 趴趴GO傳回練功區
            # ⚠⚠ 跟補給那邊同一個坑（見 _jump_step）：**送出去 ≠ 到得了**。
            #   舊版送完就把 _death_jumped 舉起來乾等，那一包沒生效就一路等到
            #   DEATH_WAIT_MAX 停機。現在等 JUMP_LAND_SECS 沒到就再送一次。
            if self._death_jumped is not None:
                if self._death_t - self._death_jumped < JUMP_LAND_SECS:
                    return True                  # 送出去了，還在等著陸
                self._death_jumped = None        # 等太久 = 沒生效 → 再送一次
            if self._death_scene is None:
                self._death_fail("死亡時不知道人在哪張地圖，沒辦法傳回去")
                return True
            pos = self._death_pos or (None, None)
            e = jumpmap.nearest(self._death_scene, pos[0], pos[1])
            if e is None:
                self._death_fail(f"{scene.scene_name(self._death_scene)}"
                                 "不在趴趴GO清單裡，沒辦法傳回去")
                return True
            # ⚠ 跳板裝不起來／指令槽正忙都是**暫時性失敗** —— 下一拍再試就好，
            #   不要為了這個直接停機（DEATH_WAIT_MAX 還是會兜底＋通知）。
            if not self._ensure_mover():
                self.status.setText("★ 復活了 → 跳板裝不起來，再試著趴趴GO…")
                return True
            ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
            self._drop_cached_addrs()            # 傳送必換地圖，位址全作廢
            if not ok:
                self.status.setText(f"★ 復活了 → 趴趴GO送不出去（{msg}），"
                                    "下一拍再試…")
                return True
            self._death_jumped = self._death_t
            self.status.setText("★ 復活了 → 已用趴趴GO傳回練功區，等著陸…")
            return True

        if self._death_t < self._death_try:
            if not self._death_sent:
                left = max(self._death_try - self._death_t, 0.0)
                self.status.setText(f"☠ 角色死亡 → {left:.0f} 秒後"
                                    "送「回標記點」復活…")
            return True

        # 倒數到了、人還死著 → 點「回標記點」（跟手點死亡視窗那顆按鈕一樣）
        if not self._ensure_mover():
            self._death_fail("跳板裝不起來，送不了「回標記點」")
            return True
        if revive.to_mark(self._mover):
            self._death_sent = True
            self._death_try = self._death_t + REVIVE_RETRY
            self.status.setText("☠ 已送「回標記點」→ 等復活…")
        else:
            # 指令槽正忙 —— 下一拍再試，不算失敗
            self._death_try = self._death_t + DEATH_POLL
        return True

    # ------------------------------------------------------------------
    # -- 回程補給（判斷 → 交棒 → 回到原地圖再接回來）-----------------------
    def _check_dry(self) -> list | None:
        """算「哪幾組藥水見底（剩 ≤robot.POTION_LOW 顆）」，**不通知**。

        ⚠ 2026-08-19 使用者定：**買得到的藥水見底不通知**（會自動回城補給，
          「補給後又通知」是吵人）——藥水的通知只剩一種：補給店沒賣、
          要停機的那種，由 _dry_stop 自己發。

        回傳見底清單（兩組都算），讓同一拍的補給判斷直接拿去用，
        不必再走一次物品陣列；沒算成就回 None。
        """
        if not (self.inv and self._mover is not None and self._mover.active):
            return None
        try:
            return robot.potions_out(self._mover, self.sc, self.inv, self.pid)
        except Exception:                              # noqa: BLE001
            return None                                # 讀不到就當沒事，別擋掛機

    def _dry_stop(self, bad: list[str]) -> None:
        """藥水見底但**補給店沒賣那種藥水**（活動藥水這類非賣品）→
        通知＋天使之翼回城＋停止掛機（2026-08-19 使用者要求：
        買不到就不跑補給，直接收工）。

        ⚠ 翼回城是順手把人送回安全的城裡，送不出去也照樣停機 ——
          停機才是重點，訊息會標明人留在原地。
        """
        got = robot.has_recall_item(self.sc, self.inv) if self.inv else None
        wing = "背包找不到天使之翼，人留在原地"
        if got:
            if (self._mover is not None and self._mover.active
                    and recall.use_item(self._mover, got[0])):
                wing = "已用天使之翼回城"
            else:
                wing = "天使之翼送不出去，人留在原地"
        extra = "" if supply.SHOP_TABLE else "（補給店販售表載不到，全當買不到）"
        msg = (f"🧪 {'、'.join(bad)}快用完了，但補給店沒賣這種藥水{extra}"
               f" → {wing}，掛機已停止")
        self._stop_with(msg)
        self.notify(msg)

    def _start_supply(self, why: str) -> bool:
        """觸發回程補給：跑**我們自己**的 `supply.run_full_supply`（存倉庫→修裝→買水→
        趴趴GO 回原地）。接得起來（開了背景執行緒）才回 True。

        ★★★ 2026-08-14 改：**不再交給官方天使精靈**（官方補給有 BUG）。我們自己那套
          （app/game/supply.py）整趟自成一趟：記錄地圖座標→天使之翼回城→銀行存標記儲存的物品→
          維修商全修→補給商照清單買→趴趴GO 跳回原練功點。整趟是阻塞式、~數十秒到幾分鐘，
          放**背景執行緒**跑、`_supply_tick` 每拍輪詢完成，不凍畫面。
        ⚠ 天使精靈的自動戰鬥**不需要再打開**（使用者要求）——我們自己走位/銀行/買賣，精靈全程讓開。
        """
        if not self._ensure_mover():
            return False
        # ⚠ 上一趟的背景執行緒還活著就**絕不能**再開一條（逾時停機殺不掉它；
        #   疊第二條會兩邊同時搶走位、各燒各的翼）。它真的收工才准開新趟。
        t = self._supply_thread
        if t is not None and t.is_alive():
            self.status.setText(f"🔧 {why} → 上一趟補給的背景執行緒還沒收工，先等它")
            return False
        # 沒有回程道具（天使之翼）就別開始 —— run_full_supply 第一步就要用它回城。
        # ⚠ None ≠ ()（見 robot.has_recall_item）：None = 陣列剛搬家/被截斷，「讀不到」≠「沒有」
        #   —— 這一拍先不出發，觸發條件還在，下一輪再試（以前混在一起會誤停機）。
        have = robot.has_recall_item(self.sc, self.inv)
        if have is None:
            self.status.setText(f"🔧 {why} → 背包暫時讀不到，下一輪再試回程補給")
            return False
        if not have:
            item = itemname.label(recall.RECALL_ITEM)
            # ⚠⚠ **不准第一次就停機**（NO_RECALL_TRIES）：剛換地圖時容器還沒填完，
            #   觸發條件不會消失，下一輪自然再走到這裡。
            self._no_recall += 1
            if self._no_recall < NO_RECALL_TRIES:
                self.status.setText(
                    f"🔧 {why} → 找不到「{item}」，{GEAR_CHECK_GAP:.0f} 秒後"
                    f"再確認一次（第 {self._no_recall}/{NO_RECALL_TRIES} 次）")
                return False
            self._stop_with(f"🔧 {why}，但背包裡找不到「{item}」（回程道具）"
                            f"——連續確認 {NO_RECALL_TRIES} 次都沒有")
            self.notify(f"{why}，但背包裡找不到「{item}」（回程道具），掛機已停止。")
            return False
        self._no_recall = 0            # 找到了 → 之前那幾次是空窗，重新計數
        # 回程目標＝記錄點（巡邏點，見 _pick_home）；沒有記錄點才記出發當下
        home = self._home
        self._supply_scene = home[2] if home else self._scene
        self._supply_left = False
        self._supply_pos = (home[0], home[1]) if home else self.my_pos()
        self._supply_gen += 1
        # 我們自己要完全讓開：不送技能鍵、不寫目標；看門狗照常把精靈自動攻擊關著
        #   （run_full_supply 是我們在開車，精靈不該插手搶怪）。
        self._keys.set_on(False)
        self._atk.hold_off()
        self._cur = None
        self._supply = True
        self._supply_t = 0.0
        self._supply_poll = 0.0
        self._supply_result = None
        self._supply_progress = why
        # ★ 藥水買到負重 95%（2026-08-19 使用者要求）：出發前在主執行緒把
        #   精靈頁放的藥水種類抓下來帶給補給那趟（potion_slots 可能走 Lua，
        #   別讓背景執行緒去碰）。讀不到＝None → 那趟就不買藥水，安全退化。
        plan = robot.potion_buy_ids(self._mover, self.sc, self.pid)
        # ★ 背景執行緒跑整趟補給。say 回報進度存進 _supply_progress，_supply_tick 顯示＋等完成。
        mv, sc, gen = self._mover, self.sc, self._supply_gen

        def _worker():
            try:
                res = supply.run_full_supply(
                    mv, sc, say=lambda m: setattr(self, "_supply_progress", m),
                    back_to=home,      # 回程跳回記錄點（None＝出發當下，原行為）
                    potions=plan,      # 藥水買到負重 95%（生產分頁不帶＝不買）
                    ledger=self._record_purchase)   # 購買紀錄（純資料 append）
            except Exception as exc:                          # noqa: BLE001
                res = (False, f"補給出錯：{exc}")
            if gen == self._supply_gen:      # 這一趟還沒被作廢才收結果
                self._supply_result = res

        t = threading.Thread(target=_worker, daemon=True)
        self._supply_thread = t
        t.start()
        self.status.setText(f"🔧 {why} → 開始跑補給（存倉庫→修裝→買水→趴趴GO回來）…")
        return True

    def _end_supply(self, why: str, stop: bool = False) -> None:
        """補給收工，恢復打怪。stop=True 代表補給失敗，順便停掉掛機並通知。

        ⚠ 補給是我們自己的背景執行緒跑的（不碰精靈），這裡不必再 robot.end_supply。
          `_supply_gen += 1` 讓還在跑的執行緒（若逾時停機）之後回來的結果自己作廢。
        """
        self._supply = False
        self._supply_gen += 1          # 作廢還在跑的背景執行緒的結果
        self._supply_result = None
        # 看門狗放回來（掛機期間精靈自動攻擊一律關）。
        self._af_free = 0.0
        self._robot_ours = False
        # 補給跑完一定換過地圖，所有快取位址都要作廢
        self._drop_cached_addrs()
        if stop:
            self._stop_with(why)
            self.notify(why)
        else:
            self.status.setText(why)

    def _update_jump_countdown(self) -> None:
        """「回程補給」那列最右邊的趴趴GO倒數（使用者要求）。

        補給中就倒數到傳送；死亡回程時同一格顯示那邊的倒數。
        其餘時間留白。⚠ 只在字串真的變了才 setText —— 心跳是 10ms 一拍。

        ⚠⚠ **不准再出現一個永遠不會變的「傳送中…」**（使用者 2026-08-09 回報
          「卡在傳送中」）。倒數走完之後這裡一律顯示**現在到底卡在哪一步**：
          還沒送出、送出後等著陸幾秒、送了第幾次、或是根本走不通的原因。
        """
        txt = ""
        if self._supply:
            # 補給是背景執行緒跑我們自己的整趟（含趴趴GO回程）→ 這裡顯示它回報的進度。
            txt = f"補給：{self._supply_progress}" if self._supply_progress else "補給中…"
        elif self._train_supply:
            # 自動練技的補給趟（只跑補給商）也顯示在同一格。
            txt = (f"練技補給：{self._train_progress}"
                   if self._train_progress else "練技補給中…")
        elif self._death:
            left = max(0.0, DEATH_REVIVE_SECS - self._death_t)
            txt = (f"死亡回程 倒數 {_mmss(left)}" if left > 0
                   else "死亡回程 等復活…")
        if self.jump_lbl.text() != txt:
            self.jump_lbl.setText(txt)

    def _supply_tick(self, dt: float) -> bool:
        """補給進行中回 True（呼叫端要整個讓開）。

        ★ 補給整趟是背景執行緒在跑我們自己的 `run_full_supply`（存倉庫→修裝→買水→趴趴GO回來）。
          這裡只做兩件事：**顯示進度** 與 **等執行緒回結果**（回來了就 `_end_supply` 恢復打怪）。
        ⚠ 完成判斷不再看「回到原地圖」——run_full_supply 自己趴趴GO 跳回，跑完給結果就算完成。
        """
        if not self._supply:
            return False
        self._supply_t += dt
        # 背景執行緒跑完了 → 收工，恢復打怪
        if self._supply_result is not None:
            ok, msg = self._supply_result
            self._supply_result = None
            # ★★ 補給回來裝備**還是壞的**？連續兩趟就大聲停 —— 那不是暫時性
            #   失敗（該城沒維修商／修裝一直失敗），再重試只是每趟燒一張翼
            #   （跟 produce_tab 同一套煞車）。
            #   ⚠ worn_broken 的 None（讀不到）不算數：不加也不清（不確定就不動）。
            worn = bag.worn_broken(self.sc)
            if worn:
                self._broken_trips += 1
                if self._broken_trips >= 2:
                    self._end_supply(
                        f"⚠ 連續 {self._broken_trips} 趟補給回來裝備還是壞的"
                        f"（{msg}）—— 已停止掛機：請確認回程那座城有維修商，"
                        "或手動修一次再重開", stop=True)
                    return True
            elif worn is not None:
                self._broken_trips = 0
            # ★★ 補給回來藥水**還是見底**？連續兩趟就大聲停 —— 買水那步一直
            #   買不進來（金幣不夠／背包滿）不是暫時性失敗，重試只會每趟燒
            #   一張翼（跟上面壞裝煞車同一套）。讀不到＝不確定 → 不加也不清。
            dry_after = None
            if self.inv and self._mover is not None and self._mover.active:
                try:
                    dry_after = robot.potions_out(self._mover, self.sc,
                                                  self.inv, self.pid)
                except Exception:                          # noqa: BLE001
                    dry_after = None
            if dry_after:
                self._dry_trips += 1
                if self._dry_trips >= 2:
                    self._end_supply(
                        f"⚠ 連續 {self._dry_trips} 趟補給回來藥水還是見底"
                        f"（{msg}）—— 已停止掛機：金幣夠嗎？背包滿了嗎？",
                        stop=True)
                    return True
            elif dry_after is not None:
                self._dry_trips = 0
            self._end_supply(f"🔧 補給完成：{msg}　共花 {_mmss(self._supply_t)}")
            return True
        # 逾時兜底：run_full_supply 內部各段都有逾時，正常會自己回結果；
        #   這是「執行緒卡死/永不回」的最後保險 —— 大聲停機，別無聲卡住。
        if self._supply_t >= SUPPLY_MAX_SECS:
            self._end_supply(
                f"🔧 補給超過 {SUPPLY_MAX_SECS / 60:.0f} 分鐘還沒回來 → 已停止掛機"
                f"（最後進度：{self._supply_progress}）", stop=True)
            return True
        self.status.setText(
            f"🔧 回程補給中…{self._supply_progress}（{_mmss(self._supply_t)}）")
        return True

    # ------------------------------------------------------------------
    # -- 自動練技（跟掛機互斥；練技本體是官方精靈在跑）--------------------
    def _on_train_toggle(self, on: bool) -> None:
        """「自動練技」開關（2026-08-19 使用者要求）。

        開：開精靈主開關＋輔助頁「原地重複練習技能」，之後 _train_tick 顧著。
        關：把**我們開的**關回去（主開關＋練習技能）—— 這個勾選的語意就是
            「練技進行中」，關掉＝停止練技；跟 apply_prefs 那種「使用者自己
            的設定不碰」是兩回事。
        ⚠ 跟「開始掛機」互斥：兩邊都要指揮同一隻角色（精靈練技 vs 我們打
          怪、兩套補給），同時開只會互相踩。
        """
        if not on:
            if self._train_supply:
                self._train_supply = False
                self._train_gen += 1       # 作廢還在跑的補給執行緒結果
                self._train_result = None
            if self._halted:
                return                     # 遊戲已經不在了，別再寫記憶體
            try:
                if robot.is_run(self.sc):
                    robot.set_run(self._mover, self.sc, False)
                # 純記憶體關；記錄不存在＝本來就是關的。關不成也只是
                # 練習技能留著開 —— 主開關已關，精靈不會動作。
                if robot.exercise_on(self.sc):
                    robot.force_exercise(self.sc, False)
            except Exception:                          # noqa: BLE001
                pass                       # 分身剛被關掉：寫失敗無所謂
            self.status.setText("自動練技已停止（精靈主開關已關）")
            return

        if self.run_cb.isChecked():
            self.run_cb.setChecked(False)  # 互斥：練技接手，掛機停
        self._train_home = None
        self._train_dry_trips = 0
        self._train_no_wing = 0
        self._train_t = TRAIN_GAP          # 下一拍立刻推開關＋記原地
        notes: list[str] = []
        if self._ensure_mover():
            # 精靈自己的「回城補給」觸發全關掉 —— 補給那趟是我們在開車
            # （跟掛機開始時同一套，見 _on_toggle）。
            notes += robot.disable_return_supply(self._mover, self.sc)
            # 購買清單保證有天使之翼×50：每趟補給都燒一張翼回城，
            # 清單有它補給商那站才會補貨，不然練技幾趟後就回不了城。
            note = robot.ensure_buy_item(self._mover, self.sc,
                                         recall.RECALL_ITEM,
                                         robot.BUY_KEEP_WINGS)
            if note:
                notes.append(note)
            self._train_push()             # 立刻把主開關＋練習技能開起來
        self.status.setText(
            "🥋 自動練技中：精靈原地重複練習技能"
            f"（藥水剩 ≤{robot.POTION_LOW} 顆會自動回城買水）"
            + ("　" + "、".join(notes) if notes else ""))

    def _train_push(self) -> None:
        """看門狗：把「精靈主開關＋原地重複練習技能」往**開**推。

        ⚠⚠ 開著是一個要一直維持的狀態，不是做過就算的動作：遊戲重開會自己
          把練習技能關掉（使用者實測）、斷網重登／被踢也一樣 —— 所以每
          TRAIN_GAP 收斂一次，推不成下一輪再推、不設上限
          （[[transient-failure-auto-retry]]）。讀寫都純記憶體，
          是開的就什麼都不做，平常成本趨近於零。
        """
        if not robot.is_run(self.sc):
            ok, why = robot.set_run(self._mover, self.sc, True)
            if not ok:
                # 還沒進遊戲／精靈子系統沒好：正常路過，別吵（_dbg 有記）
                self._dbg(f"練技：主開關開不起來（{why}）→ 下一輪再開")
        cur = robot.exercise_on(self.sc)
        if cur is None:
            return          # 樹讀不到（登入中／改版）→ 不猜、不動作（同 _af_tick）
        if cur is True:
            return
        if robot.force_exercise(self.sc, True):
            self._dbg("練技：開回「原地重複練習技能」")
            return
        # 寫不動＝記錄還沒建（角色從沒勾過那個框）→ 只能退 Lua 讓遊戲自己建。
        # ⚠ Lua 是全專案風險最高的動作，節流 TRAIN_LUA_GAP 才試一次。
        now = time.monotonic()
        if (now - self._train_lua_t >= TRAIN_LUA_GAP
                and self._mover is not None and self._mover.active):
            self._train_lua_t = now
            robot.set_bool(self._mover, self.sc,
                           robot.AS_EXERCISE_SKILL, True)

    def _train_stop(self, msg: str) -> None:
        """大聲停用自動練技：放掉勾勾（收尾在 _on_train_toggle）＋通知。"""
        self.train_cb.setChecked(False)
        self.status.setText(msg)
        self.notify(msg)

    def _train_start_supply(self, why: str) -> None:
        """練技的補給趟：關精靈主開關 → 背景執行緒跑**只有補給商那站**的
        run_full_supply（potion_only，不存倉不修裝）→ 回標記地圖。

        跟 _start_supply 平行的一套，差在：不動 KeyWorker/TargetWorker
        （練技時它們本來就沒在跑）、回程點固定是 _train_home（練技的原地，
        不是巡邏點 —— 使用者指定這功能不走巡邏點）。
        """
        # ⚠ 背景執行緒的把手跟掛機補給共用（同一隻角色只准有一趟在路上）。
        t = self._supply_thread
        if t is not None and t.is_alive():
            self.status.setText(f"🥋 {why} → 上一趟補給的背景執行緒還沒收工，先等它")
            return
        if self._train_home is None:
            # 還沒記到練技位置就不出發 —— 這時讓 run_full_supply 記「出發
            # 當下」等於把回程點交給運氣（見 _train_home 的說明）。
            self.status.setText(f"🥋 {why} → 還沒記到練技位置（座標讀不到），下一輪再出發")
            return
        have = robot.has_recall_item(self.sc, self.inv)
        if have is None:
            self.status.setText(f"🥋 {why} → 背包暫時讀不到，下一輪再試")
            return
        if not have:
            item = itemname.label(recall.RECALL_ITEM)
            self._train_no_wing += 1
            if self._train_no_wing < NO_RECALL_TRIES:
                self.status.setText(
                    f"🥋 {why} → 找不到「{item}」，{TRAIN_GAP:.0f} 秒後再確認"
                    f"（第 {self._train_no_wing}/{NO_RECALL_TRIES} 次）")
                return
            self._train_stop(f"🥋 {why}，但背包裡找不到「{item}」（回程道具）"
                             "→ 自動練技已停止")
            return
        self._train_no_wing = 0
        # 藥水種類在主執行緒抓（potion_slots 可能走 Lua，背景執行緒不准碰）。
        plan = robot.potion_buy_ids(self._mover, self.sc, self.pid)
        # ★ 出發前關精靈主開關（使用者指定）：路上精靈不能再原地施法。
        if robot.is_run(self.sc):
            robot.set_run(self._mover, self.sc, False)
        self._train_supply = True
        self._train_supply_t = 0.0
        self._train_result = None
        self._train_progress = why
        self._train_gen += 1
        mv, sc, gen = self._mover, self.sc, self._train_gen
        home = self._train_home

        def _worker():
            try:
                res = supply.run_full_supply(
                    mv, sc,
                    say=lambda m: setattr(self, "_train_progress", m),
                    back_to=home,          # 回練技的原地圖（不是巡邏點）
                    potions=plan,          # 藥水買到負重 95%
                    potion_only=True,      # 只跑補給商：不存倉、不修裝
                    ledger=self._record_purchase)   # 購買紀錄（純資料 append）
            except Exception as exc:                      # noqa: BLE001
                res = (False, f"補給出錯：{exc}")
            if gen == self._train_gen:     # 這一趟還沒被作廢才收結果
                self._train_result = res

        t = threading.Thread(target=_worker, daemon=True)
        self._supply_thread = t
        t.start()
        self.status.setText(f"🥋 {why} → 關精靈主開關，回城找補給商買藥水"
                            "（不存倉、不修裝）…")

    def _train_tick(self, dt: float) -> None:
        """自動練技的心跳（只在**沒掛機**時被 tick 呼叫；兩者互斥）。

        練技本體是官方精靈在跑（原地重複練習技能），這裡只做三件事：
          ① 看門狗把「主開關＋練習技能」往開推（_train_push）
          ② 藥水見底（兩組加總各 ≤POTION_LOW）→ 練技補給趟（_train_start_supply）
          ③ 見底的那組放的是**店裡沒賣**的藥水 → 通知＋自動關閉（使用者指定）
        """
        if not self.train_cb.isChecked():
            return
        # ── 補給趟進行中：只等結果／逾時。開關看門狗暫停 ——
        #    主開關是我們**刻意**關的，這時推回去等於邊跑補給邊施法。
        if self._train_supply:
            self._train_supply_t += dt
            if self._train_result is not None:
                ok, msg = self._train_result
                self._train_result = None
                self._train_supply = False
                # ★ 回來還是見底？連續兩趟就大聲停 —— 一直買不進來
                #   （金幣不夠／背包滿）不是暫時性失敗，重試只會每趟燒一張翼
                #   （跟掛機補給的 _dry_trips 煞車同一套）。
                # ⚠ 要在 _drop_cached_addrs **之前**算：它會把 self.inv 清成
                #   None，之後 _check_dry 只會回「讀不到」，煞車永遠數不到
                #   （potions_out 自帶表頭驗證，拿舊表頭算是安全的 ——
                #   驗不過就回不觸發，跟掛機補給那邊同一個順序）。
                dry_after = self._check_dry()
                self._drop_cached_addrs()  # 補給跑完換過地圖，快取位址作廢
                self._train_push()         # 回來了 → 主開關＋練習技能開回去
                if dry_after:
                    self._train_dry_trips += 1
                    if self._train_dry_trips >= 2:
                        self._train_stop(
                            f"⚠ 連續 {self._train_dry_trips} 趟補給回來藥水"
                            f"還是見底（{msg}）→ 自動練技已停止："
                            "金幣夠嗎？背包滿了嗎？")
                        return
                elif dry_after is not None:
                    self._train_dry_trips = 0
                self.status.setText(
                    f"🥋 練技補給完成：{msg}　共花 {_mmss(self._train_supply_t)}"
                    "（主開關已開回，練技繼續）")
                return
            if self._train_supply_t >= SUPPLY_MAX_SECS:
                # run_full_supply 各段都有逾時，這是執行緒卡死的最後保險。
                self._train_stop(
                    f"⚠ 練技補給超過 {SUPPLY_MAX_SECS / 60:.0f} 分鐘還沒回來"
                    f"（最後進度：{self._train_progress}）→ 自動練技已停止")
            return

        self._train_t += dt
        if self._train_t < TRAIN_GAP:
            return
        self._train_t = 0.0
        if not self._ensure_mover():
            return                          # 跳板還沒好：下一輪再來（會自動重試）
        # 記「練技的原地」：第一次讀到位置才記（補給回程要跳回這張圖）。
        if self._train_home is None:
            here = self.cur_scene()
            pos = self.my_pos()
            if here is not None and pos is not None:
                self._train_home = (pos[0], pos[1], here)
        self._train_push()
        # 藥水見底？（HP/MP 兩組都看，判定跟掛機同一套 —— _check_dry）
        dry = self._check_dry()
        if not dry:
            return
        # 見底的那組放的是店裡沒賣的藥水（活動藥水這類）→ 買不到，
        # 跑補給也是白燒一張翼：通知＋自動關閉（使用者指定）。
        plan = robot.potion_buy_ids(self._mover, self.sc, self.pid)
        bad = [d for w, d in dry
               if plan is not None and plan.get(w)
               and not any(supply.shop_sells(t) for t in plan.get(w, ()))]
        if bad:
            extra = "" if supply.SHOP_TABLE else "（補給店販售表載不到，全當買不到）"
            self._train_stop(f"🥋 {'、'.join(bad)}快用完了，"
                             f"但補給店沒賣這種藥水{extra} → 自動練技已關閉")
            return
        self._train_start_supply(
            "、".join(d for _, d in dry) + f"剩 ≤{robot.POTION_LOW} 顆")

    # ------------------------------------------------------------------
    # -- 購買紀錄（回程補給跟商人買了什麼；2026-08-20 使用者要求）--------
    def _record_purchase(self, merchant: str, tid: int, qty: int) -> None:
        """補給那趟買到一筆 → 記帳。**背景執行緒呼叫**：只碰純資料，不碰 Qt。

        數量是 run_buy／run_potion_fill 的背包對帳差額（真的進來幾個）。
        花費＝補給店販售表的單價 × 數量 —— 表是 build_supply_shop.py 從遊戲
        資源包抽的（有 tablestamp 過期偵測）；表裡沒有這一項就記 None，
        表單顯示「—」並排除在總額外（不猜價錢）。
        """
        price = supply.SHOP_TABLE.get(int(tid), (0, 0))[1]
        cost = int(price) * int(qty) if price else None
        self._purchases.append(
            (time.time(), merchant, int(tid), int(qty), cost))
        if len(self._purchases) > PURCHASE_CAP:
            del self._purchases[:-PURCHASE_CAP]

    def _purchases_dialog(self) -> QDialog:
        """把購買紀錄畫成一張表（新的在上面），總額掛在表的上方。

        跟 exec 拆開是為了離線測試能只建表不進事件迴圈（train_check.py）。
        ⚠ 一次性快照：開的當下有什麼畫什麼，要看新的關掉重開（不做即時
          刷新 —— 高頻改表是 qt-ui-pitfalls 的坑，這裡不需要）。
        """
        rows = list(self._purchases)[::-1]        # 新的在上面
        total = sum(c for *_x, c in rows if c is not None)
        unknown = sum(1 for *_x, c in rows if c is None)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"購買紀錄 — {self.char_name or self.account or self.pid}")
        v = QVBoxLayout(dlg)
        if rows:
            head = f"總花費 {total:,} 金幣（共 {len(rows)} 筆）"
            if unknown:
                head += (f"，另有 {unknown} 筆單價不明未計入"
                         "（不在補給店販售表 —— 表過期？）")
        else:
            head = "還沒有購買紀錄 —— 回程補給跟商人買到東西時會記在這裡。"
        lab = QLabel(head)
        lab.setStyleSheet("font-weight: bold;")
        v.addWidget(lab)

        tbl = QTableWidget(len(rows), 5)
        tbl.setHorizontalHeaderLabels(["時間", "商人", "物品", "數量", "花費(金幣)"])
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        for i, (ts, merchant, tid, qty, cost) in enumerate(rows):
            cells = (time.strftime("%m/%d %H:%M:%S", time.localtime(ts)),
                     merchant, itemname.label(tid), str(qty),
                     f"{cost:,}" if cost is not None else "—")
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if col >= 3:                      # 數量／花費靠右對齊
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                tbl.setItem(i, col, it)
        # 欄寬手動給、最後一欄補滿 —— 不用 ResizeToContents（qt-ui-pitfalls）。
        for col, w in enumerate((115, 130, 170, 55)):
            tbl.setColumnWidth(col, w)
        tbl.horizontalHeader().setStretchLastSection(True)
        v.addWidget(tbl, 1)
        dlg.resize(640, 420)
        # 給離線測試摸得到（不重跑排版邏輯就能驗內容）
        dlg._head, dlg._tbl = lab, tbl
        return dlg

    def _show_purchases(self) -> None:
        self._purchases_dialog().exec()

    # ------------------------------------------------------------------
    # -- 自動換球（2026-08-21 使用者要求）--------------------------------
    #
    # 規則（使用者定，2026-08-21 兩次澄清）：
    #   ★★ **飾品欄上「裝著的球」全部都滿了才動作，而且一次全換掉。**
    #      只有一顆滿 → **什麼都不做、也不通知**（使用者原話：「只有一顆滿
    #      不出發任何流程」）。他的玩法是兩顆一起換，換一顆等於浪費一次。
    #      飾品欄**根本沒裝球** → 一樣什麼都不做（使用者原話：「原本就沒
    #      用球那就不用管了，只有用球並滿了我們才會觸發流程」）。
    #   · 換上去的必須是**同族**（技能／角色／寵物）而且**沒滿**的球；
    #     兩格一起配對，同一顆備球不會被兩格重複認領。
    #   · 備球不夠 → **去商城買到夠**（買完自動從商城倉庫領進背包），
    #     然後下一輪自然就換上了。
    #   · 商城沒賣／買不到（點數不足…）→ 通知**一次**，掛機照常繼續。
    #   ⚠⚠ 買東西花的是真的點數，所以「這一次都滿了」最多只買一輪：
    #      決定要買的當下就把門閂閂上，等球真的被換下去（不再是滿的）
    #      才重新武裝。這樣一個「滿了」事件最多花一次點數。
    def _on_ball_toggle(self, on: bool) -> None:
        """「自動換球」開關。純粹是換一組狀態，不動掛機。"""
        self._save_settings()
        self._ball_t = BALL_GAP            # 勾起來就馬上看一次，不等一輪
        self._ball_said = self._ball_told = False
        self._ball_off = ""
        if on:
            self.status.setText("自動換球已開啟：兩顆經驗球都滿了會一起換")

    def _ball_done(self, ok: bool, msg: str) -> None:
        """換球／補貨背景執行緒的收尾（在 UI 執行緒上跑）。"""
        self._ball_busy = False
        self.status.setText(("經驗球：" if ok else "⚠ 自動換球：") + msg)
        if ok:
            self.notify(msg)
            return
        # ⚠ 定位失敗＝改版把函式搬走了，重試沒有意義 → 大聲停用。
        if "定位失敗" in msg:
            self._ball_off = msg
            self.notify(f"自動換球已停用：{msg}")
        elif not self._ball_told:
            # ★ 同一個「都滿了」事件的失敗只講一次（換球那種暫時性失敗下一輪
            #   還是會再試，只是不再吵人）。球被換下去之後門閂自動重新武裝。
            self._ball_told = True
            self.notify(msg)

    def _test_ball_swap(self) -> None:
        """臨時測試鈕：**假裝球已經滿了**，把真正的流程原封不動跑一次。

        使用者 2026-08-21 要求：不要再做「左右對調」那種代用測試，直接
        「背包有沒滿的就換上去、沒有就去買」—— 也就是跟 `_ball_tick` 觸發時
        完全同一條路（`_ball_run`），只差**跳過「全部都滿了」那道閘門**。

        ⚠ 這顆是驗證用的（memory 的 test-via-button）。三種封包都是
          2026-08-21 離線反組譯挖出來的：
          ✅ 換球 0x12、領取 0x2F/0x16 已實機驗證（嵐狐）。
          ⚠ 買 0x12B 只有花點數才驗得到 → 要買之前**一定另外問一次**，
            問句寫明買什麼、幾點，而且預設鈕是「否」。
          三包都驗過之後這顆鈕就可以拆了。
        """
        sc = self.sc
        if sc is None or not sc.attached:
            QMessageBox.warning(self, "測試換球", "還沒接上這台遊戲。")
            return
        if self._ball_busy:
            QMessageBox.information(self, "測試換球", "上一次還在跑，等它跑完。")
            return
        got = balls.worn(sc)
        pool = balls.spares(sc)
        if got is None:
            QMessageBox.warning(self, "測試換球",
                                "飾品欄讀不到（還沒進場？）—— 沒有動作。")
            return
        cur = got[0]
        lines = ["【現況】"]
        for b in cur:
            lines.append(f"　{inventory.slot_side(b.slot)}飾品（第 {b.slot} 格）"
                         f"　{b.name}　{b.value:,}/{b.cap:,}"
                         + ("　★滿了" if b.full else ""))
        if not cur:
            lines.append("　飾品欄兩格都沒有經驗球")
        lines.append("　背包備球：" + (
            "讀不到" if pool is None else
            "、".join(f"{b.name} {b.value:,}/{b.cap:,}（第 {b.slot} 格）"
                      for b in pool) or "一顆都沒有"))
        if not cur:
            lines.append("\n飾品欄沒有球 —— 真流程也不會動作（使用者定的規則）。")
            QMessageBox.information(self, "測試換球", "\n".join(lines))
            return
        if pool is None:
            lines.append("\n背包讀不到 —— 真流程這時候不動作。")
            QMessageBox.information(self, "測試換球", "\n".join(lines))
            return

        # 先算一次配對，讓使用者知道待會會做什麼；要花點數就問清楚再送。
        pairs, missing = balls.pick_spares(pool, cur)
        plan = [f"　換上背包第 {sp.slot} 格的「{sp.name}」"
                f"→ {inventory.slot_side(old.slot)}飾品" for old, sp in pairs]
        cost = 0
        for b in missing:
            g = mall.cheapest(sc, b.type_id)
            if g is None:
                plan.append(f"　⛔ 商城查不到「{b.name}」，這一格補不到")
                continue
            cost += g.price
            plan.append(f"　去商城買 {g.name}×{g.count}"
                        f"（編號 {g.mall_id}，{g.price} 點）→ 領進背包 → 換上"
                        f"{inventory.slot_side(b.slot)}飾品")
        lines.append("\n【假裝兩顆都滿了，接下來會做】")
        lines += plan or ["　（沒有可做的事）"]

        ask_text = "\n".join(lines) + "\n\n要現在跑一次嗎？"
        if cost:
            ask_text += f"\n⚠ 其中會真的花掉 {cost} 點商城點數。"
        if missing:
            # 官方限制兩次商城動作要隔 5 秒以上（mall.ACTION_GAP），所以
            # 補球一定會慢 —— 先講清楚，不然又會被當成當掉。
            secs = int(len(missing) * 2 * mall.ACTION_GAP)
            ask_text += (f"\n（官方限制商城動作要間隔 {int(mall.ACTION_GAP)} 秒，"
                         f"補球至少要跑 {secs} 秒，進度看狀態列）")
        if QMessageBox.question(
                self, "測試換球", ask_text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        if not self._ensure_mover():
            QMessageBox.warning(self, "測試換球", "跳板沒接上，無法送封包。")
            return

        # ⚠ 整條流程會等伺服器（買／領各最多 8 秒、每次換球最多 3 秒），
        #   放 UI 執行緒上會把畫面凍住 —— 跟真流程一樣丟背景執行緒。
        self._ball_busy = True
        self.ball_test_btn.setEnabled(False)
        self.status.setText("測試換球：跑真流程中…")

        def _worker() -> None:
            try:
                ok, msg = self._ball_run(sc, cur, pool)
            except Exception as exc:                 # noqa: BLE001
                ok, msg = False, f"{exc!r}"
            QTimer.singleShot(0, lambda: self._test_ball_done(lines, ok, msg))

        threading.Thread(target=_worker, daemon=True,
                         name=f"balltest-{self.pid}").start()

    def _test_ball_done(self, lines: list, ok: bool, msg: str) -> None:
        """測試鈕的收尾（UI 執行緒）：把結果與換完之後的狀態攤開來。"""
        self._ball_busy = False
        self.ball_test_btn.setEnabled(True)
        out = list(lines)
        out.append(f"\n【結果】{'✔ ' if ok else '⛔ '}{msg}")
        after = balls.worn(self.sc) if self.sc is not None else None
        if after is not None:
            out.append("\n【跑完之後】")
            for b in after[0]:
                out.append(f"　{inventory.slot_side(b.slot)}飾品"
                           f"（第 {b.slot} 格）　{b.name}"
                           f"　{b.value:,}/{b.cap:,}")
            spare = balls.spares(self.sc)
            out.append("　背包備球：" + (
                "讀不到" if spare is None else
                "、".join(f"{b.name} {b.value:,}/{b.cap:,}" for b in spare)
                or "一顆都沒有"))
        QMessageBox.information(self, "測試換球", "\n".join(out))
        self.status.setText(f"測試換球：{'成功' if ok else '失敗'}　{msg}")

    def _ball_say(self, text: str) -> None:
        """把背景流程的進度丟回狀態列。**背景執行緒呼叫**，所以要繞回 UI 執行緒。

        ⚠ 沒有這個的話，整條流程（官方兩次商城動作要隔 5 秒以上，見
          `mall.ACTION_GAP`）跑起來會靜默好幾十秒，看起來就像當掉
          —— 使用者 2026-08-21 回報的「卡住了」有一半是這個。
        """
        QTimer.singleShot(0, lambda: self.status.setText(f"經驗球：{text}"))

    def _ball_restock(self, sc, need: list) -> tuple[bool, str]:
        """去商城把缺的備球補齊（買 → 從商城倉庫領進背包）。

        ⚠⚠ **會花真的點數**，所以每一步都驗結果：買完要看商城倉庫真的多一筆、
          領完要看那一筆真的離開商城倉庫。任何一步失敗就整個停手回報。
        ⚠ 每一包送出前 `mall` 那邊都會等滿官方的 5 秒節流（`mall.ACTION_GAP`），
          所以補兩顆球大概要跑 30 秒 —— 進度會即時寫在狀態列。
        """
        spent = 0
        bought = 0
        for cur in need:
            g = mall.cheapest(sc, cur.type_id)
            if g is None:
                return False, f"商城查不到「{cur.name}」，補不到備球"
            ok, msg = mall.buy(self._mover, sc, g, say=self._ball_say)
            if not ok:
                return False, f"商城購買「{g.name}」失敗：{msg}"
            spent += g.price
            bought += g.count
            # ⚠ 刻意**不**寫進「購買紀錄」那張表：那張表的花費欄是**金幣**
            #   （單價來自補給店販售表），把點數混進去總額就是錯的。
            #   商城花了幾點改成每次都在通知與狀態列明講。
            # 買到的東西在商城倉庫，要領進背包才換得上
            st = mall.storage(sc)
            if st is None:
                return False, "商城倉庫讀不到，領不出來（東西已經買了）"
            mine = [r for r in st if r[1] == g.type_id]
            if not mine:
                return False, f"商城倉庫裡找不到剛買的「{g.name}」"
            ok, msg = mall.take(self._mover, sc, mine[0][0], g.type_id,
                                say=self._ball_say)
            if not ok:
                return False, f"從商城倉庫領「{g.name}」失敗：{msg}"
        return True, f"已從商城補 {bought} 顆備球（花費 {spent} 點）"

    def _ball_run(self, sc, cur: list, pool) -> tuple[bool, str]:
        """整條換球流程（會 block，一定要在背景執行緒上跑）。"""
        if pool is None:
            return False, "背包讀不到，這輪不動作"
        pairs, missing = balls.pick_spares(pool, cur)
        # ★ 花掉的點數一定要跟著結果講出來（通知＋狀態列都吃這句）——
        #   自動花錢的功能不准安靜地花。
        spent_note = ""
        if missing:
            ok, msg = self._ball_restock(sc, missing)
            if not ok:
                return False, (f"經驗球都滿了，但{msg} —— "
                               f"還缺 {len(missing)} 顆備球，請手動處理。")
            spent_note = msg + "；"
            pool = balls.spares(sc)
            if pool is None:
                return False, spent_note + "補貨完背包讀不到，下一輪再換"
            pairs, missing = balls.pick_spares(pool, cur)
            if missing:
                return False, (spent_note
                               + f"補貨完備球還是不夠（差 {len(missing)} 顆）")
        names = []
        for old, new in pairs:
            self._ball_say(f"換上「{new.name}」→ "
                           f"{inventory.slot_side(old.slot)}飾品")
            ok, msg = balls.swap(self._mover, sc, new.slot, old.slot,
                                 say=self._ball_say)
            if not ok:
                return False, (spent_note
                               + f"{inventory.slot_side(old.slot)}飾品換球失敗："
                               + msg)
            names.append(new.name)
        return True, (spent_note + "經驗球都滿了 → 已換上「"
                      + "」、「".join(names) + f"」共 {len(names)} 顆。")

    def _ball_tick(self, dt: float) -> None:
        """自動換球的心跳（BALL_GAP 一拍）。

        ⚠⚠ 「讀不到就不下結論」的關卡（[[bag-false-empty-guards]]）：
          ① 飾品欄整段沒讀完 → 這輪跳過（不是「沒裝球」）
          ② 球的上限讀不到（`Ball.known` False）→ 不判斷滿沒滿
          ③ 背包沒讀完 → 不准說「沒有備球」（那句話會發通知、還會去買東西）
        """
        if not self.ball_cb.isChecked() or self._ball_off or self._ball_busy:
            return
        self._ball_t += dt
        if self._ball_t < BALL_GAP:
            return
        self._ball_t = 0.0
        sc = self.sc
        if sc is None or not sc.attached:
            return

        got = balls.worn(sc)
        if got is None:
            return                                   # ① 讀不到 → 不下結論
        cur = got[0]
        # ★★ 使用者定：**全部都滿了才動**。只有一顆滿＝什麼都不做也不通知。
        if not cur or not all(b.full for b in cur):
            # 情況恢復（換上新球了）→ 兩個門閂都重新武裝
            self._ball_said = self._ball_told = False
            return
        pool = balls.spares(sc)
        if pool is None:
            return                                   # ③ 讀不到 → 不下結論
        # 備球不夠就要去商城買 —— 決定的當下先閂上，一個「滿了」事件只買一輪。
        _, missing = balls.pick_spares(pool, cur)
        if missing and self._ball_said:
            return
        if missing:
            self._ball_said = True
        if not self._ensure_mover():
            return

        self._ball_busy = True

        def _worker() -> None:
            try:
                ok, msg = self._ball_run(sc, cur, pool)
            except Exception as exc:                 # noqa: BLE001
                ok, msg = False, f"{exc!r}"
            # ⚠ 回 UI 執行緒才碰 Qt（背景執行緒直接改元件會炸）。
            QTimer.singleShot(0, lambda: self._ball_done(ok, msg))

        threading.Thread(target=_worker, daemon=True,
                         name=f"ballswap-{self.pid}").start()

    # ------------------------------------------------------------------
    # -- 血/魔不足時坐下回復 ---------------------------------------------
    def _foe_evidence(self, m, st: str, p, me) -> str:
        """這隻怪「正在打我」的證據有多硬：`"hard"` / `"guess"` / `""`。

        ★ **只有這裡定義「誰在打我」**，別的地方一律問這支（或 _fighting_me）。

        `"hard"` 交戰槽（三個都看，entity.attacking）裡有我的角色物件。
           ⚠ 唯讀跟拍實錘：怪出手的當下三個槽**全是空的**（17/17），
             指標反而殘留在發呆中／屍體上 —— 所以它會**漏**，
             但它出現時是真的（不會指到一隻跟我無關的怪身上）。
        `"guess"` 動畫是攻擊中（Att/Att2/Cast）而且離我 FOE_NEAR 格內。
           出手那幾拍一定抓得到，但**不知道牠在打誰** —— 別人在我旁邊
           拉怪打也會中。
        呼叫端自己先排掉屍體（st == "Dead"）。

        ⚠ 還有第三個更寬的訊號（「我在掉血且牠在附近」）**故意不放進來**：
          那個連「牠有沒有出手」都不看，只有 _pick_next 的收尾反擊在用，
          而且只准用在本來就要打的目標上。
        ⚠ 2026-08-08 起「猜的」不再等同「硬的」：反擊名單外的怪（尤其是王）
          一律要 `"hard"`，見 _pick_next。
        """
        if self.player and entity.attacking(self.sc, m, self.player):
            return "hard"
        d = (math.hypot(p[0] - me[0], p[1] - me[1])
             if (p is not None and me is not None) else float("inf"))
        return "guess" if st in entity.ATT_STATES and d <= FOE_NEAR else ""

    def _fighting_me(self, m, st: str, p, me) -> bool:
        """這隻怪是不是「正在打我」（證據硬不硬不管）。見 _foe_evidence。

        ⚠ 要據此做危險的事（跑去打一隻沒選的怪）請改看 _foe_evidence 的等級。
        ⚠⚠ **這支跟 `_foes()` 已經不是同一個標準了**（2026-08-09）：
          `_foes()` 只認 `"hard"`（見那裡的正回饋循環說明），這支照舊連
          `"guess"` 都收。唯一的呼叫端是「冷卻中的怪要不要解禁」——
          解禁只是**允許**它重新被挑，不是強制去打，猜錯的代價很小。
        """
        return bool(self._foe_evidence(m, st, p, me))

    def _under_attack(self) -> bool:
        """最近 UNDER_ATTACK_SECS 內自己的 HP 掉過 → 一定有怪在打我。

        ★ 這是**不靠任何交戰欄位**的硬保險（欄位會失靈，見 _fighting_me）。
        HP 只會被怪打掉 —— 喝水、坐下、被動回復都只會往上。
        """
        return time.monotonic() - self._hp_drop_t <= UNDER_ATTACK_SECS

    # ⛔⛔ 這裡以前有一整組「坐下休息」的方法（2026-08-09 依使用者要求移除）：
    #   _foes／_sit_down／_stand_up／_rest_wanted／_rest_full／_end_rest／_rest_tick。
    #   理由與替代路徑見建構式裡那段說明。
    # ⚠ `_foe_evidence()` / `_fighting_me()` / `_under_attack()` **沒有刪** ——
    #   「冷卻中的怪正在打我就解禁」還在用它們（見 _pick_next）。

    def _rot_say(self, text: str) -> None:
        """更新巡迴狀態文字。**內容沒變就不要動它** ——
        心跳是 10ms 一拍，每拍都 setText 等於每秒重畫 100 次，白花 CPU 也會閃。
        """
        if text != self._rot_last:
            self._rot_last = text
            self.rot_lbl.setText(text)

    def _tick_rotation(self, dt: float) -> bool:
        """巡迴換頻道的節奏。回傳 True = 這一拍不要打怪（正在換／剛換完）。

        ★ 只有「切換的那幾秒」會暫停，**停留期間照常掛機** ——
          不然巡迴就只是在浪費時間。
        ⚠ 補給那一趟完全不換頻：換頻會斷線重連、把人丟到別的分流，
          正在跑回程的精靈跟排著的趴趴GO都會被打斷（回不去的成因之一）。
          這裡不累積時間，補給結束後接著算就好。
        """
        if self._supply:
            return False
        if not self.rot_cb.isChecked():
            if self._rot_seq or self._rot_settle:
                self._rot_seq, self._rot_settle = [], 0.0
                self._rot_say("")
            return False

        # 剛換完 → 等重連與重新定位，這段不打怪
        if self._rot_settle > 0:
            self._rot_settle -= dt
            here = channel.current(self.hwnd)
            self._rot_say(
                f"　換到 {here or '?'} 頻，穩定中…{_mmss(self._rot_settle)}")
            return True

        if self._rot_seq:
            self._rot_wait -= dt
            if self._rot_wait > 0:
                left = len(self._rot_seq)
                self._rot_say(
                    f"　巡迴中：{channel.current(self.hwnd) or '?'} 頻"
                    f"　還有 {_mmss(self._rot_wait)} 換下一個（剩 {left} 站）")
                return False              # ← 停留期間照常打怪
            nxt = self._rot_seq.pop(0)
            if not self._switch_channel(nxt):
                self._rot_seq.insert(0, nxt)   # 排不進指令槽，下一拍再試
                self._rot_wait = 1.0
                self._rot_say("　換頻道失敗，重試中…")
                return True
            self._rot_wait = float(self.rot_stay.value())
            self._rot_settle = ROT_SETTLE
            if not self._rot_seq:
                self._rot_t = 0.0        # 繞完一圈了，重新計時
                self._rot_say(f"　巡迴完成，回到 {self._rot_home} 頻")
            return True

        # 沒在巡迴 → 累積時間，到了就排一輪
        self._rot_t += dt
        every = float(self.rot_every.value()) * 60.0
        if self._rot_t < every:
            left = every - self._rot_t
            self._rot_say(f"　下一輪巡迴還有 {_mmss(left)}")
            return False
        here = channel.current(self.hwnd)
        # ★ 分流數讀遊戲載進記憶體的伺服器清單，改版增減分流會自動跟上。
        #   ⚠ channel.count() 是**全記憶體掃描**（一次 0.3~1 秒）而這裡跑在
        #   GUI 執行緒 —— 所以只有第一次（_rot_max 還是 0）才掃，之後重用：
        #   分流數一個 session 內不會變；分頁只在分身重開時重建、
        #   快取自然歸零。讀不到就整輪跳過 ——
        #   寧可不換，也不要拿猜的數字去送。
        n = self._rot_max or channel.count(self.sc, self.hwnd)
        if here is None or not n:
            self._rot_t = 0.0
            self._rot_say(
                "　⚠ 讀不到目前頻道（視窗標題），這一輪跳過" if here is None
                else "　⚠ 讀不到分流數（伺服器清單），這一輪跳過")
            return False
        # 從目前這一頻繞一圈回來：在 3 頻（共 5 頻）→ 4,5,1,2,3
        self._rot_max = n
        self._rot_home = here
        self._rot_seq = [(here - 1 + i) % n + 1 for i in range(1, n + 1)]
        self._rot_wait = 0.0             # 下一拍就出發
        self._rot_say(
            "　開始巡迴：" + " → ".join(str(c) for c in [here] + self._rot_seq))
        return True

    # ------------------------------------------------------------------
    # -- 目前在哪張地圖 --------------------------------------------------
    def _read_scene(self) -> int | None:
        """目前場景編號。優先走靜態指標（1.5ms）；它失效才掃一次留著用。"""
        sid = scene.current_id(self.sc, allow_scan=False)
        if sid is not None:
            return sid
        # 走到這裡代表靜態指標失效（多半是遊戲改版位移）。全掃 0.25 秒，
        # 只做一次並把物件位址記下來 —— 心跳是 10ms 一拍，不能每次都掃。
        # ⚠⚠ 還要加**冷卻**：下面讀失敗時會把 _scene_scanned 放回 False，
        #   所以「掃到了但物件一直讀不到」（換地圖、重連時的正常現象）會變成
        #   每 SCENE_SAMPLE(0.5 秒) 就在 GUI 執行緒上全掃一次 0.25 秒 ——
        #   畫面等於半凍住。讀不到場景本來就是容許的狀態（各處都會處理 None），
        #   所以慢一點重試不影響任何判斷。
        if not self._scene_scanned:
            now = time.monotonic()
            if now - self._scene_try < SCENE_RELOCATE_GAP:
                return None
            self._scene_try = now
            self._scene_scanned = True
            found = scene.locate(self.sc)
            self._scene_obj = found.obj if found else None
        if self._scene_obj is None:
            return None
        s = scene.read_at(self.sc, self._scene_obj)
        if s is None:
            self._scene_obj = None      # 物件搬家了，下次重新定位
            self._scene_scanned = False
        return None if s is None else s.id

    def cur_scene(self) -> int | None:
        """給 UI 用的即時版（加入巡邏點時要用，不能拿半秒前的舊值）。"""
        self._scene = self._read_scene()
        self._scene_t = 0.0
        return self._scene

    def _spot_here(self, sid: int | None) -> bool:
        """這個巡邏點是不是「現在這張圖」的。

        沒標記地圖的舊點一律視為可以去 —— 我們不知道它當初存在哪，
        直接擋掉會讓使用者原本設好的巡邏路線突然全部失效。
        """
        if sid is None:
            return True
        return scene.same_map(sid, self._scene)

    def _scan_now(self) -> None:
        """「掃描周圍怪物」鈕：排一次掃描。

        ★ 只是把「距離上次掃描」推到門檻以上，下一拍心跳（10ms）就會送請求；
          **不繞過 `_waiting` 那個閂**，也不會多送重複的請求。
        """
        self._since_scan = SCAN_NOW
        if not self.run_cb.isChecked():
            self.status.setText("掃描中…")

    # ------------------------------------------------------------------
    # -- 巡邏點 --------------------------------------------------------
    def _refresh_spots(self) -> None:
        self.spot_list.clear()
        for n, (x, y, sid) in enumerate(self._spots):
            where = scene.scene_name(sid) if sid is not None else "未標記"
            self.spot_list.addItem(f"{n + 1}. {where} ({x:.0f}, {y:.0f})")
            self.spot_list.item(n).setToolTip(
                f"{where}　格子 ({x:.1f}, {y:.1f})\n"
                + (f"場景編號 {sid}" if sid is not None else
                   "舊版的點沒記地圖，建議刪掉重加。"))

    def _forget_routes(self) -> None:
        """巡邏點清單一動 → 導航整個重來（正在走的那一段的目標可能換人了）。

        ⛔ 這裡以前還會清掉「記住的路線」（_routes）。那份記憶 2026-08-10
          跟著 navigate 的探索式繞路一起刪了 —— 走路現在一律用地形圖算的
          最短路，A* 12~19ms 就給出**最佳**路線，記住一條走過的路只會更差
          （而且它是用點的編號當 key 的，插一個點就整個對錯人）。
        """
        self._nav.reset()

    def _add_spot(self) -> None:
        p = self.my_pos()
        if p is None:
            self.status.setText("讀不到位置，請先按「掃描周圍怪物」")
            return
        sid = self.cur_scene()
        self._spots.append((p[0], p[1], sid))
        self._forget_routes()
        self._refresh_spots()
        self._save_settings()
        where = scene.scene_name(sid) if sid is not None else "未知地圖"
        self.status.setText(
            f"已加入巡邏點 {len(self._spots)}：{where} ({p[0]:.0f}, {p[1]:.0f})"
            + ("" if sid is not None else "　⚠ 讀不到地圖，這個點不會做地圖比對"))

    def _remove_spots(self) -> None:
        for row in sorted((self.spot_list.row(i)
                           for i in self.spot_list.selectedItems()),
                          reverse=True):
            del self._spots[row]
        self._spot_i = 0
        self._forget_routes()          # 編號整個位移了，舊路線全部作廢
        self._refresh_spots()
        self._save_settings()

    def _set_home(self, x: float, y: float, sid: int) -> None:
        """換記錄點（回程補給／死亡回程要飛回的練功點）並更新顯示。"""
        self._home = (float(x), float(y), int(sid))
        self.home_lab.setText(
            f"記錄點：{scene.scene_name(sid)} ({x:.0f}, {y:.0f})")

    def _pick_home(self) -> None:
        """按「開始掛機」時定記錄點：**巡邏點優先**，不是人站的地方。

        2026-08-18 使用者要求：以前等於記「按開始當下的地圖」——在城裡按
        開始，補給／死亡回程就飛回城。改成：
          1. 目前地圖有巡邏點 → 目前地圖的第一個巡邏點；
          2. 否則 → 清單第一個有記地圖的巡邏點；
          3. 完全沒設巡邏點 → 當下位置（原行為）；讀不到就保留舊記錄點。
        """
        here = self.cur_scene()
        spots = [s for s in self._spots if s[2] is not None]
        if spots:
            pick = next((s for s in spots
                         if here is not None and scene.same_map(s[2], here)),
                        spots[0])
            self._set_home(pick[0], pick[1], pick[2])
            return
        pos = self.my_pos()
        if pos is not None and here is not None:
            self._set_home(pos[0], pos[1], here)

    def _spot_menu(self, pos) -> None:
        """巡邏點清單的右鍵選單：用天使趴趴GO飛到那個點所在的地圖。

        ⚠ 落點是趴趴GO在那張圖的傳送點（挑離巡邏點最近的），不是巡邏點
          本身 —— 掛機開著的話，落地掃不到怪自己會走去巡邏點。
        """
        it = self.spot_list.itemAt(pos)
        if it is None:
            return
        row = self.spot_list.row(it)
        if not 0 <= row < len(self._spots):
            return                      # 清單跟資料不同步就寧可不出選單
        x, y, sid = self._spots[row]
        menu = QMenu(self.spot_list)
        act = menu.addAction("✈ 用天使趴趴GO飛到這張圖（記錄點跟著改）")
        # 飛不了的原因直接寫在選單項上（QMenu 的滑鼠提示預設不顯示）
        if sid is None:
            act.setEnabled(False)
            act.setText("✈ 這個點沒記地圖，不能飛（刪掉重加）")
        elif jumpmap.nearest(sid, x, y) is None:
            # 表裡沒這張圖的落點（或 jumpmap.tsv 缺檔）→ 拒絕動作，不猜
            act.setEnabled(False)
            act.setText(f"✈ 趴趴GO沒有去{scene.scene_name(sid)}的傳送點")
        # pos 是 viewport 座標（QListWidget 的右鍵事件發在 viewport 上）
        if menu.exec(self.spot_list.viewport().mapToGlobal(pos)) is act:
            self._fly_to_spot(row)

    def _fly_to_spot(self, row: int) -> None:
        """真的送趴趴GO傳送包（右鍵選單觸發）。

        跟回程補給的 `_jump_step` 同一套路（nearest → teleport），但這是
        使用者手點的一次性動作：送不出去就把原因寫在狀態列，**不重試**
        （要重試再點一次右鍵就好，不值得掛計時器）。
        ⚠ teleport 只保證「封包送出去了」，到不到得看伺服器。
        """
        x, y, sid = self._spots[row]
        if sid is None:
            return
        e = jumpmap.nearest(sid, x, y)
        if e is None:
            self.status.setText(f"⚠ 趴趴GO沒有去{scene.scene_name(sid)}的傳送點")
            return
        if not self._ensure_mover():
            self.status.setText("⚠ 無法啟用移動，趴趴GO送不出去")
            return
        ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
        if not ok:
            self.status.setText(f"⚠ 趴趴GO送不出去（{msg}）")
            return
        # 傳送會換地圖：快取位址全作廢＋導航重來（跟 _jump_step 同一套）。
        self._drop_cached_addrs()
        self._forget_routes()
        # 飛過去＝記錄點也跟著過去（2026-08-18 使用者要求）
        self._set_home(x, y, sid)
        self.status.setText(f"✈ {msg}（記錄點已改成這個巡邏點）")

    def _ensure_mover(self) -> bool:
        """需要移動時才安裝 hook —— 沒用到就不要在遊戲裡放程式碼。

        ⚠ 一定要走 `move.acquire()`：同一個遊戲行程只能有一份跳板，
          自己 new 一份會把別的分頁（能量晶化）正在用的那份拆掉。
        """
        if self._mover is not None and self._mover.active:
            return True
        if self._mover_failed:
            return False                       # 之前裝失敗過，別一直重試
        try:
            self._mover = move.acquire(
                self.pid, injector.process_path(self.pid), self)
            return True
        except Exception as exc:               # noqa: BLE001
            self._mover = None
            self._mover_failed = True
            self.status.setText(f"⚠ 無法啟用移動：{exc}（掛機其他功能不受影響）")
            return False

    def _want_castwatch(self) -> bool:
        """誰需要施放廣播監聽：①掛機中有設首次攻擊（首發逐發確認）
        ②自動分身學到技能（補放要確認伺服器受理）。都不要就不該裝著。"""
        return bool((self.run_cb.isChecked() and self._keys.opener_vk)
                    or (self.buff_cb.isChecked() and self._buff.skill))

    def _sync_castwatch(self) -> None:
        """照需求裝／卸施放廣播監聽（inline hook 熱路徑：沒人用就別放進遊戲）。

        ⚠ 裝不起來（AOB 對不上／改版）castwatch.acquire 回 None → 記住失敗
          別每拍狂試；首發退化成「送一次就算」（_opener_gate）、補分身退化成
          「送出就當成功」（buff.step），都不會卡死。
        """
        if self._want_castwatch():
            if self._castwatch is not None and self._castwatch.active:
                self._keys.castwatch = self._castwatch
                return
            if self._cw_failed:
                return
            try:
                self._castwatch = castwatch.acquire(self.pid, self)
            except Exception:                  # noqa: BLE001
                self._castwatch = None
            self._keys.castwatch = self._castwatch
            if self._castwatch is None:
                self._cw_failed = True
                self.status.setText("⚠ 施放廣播監聽裝不起來（改版？）→ "
                                    "首發/補分身改『送出就算』，其餘不受影響")
            return
        self._release_castwatch()

    def _release_castwatch(self) -> None:
        """卸施放廣播監聽（最後一個使用者還完才真的卸 hook）。"""
        if self._castwatch is not None:
            try:
                castwatch.release(self.pid, self)
            except Exception:                  # noqa: BLE001
                pass
            self._castwatch = None
        self._keys.castwatch = None

    def _walk_toward(self, gx: float, gy: float, me, keep: float) -> int:
        """往 (gx,gy) 走，但在距離 keep 格處停下。有冷卻，不會狂送。

        ★★ 走的是 move.Mover.walk_route()：**對目標本身尋路**，走遊戲算出來
        的那條路，要留距離就沿著那條路往回退 —— 退到的點一定在它走得到的
        路段上。

        ⛔ 以前是「先在直線上取一個距離目標 keep 格的點，再對那個點尋路」。
           那個幾何點落在牆後面時尋路就失敗，往回縮短還是同一條直線、
           還是撞牆 —— 使用者實拍：站在原地 **32 秒**撞牆，而同樣的位置
           自己點地圖遊戲是走得過去的（那時它一包送 5 個繞路點）。

        回傳尋路算出的路徑點數（0 = 走不了，1 = 直線通，>1 = 中間有地形）。
        """
        if not self._ensure_mover():
            return 0
        gd = math.hypot(gx - me[0], gy - me[1])
        # ⛔ 「太近就後退站位」（卡進怪身體站開到 1.8）拿掉了 ——
        #   使用者 2026-08-07 指定：走位交棒給客戶端（停 10 格）之後
        #   基本上不會再卡進怪身體，被貼身就站著打、不後退
        #   （「退開一步」這類動作使用者本來就否決過）。太近一律不動。
        if gd <= keep:
            return 0
        # ★★★ **近距離一律不尋路**，直接朝目標微調位置。
        #   2026-08-06 實拍：雪狐站 2.2 格、怪滿血、沒在走，**卡 8.2 秒**。
        #   近戰打得到 2.0，只差 0.2 格卻走不過去 —— 因為 walk_route() 要先
        #   對怪尋路，而**尋路到貼身的目標一定回 0**（＝算路徑到自己腳下），
        #   中繼點又都比實際距離遠，整趟回 0、一步沒動。這就是使用者說的
        #   「發呆一段時間然後又開始打」（等到怪自己走過來才恢復）。
        #   走位本來就只差一兩格，用不著尋路，交給 walk_near 直接走。
        # ⚠⚠ **不尋路只適用於「確定沒地形」的情況**（使用者指出的疑點）：
        #   walk_near 是直線走，中間有障礙就會撞牆原地不動。判斷依據有兩個，
        #   都要顧到：
        #     ① 上一次尋路說要繞路（_path_pts > 1）→ 直接走 walk_route
        #     ② 擋線判斷每 _path_gap(0.2s) 才更新（2026-08-19 起貼身也走真的
        #        地形圖，不再一律當可通），節奏之間仍可能是舊的
        #        → 所以再加一道實測保險：連續走了兩次都沒真的位移，就改用尋路
        # ⚠ 再加一個條件 `self._line_clear`（2026-08-10）：`_path_pts <= 1` 在
        #   「地形圖讀不到」時也成立（那時它是 0），等於在沒有任何擋線判斷的
        #   情況下走直線。
        if (gd < NEAR_WALK and self._path_pts <= 1 and self._near_fail < 2
                and self._line_clear):
            # 上一次 walk_near 之後到底有沒有動？沒動就記一次失敗。
            if self._near_from is not None and me:
                if math.hypot(me[0] - self._near_from[0],
                              me[1] - self._near_from[1]) < 0.3:
                    self._near_fail += 1
                else:
                    self._near_fail = 0
            self._near_from = me
            ok = self._mover.walk_near(self.sc, self.player, gx, gy, keep)
            self._walk_t = 0.0
            return 1 if ok else 0
        self._near_from = None
        # ★★ 路徑一律用**我們自己算的**交給遊戲走，不再請它尋一次路
        #   （省 5~6ms，而且不佔指令槽 —— 那個槽攻擊也要用）。
        #     · 隔著地形 → 這一拍地形圖算好的繞路點（_way）
        #     · 直線可通 → 就是目標那一格（格子中心，跟遊戲的單點路徑一樣）
        #   ⚠ 只有「地形圖這一拍親口說直線可通」（_line_clear）才敢走直線。
        #     讀不到地形圖時 _line_clear 是 False，就退回讓 walk_route 自己
        #     問遊戲尋路 —— 拿一個可能過期的判斷去走直線會直接撞牆。
        # ⚠ 終點用**格子中心**不要用怪的浮點座標：踩到怪自己那一格時
        #   伺服器不給站，整段移動會被退回（見 move.walk_route 的說明）。
        # ⛔⛔ 沒有路徑點就**不走**（2026-08-10 使用者指定）。以前這裡是
        #   `points=None`，而 walk_route 收到 None 會去問遊戲的尋路，問不到
        #   就沿直線取 28/18/10/5 格的中繼點、再 ±40°/±70° 亂試 ——
        #   那就是「卡在牆邊」。現在只走**我們自己從地形圖算出來的路**。
        pts = self._way or ([(int(gx) + 0.5, int(gy) + 0.5)]
                            if self._line_clear else None)
        if not pts:
            return 0
        n = self._mover.walk_route(self.sc, self.player, gx, gy,
                                   stop_short=keep, points=pts)
        self._walk_t = 0.0
        return n

    def _my_id(self) -> int:
        """自己的實體 ID —— 施放封包的「目標」欄位要填它。

        ★ 實測跟使用者攔到的封包完全一致（0x47b403c2）。
        """
        if not self.player:
            return 0
        raw = self.sc._read_bytes(self.player + entity.OFF_ID, 4)
        return struct.unpack("<I", bytes(raw))[0] if raw else 0

    def my_pos(self) -> tuple[float, float] | None:
        """玩家目前的格子座標（每次都重讀，因為角色會走動）。

        ⚠⚠ 走 `entity.player_pos()`：它會**順便驗玩家物件還是不是玩家物件**
          （同一次讀取，免費）。物件搬家之後舊位址照樣讀得到，讀出來是別人的
          資料卻長得像一組合法座標 —— 而座標會留下後果（走路目的地、巡邏點、
          回程落點）。驗不過就回 None 並把快取作廢，下一次掃描自己重新定位。
        """
        if self.player is None:
            return None
        pos = entity.player_pos(self.sc, self.player)
        if pos is None:
            # 位址過期（或這一瞬間讀不到）→ 作廢＋催一次全掃，別再拿它算路。
            # 呼叫端本來就全部處理 None（「讀不到就不要動」）。
            self.player = None
            self._ask_full("玩家物件搬家了（座標驗不過）")
        return pos

    def apply_scan(self, s: Scan) -> None:
        """外殼：**不管中間出什麼事，`_waiting` 一定要放掉**。

        ⚠⚠⚠ 這支底下跑十幾次記憶體讀取（read_live／index_base／is_boss／
          pathfinder_this）再加上重建 Qt 清單，任何一步丟例外都會讓
          `_waiting` 永遠停在 True —— 那台分身的掃描就**再也不會發出下一次
          請求**，怪物清單從此定格（使用者 2026-08-09 回報「掃周圍怪物跑掉，
          然後一直找不到怪物」）。而且例外是在 Qt 訊號槽裡丟的，PySide6
          只會把 traceback 印到 stderr —— 打包成 exe 之後根本沒有人看得到，
          症狀就是「安靜地壞掉」。
        ★ 所以：try/finally 保證放閂，例外寫進狀態列讓它**大聲**。
        """
        try:
            self._apply_scan(s)
        except Exception as exc:                   # noqa: BLE001
            self._scan_err += 1
            self._dbg(f"套用掃描結果時出錯：{exc!r}")
            self.status.setText(
                f"⚠ 套用掃描結果時出錯（第 {self._scan_err} 次）：{exc}"
                "　—— 掃描會繼續，這一拍的結果丟掉")
        finally:
            self._waiting = False
            self._wait_t = 0.0

    def _apply_scan(self, s: Scan) -> None:
        # ⚠⚠⚠ 狀態物件**換位址或不見了** → 一定要先叫寫入執行緒停手。
        #   它手上的 `_job` 記著舊位址，而它每 20ms 就往 `舊位址 + 0x2D8`
        #   寫 4 bytes。物件搬家（換地圖、死亡重生、斷線重連、傳送）之後
        #   那塊記憶體早就是別人的了 —— 等於每秒 50 次亂改遊戲的堆積，
        #   遊戲會在很久以後跳「This program will be terminated…」掛掉，
        #   而且離真正的元凶已經很遠，完全看不出關聯。
        #   以前只有「我們自己觸發的傳送／換頻」有作廢位址（_drop_cached_addrs），
        #   角色自己死掉重生、使用者手動換地圖、伺服器重連都沒人管。
        if s.state != self.state:
            self._atk.hold_off()
            self._keys.set_on(False)
            self._keys.eid = None
            self._cur = None
            # ★ 本體搬家 ＝ 換地圖／換頻道／重連／重生 → 舊的怪物清單整份
            #   過期，不要留給底下那段「掃壞了就沿用上一拍」（SCAN_KEEP_BAD）。
            #   ⚠ 講清楚**這不是在防打錯怪**：`entity.read_live()` 會當場驗
            #     vtable＋eid（entity.py `alive`），位址被新場景重用時身分對
            #     不上就 `alive=False`，`_pick_next` 直接跳過 —— 那條路本來
            #     就安全（[[boss-attack-triggers]] 已排除過這個假說，別重查）。
            #   清掉是為了另外兩件事：`_pick_next` 不必每輪白跑一趟死清單，
            #   以及「周圍怪物」那份清單不會停在上一張圖的怪名上。
            self.mons = []
            self.pets = []
        self.state = s.state
        self.player = s.player
        self.stats = s.stats
        self.inv = s.inv
        # ⚠⚠ **「這一拍掃壞了」≠「周圍沒有怪」**（跟背包那條「讀不到 ≠ 沒有」
        #   是同一個病，見 memory 的 bag-false-empty-guards）。
        #   s.err 有東西＝掃描失敗，或狀態／玩家物件掃到 0 個或多個 ——
        #   那一拍的清單根本不可信，拿它覆蓋會讓「周圍怪物」瞬間清空、
        #   掛機當場沒目標（使用者回報的「掃周圍怪物跑掉」）。
        # ★ 保留上一拍的清單，最多撐 SCAN_KEEP_BAD 拍（之後寧可清空，
        #   免得換地圖後一直拿舊圖的怪）。留著是安全的：挑目標時每一隻都會
        #   當場重讀驗證（_pick_next 的 read_live，物件沒了就跳過）。
        self._bad_scans = self._bad_scans + 1 if s.err else 0
        if s.err and self.mons and self._bad_scans <= SCAN_KEEP_BAD:
            self._ask_full(f"掃描不可信（{s.err}）")
        else:
            # ★ 掃描端漏怪的保險：上一拍還在的活怪這一拍不見了，物件卻還在
            #   原位址 → 掃描漏了那一塊（唯讀實測這很罕見，1/523 —— 消失多半
            #   是遊戲自己回收物件，那種 read_live 會失敗，不會誤觸發）。
            #   抽驗最多 3 隻，只是要決定「要不要補一次全掃」。
            if not s.err and self.mons:
                cur_eids = {m.eid for m in (s.mons or [])}
                lost = [m for m in self.mons if m.eid not in cur_eids]
                for m in lost[:3]:
                    alive, st, _p = entity.read_live(self.sc, m)
                    if alive and st != "Dead":
                        self._ask_full(f"{len(lost)} 隻怪從掃描消失但物件還在")
                        break
            self.mons = s.mons or []
            # kind=4 跟著同一套規則走：掃壞了沿用上一拍（自動召喚的認養
            # 有時間窗，用到的每一隻也都會 read_live 重驗，舊清單無害）。
            self.pets = s.pets or []
        # 送鍵執行緒要的兩樣東西，跟著掃描一起更新（物件會搬家）：
        #   stats —— 學技能 ID 用（角色屬性基準 −0x50）
        #   pf    —— 三連包第①包的參數，**玩家物件 −8**（純讀取算得出來）
        self._keys.stats = s.stats
        self._keys.pf = move.pathfinder_this(self.sc) if s.player else None
        # 跳板可能是走位那邊裝上的（比開始掛機晚），所以跟著更新
        self._keys.mover = self._mover if (
            self._mover is not None and self._mover.active) else None
        err = s.err
        # 只列中文名字（去重、不顯示數量、不顯示任何 ID）。
        # 掛機時每秒都在刷新，內容沒變就別重建清單 —— 不然使用者的選取會一直被清掉。
        # ★ 王的前面加「【王】」（讀遊戲自己的怪物資料，不是靠名字猜）。
        #   ⚠ 顯示文字加了字首，所以**名字要另外存在 UserRole** —— 點名字是拿它
        #     加進「選中怪物」的，直接用顯示文字會加成「【王】水晶傘蜥蜴」。
        #   ⚠ 不要用 👑：中文字型沒有那個字形，會變豆腐方塊（離屏截圖實拍到）。
        idx = monsters.index_base(self.sc)
        seen: list[tuple[str, str]] = []          # (顯示文字, 真正的名字)
        names: set[str] = set()      # 去重用（150 隻怪 × 每秒刷新，線性掃太貴）
        for m in self.mons:
            if m.name in names:
                continue
            names.add(m.name)
            crown = "【王】" if monsters.is_boss(self.sc, m.type_id, idx) else ""
            seen.append((crown + m.name, m.name))
        # ⚠⚠ 排序**不能省**。清單本來是照 self.mons 的順序排的，而那是**掃描
        #   命中的記憶體位址順序** —— 怪一生一死就整個洗牌。刷新從 1.5 秒加快到
        #   0.15 秒之後，同一批怪會變成每 0.15 秒跳一次位置，使用者滑鼠移過去
        #   正要點，那一行已經換人了。照名字排之後「內容沒變 → 畫面完全不動」。
        seen.sort(key=lambda p: p[1])
        self._sync_near(seen)

        if err:
            self.status.setText(
                f"⚠ {err}"
                + (f"（連續 {self._bad_scans} 拍，先沿用上一拍的怪物清單）"
                   if self._bad_scans and self.mons else ""))
            return

        # ★ 正在打的那隻已經不在掃描結果裡 → 牠死了（或離開視野）。
        # 這是**獨立於血量的死亡訊號**：使用者回報「打死了卻還在選他」，
        # 原因是我們每輪把目標血量寫回 100，等於把遊戲的死亡訊號蓋掉了
        # （`read_target_hp() or 100` —— 死掉時讀到 0，`0 or 100` 就變 100）。
        # ⚠ 要**連續兩次**掃不到才算 —— 熱區掃描偶爾會漏掉一隻，
        #   單次就放棄會在打鬥中間換目標（使用者回報「打一下就換下一隻，
        #   結果一路結仇被打死」）。
        if (self.run_cb.isChecked() and self._cur is not None
                and not any(m.eid == self._cur.eid for m in self.mons)):
            # ★ 判死之前**先去舊位址驗一次物件**。「掃不到」≠「死了」。
            #   唯讀實測（5 台 × 300 秒）：活怪從清單消失又帶著同一個 eid
            #   回來的事件有 15~104 件／台（中位 6~22 秒）——多半是遊戲自己
            #   回收物件（視野剔除），但也拍到過物件還在、純粹掃描漏掉的
            #   （1/523）。物件還在而且不是屍體 → 一定是掃描端漏了：照打
            #   （攻擊執行緒拿的是位址，不受清單影響），並補一次全掃。
            #   物件沒了才走原本的兩拍判死。
            alive, st, _p = entity.read_live(self.sc, self._cur)
            if alive and st != "Dead":
                self._gone = 0
                self._ask_full(f"目標「{self._cur.name}」被掃描漏掉（物件還在）")
            else:
                self._gone += 1
                if self._gone >= GONE_SCANS:
                    self._on_died(self._cur.eid)
                    return
        else:
            self._gone = 0

        # 掛機中且正在等下一隻 → 自動挑名字在清單裡、離自己最近的接上去
        if self.run_cb.isChecked():
            if self._cur is None and not self._pick_next():
                self.status.setText(
                    f"附近沒有選中的怪了（已擊殺 {self._kills} 隻）→ 等新的出現…")
            return          # 掛機中的狀態列由 tick() 負責，別蓋掉
        # 沒在掛機時：掃描一直在背景跑，狀態列**不要**跟著一直重寫，
        # 只有內容真的變了才更新一次（不然又變成一直跳的那種）。
        msg = f"周圍 {len(self.mons)} 隻、{self.near.count()} 種"
        if self.status.text() != msg:
            self.status.setText(msg)

    def _near_name(self, row: int) -> str:
        """「周圍怪物」第 row 列的**真正名字**（顯示文字可能有「【王】」字首）。"""
        it = self.near.item(row)
        return "" if it is None else (it.data(Qt.UserRole) or it.text())

    def _sync_near(self, want: list[tuple[str, str]]) -> None:
        """把「周圍怪物」清單**就地**改成 want（(顯示文字, 名字)，已依名字排序）。

        ⚠⚠ 不可以 `clear()` 重建。重建會清掉使用者的選取，而且重建那一瞬間
          按下去的滑鼠會落到新的一行上 —— 刷新加快到 0.15 秒之後，撞上的機會
          變成十倍。這裡只動**真的有變**的那幾列：怪的種類沒變（絕大多數的拍）
          就一個 Qt 物件都不碰，畫面完全不會閃。
        ★ 兩邊都照名字排序，所以一趟合併掃描就對得齊：
          清單裡排在前面而 want 沒有的 → 刪掉；對得上 → 留著（只有文字不同才
          改，例如同名怪從小怪換成【王】）；want 有而清單沒有 → 插進去。
        """
        i = 0
        for text, name in want:
            # 排在這個名字前面的，代表已經不在附近了
            while i < self.near.count() and self._near_name(i) < name:
                self.near.takeItem(i)
            if i < self.near.count() and self._near_name(i) == name:
                if self.near.item(i).text() != text:
                    self.near.item(i).setText(text)
            else:
                it = QListWidgetItem(text)
                it.setData(Qt.UserRole, name)
                self.near.insertItem(i, it)
            i += 1
        while self.near.count() > i:               # 尾巴多出來的也清掉
            self.near.takeItem(i)

    def _dbg(self, msg: str) -> None:
        """事件型的診斷紀錄（AO_FARM_LOG=1 才寫），跟每秒的決策行同一個檔。

        ★ 使用者回報「沒優先打最近的」「10 秒沒反應」時，每秒一行的決策
          看不出**挑目標當下**發生什麼事 —— 挑中誰、跳過了誰、為什麼跳過，
          只有在 _pick_next 裡當場記下來才對得回去。
        """
        if not _FARM_LOG:
            return
        try:
            with open(f"farm_debug_{self.account}.log", "a",
                      encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except OSError:
            pass

    def _ask_full(self, why: str = "") -> None:
        """請下一次掃描做全掃（有節流，最快 FULL_HUNT_GAP 一次）。

        熱區掃描漏掉整塊時的補救 —— 見 FULL_HUNT_GAP 的說明。
        """
        now = time.monotonic()
        if now - self._full_req_t < FULL_HUNT_GAP:
            return
        self._full_req_t = now
        self._want_full = True
        if why:
            self._dbg(f"要求全掃：{why}")

    def _cool_unreach(self, eid: int) -> None:
        """把「走不到」的怪冷凍起來，**時間隨失敗次數翻倍**。

        暫時算不出來的（角色站的位置的問題）8 秒後就會再試；
        真的站在走不進去的地形上的（實測：曼陀羅怪菇一律回 0），
        會一路翻倍到 UNREACH_MAX，等於自動淡出，不再浪費時間。
        """
        n = self._unreach_n.get(eid, 0)
        self._unreach_n[eid] = n + 1
        wait = min(UNREACH_MEMORY * (2 ** n), UNREACH_MAX)
        self._killed[eid] = time.monotonic() + wait
        self._dbg(f"冷卻（走不到 ×{n + 1}）eid={eid:#x} {wait:.0f} 秒"
                  + (f"　{self._why}" if self._why else ""))

    def _reach_set(self, me) -> tuple[object, set | None]:
        """我現在站的這一塊連通區（走得到的所有格）；讀不到地形圖回 (grid, None)。

        ★★★ 為什麼挑目標一定要有它：`_candidates` 是照**直線距離**排的，
          而直線距離跟「走不走得到」完全是兩回事 —— 河對岸那隻直線 12 格
          就贏過同岸 15 格的，鎖上去才發現過不去。（2026-08-10 之前「走不到
          就換一隻」那條規則還有 25 格上限，更遠的**根本不會觸發**，
          只能等 15 秒沒進展 → 冷卻 8 秒 → 牠又是最近的 → 再挑一次，
          就是使用者看到的「掃到對岸的怪就卡住」。）
          先泛洪一次把不同連通區的怪整個排除，這個迴圈就不存在了。

        ★★ 2026-08-10 起這一支還多了一個責任：**它是「走不到」的唯一權威**。
          走路那邊的 A* 已經沒有距離／繞路倍數上限了，所以「算不出路徑」
          必須真的等於走不到 —— 靠這裡先把不同連通區的怪剔掉，A* 才不會
          去展開一整片走不通的地方（那才是它唯一會變慢的情況）。

        ⚠ 泛洪一次約 33ms，**一定要快取**：只有換地圖（grid 物件換掉）或
          自己跑到別的連通區（傳送、走過門）才重算，平常每拍只是一次
          set 查詢（微秒級）。
        ⚠ 起點落在不可走格（站在縫裡、剛傳送完）→ 先放寬到最近的可走格；
          還是不行就回 None = **不過濾**（安全退化，維持舊行為）。
        """
        grid = self._maps.get(self.sc)
        if grid is None or not me:
            return grid, None
        tile = (int(me[0]), int(me[1]))
        # ⚠⚠ 快取的鍵要用**放寬後的起點**，不能用原始格：角色偶爾會站在
        #   判定為牆的格子上（一格寬的縫、剛傳送完），那時原始格永遠不在
        #   泛洪結果裡 → 每次呼叫都重跑一次 33ms 的泛洪（每秒好幾次）。
        #   nearest_open 對可走格是 O(1)（直接回自己），所以這行幾乎不花錢，
        #   而且它的結果一定在自己那次泛洪的集合裡 —— 快取必定命中。
        start = grid.nearest_open(*tile)
        if (start is not None and self._reach is not None
                and self._reach_grid is grid and start in self._reach):
            return grid, self._reach          # 還在同一塊，直接用
        got = grid.reachable(*start) if start else None
        self._reach, self._reach_grid = got, grid
        if got is not None:
            self._dbg(f"重算連通區：站 {tile}，這一塊有 {len(got)} 格")
        return grid, got

    def _path_cost(self, grid, me, pos, max_cost: float | None = None
                   ) -> float | None:
        """從我這裡走到 pos 的**實際路徑長度**（格）；走不到／超過上限回 None。

        ⚠ 回傳的是幾何成本（直走 1、斜走 √2），跟直線距離同單位 —— 兩者混用
          會讓「近多少」的門檻失去意義。
        ⚠ max_cost 一定要給：走不到的目標最貴（A* 要把整片展開完才敢說 None）。
        """
        if grid is None or not me or not pos:
            return None
        path = grid.route((int(me[0]), int(me[1])),
                          (int(pos[0]), int(pos[1])), max_cost=max_cost)
        if not path:
            return None
        tot = 0.0
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            tot += _SQRT2 if (x0 != x1 and y0 != y1) else 1.0
        return tot

    def _nearest_by_path(self, pool, grid, me, cap: float | None = None):
        """在候選裡挑出**路徑最短**的那一隻 → (路徑長度, 直線距離, 怪)；沒有回 None。

        ★★ 這就是使用者要的「近＝我們自己算出來的路徑，不是無腦直線」。
        ★ 效率靠一條數學性質：**直線距離永遠 ≤ 路徑長度**。所以照直線排序後，
          一旦手上最好的路徑長度已經 ≤ 下一隻的直線距離，後面**不可能**更近，
          直接收手 —— 實務上只會算兩三次 A*，不是每隻都算。
        ⚠ 每次 route() 都把 max_cost 設成「目前最好的成績」——那是**純粹的
          剪枝**（比目前最好的還長就不必算完），不是「走不到」的判準。
        cap: 還沒有任何成績時的上限（None = 不設限，照樣把路算完）。
          ⛔ 這裡以前預設 `max(直線×3, 30)`：繞路遠的怪整批算不出來 →
            退回直線最近 → 挑到牆對面那隻。已刪。
        """
        if grid is None or not me:
            # ⚠ 讀不到地形圖 → **退回直線距離**（安全退化 = 舊行為）。
            #   不能因此整個不挑／不換：那會讓功能安靜地消失。
            for d, mon, _pos in pool:
                if cap is not None and d >= cap:
                    break
                return (d, d, mon)
            return None
        best = None                      # (路徑長度, 直線距離, 怪)
        for d, mon, pos in pool:
            limit = best[0] if best is not None else cap
            if limit is not None and d >= limit:
                break                    # 直線就輸了 → 後面排序更遠的更不用算
            # ⛔ 「還沒有成績時用 max(直線×3, 30) 當上限」**拿掉了**
            #   （2026-08-10）。那道上限的語意是「繞超過 3 倍就當走不到」，
            #   於是繞路遠的怪整批算不出來 → 下面退回「直線最近」→ 挑到的
            #   正好是牆對面那隻（使用者回報的症狀）。
            #   ★ 不必怕慢：pool 已經被連通區泛洪過濾過，這裡的 A* 一定
            #     找得到路（貴的是「走不到」，那個情況不存在了），
            #     而且第一隻算完之後就有 limit 可以夾。
            c = self._path_cost(grid, me, pos, max_cost=limit)
            if c is not None and (best is None or c < best[0]):
                best = (c, d, mon)
        return best

    def _candidates(self, quiet: bool = False) -> tuple[
            list[tuple[float, entity.Entity, tuple[float, float] | None]],
            list[tuple[float, str, str]] | None]:
        """照規則挑出「現在打得了」的怪，**照距離排序**（近→遠）。

        回傳 (候選, 被跳過的診斷紀錄)。挑目標與「有沒有更近的」兩條路都走這一支
        —— 規則只能有一份，兩邊各寫一套遲早會不一致。
        quiet=True 時不寫診斷紀錄（「偷看一眼」用的，免得每秒灌一次檔案）。

        用名字比對而不是種類 ID，因為使用者要能手動輸入怪物名稱。

        ★ 一定要按距離排序。實體串列是靠掃 vtable 得到的，順序等同記憶體位址，
          直接拿第一隻常常是地圖另一頭那隻 —— 這就是先前「有時打得到、
          有時打不到」的原因。現在怪和玩家都讀得到座標，排序即可，
          不需要一隻一隻試。

        ★ 這裡**不重掃記憶體**，直接用上一次掃描的清單。一張圖上通常有十幾隻，
          打死一隻就重掃等於白等 2 秒（1.5 秒間隔 + 0.5 秒掃描），
          而換下一隻其實只要挑清單裡還活著的即可 —— 這是換怪速度的關鍵。
          清單裡真的沒得打了，才由 tick() 去排重掃。
        """
        # ⛔ 這裡以前有「休息收尾中只挑正在打我的怪」（only_foes）那一整段。
        #   坐下休息 2026-08-09 移除，那段一起走 —— 現在永遠是「照名單挑最近的」。
        want = self.wanted()
        me = self.my_pos()
        now = time.monotonic()
        # ★ 只打王：完全不看名字，改看種類 ID 的王旗標。
        #   索引表位址先解一次，迴圈裡每隻只要兩次 4-byte 讀取。
        boss_only = self.boss_cb.isChecked()
        idx = monsters.index_base(self.sc) if boss_only else None
        # _killed 存的是「到期時間」，不是記錄時間 —— 因為兩種跳過的冷卻長度
        # 不一樣（確定打死 KILL_MEMORY、只是沒給血量 NOHP_MEMORY）。
        for eid, until in list(self._killed.items()):
            if now > until:
                del self._killed[eid]
        # (直線距離, 怪, 牠這一拍的座標)。座標要一起帶著 —— 後面算路徑長度
        # 時才不必再讀一次記憶體，也才確定用的是同一份快照。
        pool: list[tuple[float, entity.Entity, tuple[float, float] | None]] = []
        # 診斷用：被跳過的怪（距離, 名字, 原因）。平常是 None，完全不花錢。
        skipped: list[tuple[float, str, str]] | None = (
            [] if (_FARM_LOG and not quiet) else None)
        # ★★★ 走不走得到，**挑之前**就要問（見 _reach_set）。
        grid, reach = self._reach_set(me)
        for m in self.mons:
            # ★ eid=0 的實體絕不能挑（唯讀實測：場上真的會出現，多半是屍體
            #   但也拍到過活狀態 —— 剛生成還沒填 ID／回收中被清掉 ID）。
            #   挑到它整條攻擊鏈都會空轉：KeyWorker 對 eid=0 不送「選定」
            #   也不送攻擊（0 = 沒目標），屍體偵測因為 selected 恆為 False
            #   也不會啟動 —— 就是站著發呆到 10 秒逾時換一隻（使用者回報的
            #   「看著一隻掛不動，10 秒沒反應」）。
            if not m.eid:
                continue
            # ★ 死活、動畫狀態、座標**一次讀回來**（相鄰欄位，見 read_live）。
            #   底下每個判斷（解禁、收尾、屍體、距離）都用同一份快照。
            #   座標當場讀（怪會走、角色也在走，掃描時記的早就過期了）。
            alive, st, p = entity.read_live(self.sc, m)
            d = (math.hypot(p[0] - me[0], p[1] - me[1])
                 if p and me else float("inf"))
            if m.eid in self._killed:
                # ★★ 冷卻中的怪**正在打我**就立刻解禁。冷卻的用途是別浪費
                #   時間在屍體／走不到的怪身上 —— 但打得到我的怪，我一定
                #   打得到牠，冷卻的理由不成立。實際案例（使用者的第 3 個
                #   回報）：被判走不到而冷凍 → 牠追過來咬人 → 挑目標永遠
                #   跳過牠 → 打別隻打到被咬死。
                #   ⚠ 只解「打我的」；不動其他冷卻、也不改「純粹挑最近的」
                #   規則（貼身的怪距離最近，排序自然輪到牠）。
                # ★★ 判定用聯集 _fighting_me（單看交戰槽會漏，實錘），
                #   再加一道保底：**自己 HP 在掉**時 UNFREEZE_NEAR 格內的
                #   冷卻活怪一律解禁 —— 咬人的怪常常正是被記成「走不到」
                #   冰起來的那隻，而牠出手當下欄位是空的、動畫又只有幾拍，
                #   掉血＋距離是唯一一定抓得到的組合。
                fighting = (alive and st != "Dead" and self.player
                            and self._fighting_me(m, st, p, me))
                if fighting or (alive and st != "Dead"
                                and self._under_attack()
                                and d <= UNFREEZE_NEAR):
                    del self._killed[m.eid]
                    self._unreach_n.pop(m.eid, None)
                    self._dbg(f"解除冷卻：「{m.name}」eid={m.eid:#x} "
                              + ("正在打我" if fighting
                                 else f"我在掉血且牠在 {d:.1f} 格內"))
                else:
                    if skipped is not None and me and m.name in want:
                        skipped.append((d, m.name, "冷卻中還剩 "
                                        f"{self._killed[m.eid] - now:.0f} 秒"))
                    continue
            # ⛔ 這裡以前有 only_foes（休息收尾只反擊正在打我的怪）那一段，
            #   跟坐下休息一起在 2026-08-09 移除。**它是「打到沒選的怪／
            #   跑去打王」最大的暴露面**：判定裡的「猜的」證據不知道那隻怪
            #   在打誰，搶怪區隨時都有一堆怪符合。現在挑目標只有一條規則 ——
            #   照「選中怪物」名單（或只打王）挑最近的，沒有例外分支。
            if boss_only:
                # ⚠ is_boss 回 None 代表「查不到」（改版位移之類）——
                #   這種模式下一律不打，寧可不動也不要打到不該打的。
                if monsters.is_boss(self.sc, m.type_id, idx) is not True:
                    continue
            elif m.name not in want:
                continue
            if not alive:
                if skipped is not None and me:
                    skipped.append((d, m.name, "物件沒了"))
                continue
            # ★★ 屍體直接跳過（別人先殺掉的）。動畫狀態當場重讀 ——
            #   掃描到現在可能已經過了 0.3 秒，牠剛好是在那之間倒下的。
            #   ⚠ 這是搶怪區「卡卡的」的主因：實測任何一個瞬間清單裡有
            #     中位 20%、最高 50% 是屍體，而且屍體會賴著中位 5 秒
            #     （最久 79.8 秒）。挑最近的就常常挑到牠們，鎖上去要等
            #     CORPSE_SECS 才發現不對 —— 每次白花快一秒。
            #   ⚠ is_alive() 擋不掉：它只比對 vtable + 實體 ID，分不出屍體。
            if st == "Dead":
                if skipped is not None and p and me:
                    skipped.append((d, m.name, "屍體"))
                continue
            # ★ 不限距離：多遠的怪都收進來（使用者要求「想打多遠都可以」），
            #   排序後自然會先打最近的，遠的靠移動封包導航過去。
            # ⛔ 這裡曾經有「正在打我的排最前面」（entity.attacking）——
            #   **拿掉了**。沒有距離限制的話，20 格外的仇人會贏過 13 格的
            #   正常目標（唯讀監控實拍：目標 20.3 格，周圍就有 13.1/13.3 格），
            #   然後為了追那隻遠的去撞牆。
            #   使用者的判斷：純粹挑最近的就好 —— 打我的怪本來就貼在身上，
            #   排序自然會先輪到牠們。
            # ★★★ 但**走不到的一律不收**：跟我不在同一塊連通區的怪（河對岸、
            #   島上、牆圍起來的），直線再近也只是拿來卡住自己（見 _reach_set）。
            #   ⚠ 牠站的那格不可走時放寬到附近的可走格再問 —— 怪偶爾會站在
            #     判定為牆的格子上，直接刷掉會漏打。
            #   ⚠ 讀不到地形圖 → reach 是 None → **完全不過濾**（安全退化）。
            if reach is not None and p is not None:
                t = (int(p[0]), int(p[1]))
                if t not in reach:
                    t2 = grid.nearest_open(*t)
                    if t2 is None or t2 not in reach:
                        if skipped is not None and me:
                            skipped.append((d, m.name, "走不到（不同連通區）"))
                        continue
            pool.append((d, m, p))
        pool.sort(key=lambda t: t[0])
        if not pool and skipped and now - self._dbg_empty_t >= 5.0:
            # 挑不到時每 0.3 秒就會再試一次，照實寫會灌爆檔案 —— 節流 5 秒。
            self._dbg_empty_t = now
            dd, name, why = min(skipped)
            self._dbg(f"挑不到目標：清單 {len(self.mons)} 隻、"
                      f"被跳過 {len(skipped)} 隻；"
                      f"最近的是 {name} {dd:.1f} 格（{why}）")
        return pool, skipped

    def _pick_next(self) -> bool:
        """挑**路徑最短**的一隻接著打；挑不到回傳 False。

        ★★ 「最近」＝我們自己 A* 算出來的路徑長度（使用者指定），不是直線距離：
          隔著一道牆的怪直線 8 格、實際要繞 40 格，比同一條路上 12 格的還遠。
        ⚠ 讀不到地形圖就退回直線排序（安全退化，維持舊行為）。
        """
        pool, skipped = self._candidates()
        if not pool:
            return False
        grid, _ = self._reach_set(self.my_pos())
        best = self._nearest_by_path(pool, grid, self.my_pos())
        if best is None and grid is not None:
            # ★★ 有地形圖卻**每一隻都算不出路** = 真的沒有走得到的怪。
            #   ⛔ 以前這裡會退回「直線最近的那隻」—— 那等於明知走不到還鎖上去，
            #     然後走去牆邊卡著（使用者回報的症狀）。現在當作沒目標，
            #     交給上層重掃／巡邏。
            self._dbg(f"候選 {len(pool)} 隻**全部算不出路徑** → 這一輪不挑")
            return False
        if best is None:                 # 沒地形圖 → 照直線挑（安全退化）
            d, mon, _p = pool[0]
        else:
            cost, d, mon = best
            if cost > d + 0.5:           # 有繞路才值得記一筆
                self._dbg(f"挑「{mon.name}」：直線 {d:.1f} 格、實走 {cost:.1f} 格")
        self._engage(d, mon, skipped)
        return True

    def _switch_closer(self, cur, dist: float | None) -> bool:
        """趕路途中冒出**明顯更近**的怪就改打牠；真的換了回傳 True。

        ★ 使用者要的行為：周圍怪物本來就一直在刷新，出現更近的就去打那隻，
          不必傻傻走完一整段路。

        ★★★ 「近」比的是**我們自己 A* 算出來的路徑長度**，不是直線距離
          （使用者指定）—— 直線 5 格但要繞過一整片湖的怪一點都不近。
          目前這隻也一樣重算一次實走距離，兩邊同一把尺才比得準。
        ⚠⚠ **打傷過的絕不換**（使用者明確指定：打到之後一定要確定牠死了才能換）。
          換掉等於留一隻結仇的怪在背後追著咬 —— 以前被圍毆致死就是這樣來的。
        ⚠ 要近 SWITCH_GAIN 格以上才換：兩隻怪距離差不多時會互相取代，
          路線一直重算等於原地打轉（乒乓，見 [[patrol-navigator-bounce]]）。
        ⚠ 節流 SWITCH_GAP 秒一次；候選清單本身是現成的，不重掃記憶體。
        """
        now = time.monotonic()
        if self._hurt or dist is None or now < self._switch_t:
            return False
        self._switch_t = now + SWITCH_GAP
        pool, _ = self._candidates(quiet=True)
        me = self.my_pos()
        grid, _reach = self._reach_set(me)
        # 目前這隻的**實走距離**。算不出來（繞太遠／被地形圍住）就當成無限遠，
        # 任何走得到的怪都比牠好 —— 那正是「掃到對岸的怪」該有的結果。
        cur_pos = next((p for _d, m2, p in pool if m2.eid == cur.eid), None)
        # ⚠ 這裡也**不設上限**（2026-08-10）：設了的話「繞得比較遠但走得到」
        #   的目標會被當成無限遠，於是每秒都想換一隻（乒乓）。
        cur_cost = self._path_cost(grid, me, cur_pos)
        if grid is None:
            cur_cost = dist              # 沒地形圖 → 退回直線比（安全退化）
        elif cur_cost is None:
            cur_cost = float("inf")
        # 上限直接設成「要贏過的那條線」：A* 一超過就放棄，省掉沒必要的展開。
        best = self._nearest_by_path(
            [t for t in pool if t[1].eid != cur.eid], grid, me,
            cap=None if cur_cost == float("inf") else cur_cost - SWITCH_GAIN)
        if best is None or best[0] > cur_cost - SWITCH_GAIN:
            return False
        cost2, d2, m2 = best
        self._dbg(f"改打更近的：「{cur.name}」實走 {cur_cost:.1f} 格 → "
                  f"「{m2.name}」實走 {cost2:.1f} 格（直線 {d2:.1f}）")
        # ⚠ 順序照其他換目標的路徑：先把上一隻整個放掉（不寫目標、不出手），
        #   再鎖新的 —— 中間那一瞬間 eid 是 None，攻擊執行緒就不會多打一下
        #   舊目標（見 KeyWorker.step 開頭「沒有目標就絕對不出手」）。
        self._atk.hold_off()
        self._cur = None
        self._keys.eid = None
        self._engage(d2, m2, None)
        return True

    def _engage(self, d: float, mon, skipped) -> None:
        """鎖定這一隻：把所有「跟目標綁在一起」的狀態全部重設，再通知兩條執行緒。

        ⚠⚠ 換目標**只准走這一支**。漏掉任何一個欄位，上一隻的狀態就會被帶進
          新目標（卡住秒數、走不到次數、有沒有打傷過…），症狀通常是
          「剛換目標就馬上又被放棄」。
        """
        me = self.my_pos()
        boss_only = self.boss_cb.isChecked()
        idx = monsters.index_base(self.sc) if boss_only else None
        self._cur = mon
        self._stuck = 0.0
        self._anchor = me                # 換目標 → 卡住偵測從這裡重新算
        self._path_pts = -1                       # -1 = 還沒算，tick() 會去問尋路
        self._line_clear = False                  # 還沒問過地形圖，先別走直線
        self._no_grid = ""
        self._path_t = PATH_GAP                   # 下一拍就算
        self._path_gap = PATH_GAP                 # 換目標 → 節奏重來
        self._way = []
        self._unreach = 0
        self._hurt = False           # 換了新目標 → 又回到「還沒打傷，可以再換」
        self._push_in = False        # 貼身繞打是跟上一隻綁的，換目標歸零
        self._switch_t = 0.0
        self._handoff_fail = False   # 這一隻的「交棒給客戶端」失敗過了嗎
        self._handoff_t = 0.0
        self._near_fail = 0          # 換目標就重算「近距離直線走」的失敗次數
        self._near_from = None
        self._gone = 0
        self._walked_ok = True
        self._why = ""
        self._last_hp = -1
        self._atk.attack(self.state, self._cur)   # 寫入執行緒：開始鎖定這隻
        self._keys.eid = self._cur.eid            # 送封包時要指名打誰
        self._keys.set_on(True)                   # 攻擊執行緒：開始發動
        if skipped is not None:
            near = sorted(s for s in skipped if s[0] < d)[:5]
            if near:
                self._dbg(f"挑中「{self._cur.name}」{d:.1f} 格；跳過更近的："
                          + "、".join(f"{n} {dd:.1f}格（{r}）"
                                      for dd, n, r in near))
        info = monsters.info(self.sc, self._cur.type_id, idx) if boss_only else None
        self.status.setText(
            ("【只打王】　" if boss_only else "")
            + f"鎖定「{self._cur.name}」"
            + (f"（{info}）" if info else "")
            + f"　距離 {d:.1f} 格　累計擊殺 {self._kills}")

    def _adopt_buff_skill(self) -> None:
        """自動分身的技能編號：**直接讀快捷欄的 F12 那一格**，不必按鍵。

        ★ 讀得到技能 → `adopt()` 收下並存進設定，之後全走封包。
        ⚠⚠ **每 2 秒都要重讀，不是「還沒學過才讀」**（2026-08-20 實機根因）：
          舊版只在 `not self._buff.skill` 時呼叫，設定檔存的舊編號會**永遠**
          蓋住現況 —— 北極狐設定裡是 5424 單體分身Ⅳ、F12 上其實是 5471
          雙體分身Ⅰ，於是工具一路在放一招他 F12 上根本沒有的技能：分身當然
          不會出現，而且**永遠等不到那一招的施放廣播**，就一直重放
          （使用者回報「無法分身、一直重複卡補發」的真根因）。
          自動召喚那邊本來就是無條件重讀的（`_adopt_summon_skill`），這裡跟上。
        ⚠ 那一格是空的／放物品／放的不是 buff（查不到持續時間）→ `block()`
          停手並在狀態列說清楚。**不要無限重試按鍵**：那是確定的狀態，
          不是暫時性失敗，而且每按一次就在 GUI 執行緒 sleep 40ms
          （白狐 2026-08-10 實機就是這樣每 8 秒卡一下）。
        ⚠ 快捷欄整個讀不到（改版位移）就什麼都不做 —— 讓 buff.py 的舊按鍵
          保底法接手（安全退化）。
        """
        now = time.monotonic()
        if now < self._buff_read_t:
            return
        self._buff_read_t = now + 2.0        # 純讀很便宜，但也不必每一拍讀
        try:
            cells = quickbar.read_page(self.sc, self._qb_ui.page())
        except Exception:                    # noqa: BLE001
            return
        if cells is None:
            return                           # 讀不到 → 走舊的按鍵保底法
        slot = BUFF_KEY - quickbar.VK_F1
        c = cells[slot] if 0 <= slot < len(cells) else None
        before = self._buff.skill
        if c is not None and c.is_skill and self._buff.adopt(c.value):
            # ⚠ 只有**真的換了**才存設定＋報告：這支現在每 2 秒跑一次，
            #   每次都寫檔／蓋狀態列會把別的訊息洗掉（tooltip/狀態列規則）。
            if self._buff.skill != before:
                self._save_settings()
                self.status.setText(
                    f"✨ 自動分身：F12 = {skills.name_of(c.value) or c.value}"
                    f"（持續 {self._buff.secs / 60:.0f} 分）")
            return
        if c is None:
            why = "⚠ 自動分身：F12 上沒有技能 → 先不補（放上去就會自動接手）"
        elif c.is_item:
            why = "⚠ 自動分身：F12 放的是物品 → 先不補"
        else:
            why = (f"⚠ 自動分身：F12 的技能 {c.value} 沒有持續時間"
                   "（不是 buff）→ 先不補")
        self._buff.block(why)

    def _adopt_summon_skill(self) -> None:
        """自動召喚的技能編號：直讀快捷欄**目前頁**的 F11 那一格。

        跟 `_adopt_buff_skill` 同一套（零副作用、有 2 秒節流），差別是
        召喚技能不在 buff 主表（沒有持續時間），所以**任何技能格都收**；
        F11 是空的／放物品才 block() 停手。
        """
        now = time.monotonic()
        if now < self._summon_read_t:
            return
        self._summon_read_t = now + 2.0
        try:
            page = self._qb_ui.page()
            cells = quickbar.read_page(self.sc, page)
        except Exception:                    # noqa: BLE001
            return
        if cells is None:
            return                           # 讀不到（改版位移）→ 下次再試
        c = (cells[summon.SLOT]
             if 0 <= summon.SLOT < len(cells) else None)
        prev = self._summon.skill
        if c is not None and c.is_skill and self._summon.adopt(c.value, page):
            # ⚠ 只在技能真的變了才講話 —— 這支每 2 秒跑一次，每次都 setText
            #   會把別人的狀態訊息一直蓋掉（跟 buff 的倒數同一個教訓）。
            # ⚠ 不用 emoji 當字首（🐾 這類中文字型沒有字形，會變豆腐方塊，
            #   見 memory qt-ui-pitfalls）。
            if self._summon.skill != prev:
                self.status.setText(
                    f"自動召喚：F11 = {skills.name_of(c.value) or c.value}")
            return
        if c is None:
            why = "⚠ 自動召喚：F11 上沒有技能 → 先不召（放上去就會自動接手）"
        else:
            why = "⚠ 自動召喚：F11 放的是物品 → 先不召"
        self._summon.block(why)

    def _companion_tick(self) -> None:
        """自動分身＋自動召喚各走一步。

        ★★ **獨立於「開始掛機」**（使用者 2026-08-13 改的規則，推翻先前的
          「單獨勾沒有用」）：手動打王時只勾這兩個，也要自己補分身／補召喚。
          tick() 有兩個呼叫點 —— 沒掛機時直接呼叫；掛機中放在補給之後、
          打怪之前（交棒給精靈的那段不會走到這裡，不必讓路）。
        ⚠ 兩個各自控節奏（buff 看剩餘時間、summon 0.5 秒驗一次死活），
          心跳每拍呼叫沒有成本。
        """
        # 兩個功能都要靠跳板送封包 —— 沒掛機時沒人裝跳板，這裡自己裝
        #   （_ensure_mover 有失敗記憶，不會每拍狂試）。
        if (((self.buff_cb.isChecked() and self._buff.skill)
             or (self.summon_cb.isChecked() and self._summon.skill))
                and not (self._mover is not None and self._mover.active)):
            self._ensure_mover()

        # ── 自動分身：時間快到了就補一次 F12（見 app/game/buff.py）──
        if self.buff_cb.isChecked():
            if not self._buff.armed:
                self._buff.arm()          # 中途才勾的話，下一拍就無腦放一次
            # ★★★ 技能編號**直接讀快捷欄**（零副作用），不要再按 F12 學。
            #   2026-08-10 白狐實機：F12 是空的 → 舊的按鍵學習法永遠學不到，
            #   每 8 秒按一次、每次在 GUI 執行緒 sleep 40ms（五台分頁共用
            #   一條 GUI 執行緒），狀態列也被錯誤訊息一直蓋掉。
            # ⚠⚠ **無條件呼叫**（自帶 2 秒節流）：以前只在「還沒學過」時讀，
            #   設定檔的舊編號會永遠蓋住現況（2026-08-20 實機根因，見那支的說明）。
            self._adopt_buff_skill()
            # ★ 補放要確認伺服器受理 → 需要施放廣播監聽（學到技能才裝；
            #   _sync_castwatch 已裝／失敗過會直接返回，每拍呼叫沒成本）。
            self._sync_castwatch()
            # ⚠ `_my_id` 傳的是**函式本身**不是值：它要讀一次記憶體，而只有
            #   真的要補分身（20 分鐘一次）才用得到，心跳每 10ms 一拍先算好
            #   等於每秒白讀 100 次。
            note = self._buff.step(
                self.sc, self._mover, self.hwnd, self._keys.pf,
                self._my_id, self.stats, win.send_key,
                cast_hook=self._castwatch)
            if note and self._buff_note != note:
                self._buff_note = note
                self.status.setText(f"✨ {note}")
                if self._buff.skill:      # 學到了就存起來，之後不用再按 F12
                    self._save_settings()
        elif self._buff.armed:
            self._buff.reset()            # 勾拿掉＝停手（重勾會重新無腦放一次）
            self._buff_note = ""
            self._sync_castwatch()        # 分身不用了 → 首發也不要就卸監聽

        # ── 自動召喚：F11 的召喚物不見了／死了就重放（app/game/summon.py）──
        if self.summon_cb.isChecked():
            if not self._summon.armed:
                self._summon.arm()        # 中途才勾：下一拍就放一次
            # 技能編號直讀快捷欄 F11（零副作用；換技能幾秒內自動跟上）
            self._adopt_summon_skill()
            note = self._summon.step(
                self.sc,
                self._mover if (self._mover is not None
                                and self._mover.active) else None,
                self.player, self.pets)
            if note and self._summon_note != note:
                self._summon_note = note
                self.status.setText(f"自動召喚：{note}")
        elif self._summon.armed:
            self._summon.reset()
            self._summon_note = ""

    def _bump_kills(self) -> None:
        self._kills += 1
        self.kills_lbl.setText(f"已擊殺 {self._kills} 隻")

    def _reset_kills(self) -> None:
        """歸零鈕。擊殺數不存設定：開程式從 0 起算，重開始也不歸零。"""
        self._kills = 0
        self.kills_lbl.setText("已擊殺 0 隻")

    def _on_died(self, eid: int, confirmed: bool = True) -> None:
        """攻擊執行緒回報目標沒了 —— 立刻從既有清單接下一隻。

        confirmed=False 代表只是「一直沒給血量」的推測（那隻可能還活著），
        所以只短暫冷卻，不要像確定打死那樣封鎖一分鐘。

        不重掃記憶體：重掃要 0.5 秒還要排隊，每殺一隻就等一次會非常卡。
        清單裡真的沒得打了，才由 tick() 去排重掃。
        """
        m = self._cur
        # ⚠ 競態防護：died 是 queued signal，攻擊執行緒回報「上一隻」死掉的
        #   訊號可能在 GUI 已換好新目標**之後**才送到（GUI 放棄 A → 選好 B，
        #   worker 正在跑 A 的最後半步）。遲到的回報只記屍體冷卻就好 ——
        #   不能把剛選好的新目標打掉，也不能灌進擊殺數。
        if m is not None and eid != m.eid:
            self._killed[eid] = time.monotonic() + (
                KILL_MEMORY if confirmed else NOHP_MEMORY)
            return
        if confirmed:
            self._bump_kills()
        # 免得又挑到同一具還沒回收的屍體（存到期時間，見 _pick_next）
        self._killed[eid] = time.monotonic() + (
            KILL_MEMORY if confirmed else NOHP_MEMORY)
        self._dbg(f"「{m.name if m else '?'}」eid={eid:#x} "
                  + ("確認死亡" if confirmed else "一直沒給血量（推測屍體）")
                  + f" → 冷卻 {KILL_MEMORY if confirmed else NOHP_MEMORY:.0f} 秒")
        self._cur = None
        self._keys.eid = None                  # 別再對著屍體送封包
        if not self.run_cb.isChecked():
            self._keys.set_on(False)
            return
        if not self._pick_next():
            self._keys.set_on(False)              # 沒目標就別空按
            self._since_scan = SCAN_NOW           # 清單空了，才排重掃
            self.status.setText(
                f"「{m.name if m else ''}」倒了（累計 {self._kills} 隻）→ 重新掃描…")

    def _on_toggle(self, on: bool) -> None:
        if on:
            # ★ 跟「自動練技」互斥（都要指揮同一隻角色）：掛機接手，練技停
            #   （setChecked 會走 _on_train_toggle 的收尾把精靈主開關關掉）。
            if self.train_cb.isChecked():
                self.train_cb.setChecked(False)
            # ★ 開自動戰鬥時把分身／召喚叫起來。**已經 armed 就不重來** ——
            #   2026-08-13 起兩個功能獨立於掛機（_companion_tick），使用者
            #   可能已經在「沒掛機」狀態下跑著它們：分身的計時是查表來的，
            #   重 arm 等於白放一次；召喚重 arm 會把活得好好的那隻換掉。
            if not self._buff.armed:
                self._buff.arm()          # 沒在跑 → 開掛機先無腦放一次（原規則）
            if not self._summon.armed:
                self._summon.arm()
        if not on:
            self._keys.set_on(False)
            self._keys.stop_learning()
            self._keys.eid = None
            self._atk.hold_off()
            # 施放廣播監聽：首發不再需要，但自動分身可能還要 → 照需求同步
            self._sync_castwatch()
            self._cur = None
            self._death = False        # 死亡回程等到一半就作廢，別再傳送
            # ⚠ 停掛機時如果正在跑回程補給：作廢它（_supply_gen++ 讓背景執行緒回來的
            #   結果自己失效、_supply=False 讓掛機不再讓開）。
            #   ⚠⚠ 補給是背景執行緒跑我們自己的整趟，**沒法中途硬殺** —— 那一趟會自己
            #   跑完（run_full_supply 各段都有逾時），跑完角色就停著。停掛機當下不會再
            #   接手打怪。這是「背景阻塞式補給」的取捨（要能中途停得再改 run_full_supply 支援）。
            # ⛔ 不再 reset 分身／召喚（2026-08-13，獨立於掛機）。
            if self._supply:
                self._supply = False
                self._supply_gen += 1        # 作廢還在跑的補給執行緒結果
                self._supply_result = None
            self._robot_ours = False
            # 若是被 _stop_with() 停的（例如角色死亡），它會在這之後蓋上原因
            self.status.setText(f"已停止（累計擊殺 {self._kills} 隻）")
            return
        # ★ 不再擋「還沒掃描」或「還沒選怪」：掃描本來就一直在背景跑，
        #   選中怪物也可以邊掛邊加。沒選到怪就只是不打而已 ——
        #   不需要用彈窗擋住使用者。
        want = self.wanted()
        # ⛔ 這裡以前會把 _kills 歸零 —— 拿掉了（使用者要求）：擊殺數顯示在
        #   主開關旁邊，只有旁邊的「歸零」鈕和重開程式會歸零。
        self._killed.clear()
        self._since_scan = SCAN_NOW        # 清單裡挑不到的話，立刻重掃
        self._cur = None
        self._pick_home()                  # 記錄點：巡邏點優先（見 _pick_home）
        # ★ 每次開始都重學一次技能 ID —— 使用者隨時可能換掉那個鍵上的技能。
        #   學法：直讀快捷欄那格（quickbar.py），通常當場拿到；讀不到才退回
        #   「清零→按鍵→讀殘留」。學到之前用按鍵攻擊（本來就有效），不會空等。
        self._keys.stats = self.stats          # 清零要用，先確保是最新的
        self._keys.begin_learning()
        self._ensure_mover()               # 選怪／移動都要用它的跳板
        self._keys.mover = self._mover if (
            self._mover is not None and self._mover.active) else None
        # ★★ 首次攻擊要 100% 確認「有沒有放出去」→ 掛施放廣播監聽（castwatch）。
        #   照需求裝（首發／自動分身其一要就裝）；重開掛機重置失敗記憶再試一次。
        self._cw_failed = False
        self._sync_castwatch()
        # ★ 開始自動戰鬥只把精靈「該關的」關掉：勾趴趴GO回地圖→關精靈的標記捲軸、
        #   勾死亡回練功區→關精靈的陣亡自動復活（都由我們自己做，精靈搶先只會壞事）。
        # ⚠ 2026-08-14 改：**不再開精靈主開關/自動戰鬥、也不推補給頁設定**——
        #   回程補給改跑我們自己的 supply.run_full_supply，精靈全程讓開（使用者要求）。
        notes = []
        if self._mover is not None and self._mover.active:
            notes = robot.apply_prefs(
                self._mover, self.sc,
                jump_back=self.sup_jump_cb.isChecked(),
                revive_mark=self.sup_revive_cb.isChecked())
            # ★ 回程補給改跑我們自己的 → 把天使精靈自己的「回城補給」觸發全關掉
            #   （使用者：「開始掛機把補給流程也關掉」），免得精靈也自己回城跑一趟撞我們。
            notes += robot.disable_return_supply(self._mover, self.sc)
            # ★ 保證購買清單裡有天使之翼×50（使用者要求保留）。藥水補給現在
            #   全自動（2026-08-19，沒有勾選可看）——一律確保：每趟補給都要用
            #   翼回城，清單有它 run_full_supply 的買水步驟才會補貨，免得翼
            #   用完下一趟回不去。⚠ 只加翼、不開精靈的補給旗標。
            note = robot.ensure_buy_item(
                self._mover, self.sc, recall.RECALL_ITEM,
                robot.BUY_KEEP_WINGS)
            if note:
                notes.append(note)
        # 技能鍵的體檢結果也說出來 —— 勾的鍵上沒技能時會完全不出手，
        # 不講的話使用者只會看到「走過去不打」。
        skill_note = ""
        if not self._keys.skills:
            skill_note = ("　⚠ 快捷欄讀不到，技能鍵改用純按鍵"
                          if not self._keys.qb_ok else
                          "　⚠ 勾的技能鍵上沒有技能（空格／物品會略過）")
        else:
            skill_note = self._keys.skip_note()   # 只是提醒，不會過濾
        # 首發鍵設了、但那個鍵上沒技能 → 閘門會自動失效，講一聲免得他以為有在等。
        if self._keys.opener_vk and not self._keys.skills.get(
                self._keys.opener_vk):
            skill_note += (f"　⚠ 首次攻擊的 {self._opener_label()} 上沒有技能"
                           "（空格／物品）→ 這次不生效")
        self.status.setText(
            ("掛機中：只打「" + "、".join(want) + "」" if want
             else "掛機中：還沒選任何怪物 —— 點右邊的名字加進「選中怪物」")
            + ("　精靈：" + "、".join(notes) if notes else "")
            + skill_note)

    def halt(self, reason: str, gone: bool = False) -> None:
        """把**這一台**停乾淨並在狀態列說明原因；之後 tick 不再做任何事。

        兩個呼叫端：
          · 遊戲視窗被關掉（`_check_game_gone`）
          · 心跳丟出未預期的例外（`FarmTab._page_failed`）

        ★ 這是「大聲停用」不是「安靜地少做事」：勾勾放掉、狀態列寫明原因，
          例外那條路還會把完整 traceback 寫進 crash.log。
        ⚠ 順序：先讓兩條背景執行緒停手，再放勾勾（`_on_toggle` 的停止流程
          會去讀記憶體／叫精靈），最後才蓋上我們的訊息 —— 反過來的話
          狀態列會被 `_on_toggle` 的「已停止」蓋掉。
        """
        if self._halted:
            return
        self._halted = reason
        self._keys.set_on(False)
        self._keys.stop_learning()
        self._keys.eid = None
        self._atk.hold_off()
        # ⚠ 遊戲沒了就別再對它的 IAT 動手（castwatch.release 會寫回原位元組）：
        #   gone=True 時行程已不在，寫入會炸 —— 直接丟掉引用即可。
        if not gone:
            self._release_castwatch()
        else:
            self._castwatch = None
            self._keys.castwatch = None
        self._cur = None
        self._death = False
        # 位址全部作廢：行程沒了的話它們早就沒有意義，
        # 心跳出事的話也不該讓別條執行緒繼續拿著用。
        self.state = self.player = self.stats = self.inv = None
        self._keys.stats = None
        self._keys.pf = None
        # ⚠ 收尾本身也可能炸：`_on_toggle` 的停止流程會叫精靈收工，那是往
        #   遊戲寫記憶體（scanner.write_value 寫不進去會丟 OSError）——
        #   行程已經不在時正好會踩到。停用流程**不准再炸**，
        #   不然狀態列的原因就永遠貼不上去，使用者只會看到「已停止」。
        try:
            if self.run_cb.isChecked():
                self.run_cb.setChecked(False)  # 走既有的停止流程
            if self.train_cb.isChecked():
                # _on_train_toggle 看到 _halted 只收旗標、不再寫記憶體
                self.train_cb.setChecked(False)
            if gone and self._mover is not None:
                # 行程都沒了，IAT 還不還得回去無所謂 —— 重點是別再有人拿著它。
                # （move.release → stop() 內部本來就整段包在 try 裡。）
                move.release(self.pid, self)
                self._mover = None
        except Exception:                      # noqa: BLE001
            pass
        self.status.setText(reason)
        self.status.setStyleSheet("color: #e06060;")

    def _check_game_gone(self, dt: float) -> bool:
        """遊戲視窗還在不在？已經關掉就停用這一台並回 True。

        ⚠⚠ 為什麼一定要主動查：遊戲被關掉之後，我們手上的控制代碼**還是
          有效的**，記憶體讀取只會安靜地回 None —— 跟「這一拍沒掃到怪」
          長得一模一樣。於是掛機會對著一個不存在的行程一直跑，直到撞上
          某個沒有防護的讀取（跳板那塊配置的記憶體）丟出
          ERROR_PARTIAL_COPY(299) 把整個工具箱掀掉 —— 使用者實際遇到過。
        ★ `alive()` 只有在**明確問到行程已結束**時才回 False（見它的說明），
          所以不會把好端端的分身誤判成關掉了。
        """
        if self._halted:
            return True
        self._gone_t += dt
        if self._gone_t < GAME_GONE_POLL:
            return False
        self._gone_t = 0.0
        if self.sc.alive():
            return False
        self.halt("⚠ 這個遊戲視窗已經關閉 → 這台分身已停止"
                  "（重開遊戲登入後會自動接上）", gone=True)
        return True

    def tick(self, dt: float) -> None:
        """UI 側的心跳：只做「挑目標、卡住偵測、更新狀態列」。

        寫目標與送鍵各自在 TargetWorker / KeyWorker 的執行緒上跑，節奏不受 UI
        影響 —— 原本整個迴圈掛在這裡，UI 一忙節奏就漂掉，感受就是「很卡」。
        """
        # ★★ 遊戲被關掉／自己當掉 → 這台整個停手，什麼都不要再做。
        #   一定要放在最前面：底下每一段都在讀寫一個已經不存在的行程。
        if self._check_game_gone(dt):
            return
        # ★ 掃描**一直都在跑**，不管有沒有在掛機 ——
        #   這樣「周圍怪物」永遠是即時的，使用者隨時可以把名字加進來，
        #   也不必先按什麼按鈕才能開始（掃描只掃熱區，很便宜）。
        # ★★★ 掃描看門狗（見 SCAN_STUCK_SECS）：請求送出去之後結果一直沒回來，
        #   就當那一次掉了 —— 把閂放掉、要求一次全掃、重新開始。
        #   沒有這一段的話，只要漏放一次閂，那台分身的怪物清單就永遠定格。
        if self._waiting:
            self._wait_t += dt
            if self._wait_t >= SCAN_STUCK_SECS:
                self._wait_t = 0.0
                self._waiting = False
                self._scan_lost += 1
                self._ask_full("掃描結果沒回來")
                self.status.setText(
                    f"⚠ 掃描結果 {SCAN_STUCK_SECS:.0f} 秒沒回來 → 重新要求"
                    f"（累計 {self._scan_lost} 次）")
        else:
            self._wait_t = 0.0

        self._since_scan += dt
        # ★★ 2026-08-11 起只剩一檔自動的（使用者要求「掃周圍怪物改成按鈕」）：
        #       掛機中     → REFRESH_GAP 自動刷新
        #       沒在掛機   → **完全不自動掃**，只有明確要求時才掃
        #                    （「掃描周圍怪物」鈕、切到這台、換地圖／重連、
        #                      清單裡挑不到目標…那些地方都是把 `_since_scan`
        #                      推到 SCAN_NOW）
        # ⚠⚠ 掛機中**不能**改成手動：那份掃描是拿來接下一隻怪、跟人搶怪的，
        #   停掉等於打完一隻就發呆（memory 的 farm-scan-refresh-tiers）。
        # ⚠ 沒在掛機而且沒人要求時要把累加值**夾住** —— 不夾的話開著十幾分鐘
        #   自己就會越過 SCAN_NOW 的門檻，又變回會自動掃。
        asked = self._since_scan >= SCAN_NOW
        if not self.run_cb.isChecked():
            if (self.buff_cb.isChecked() or self.summon_cb.isChecked()
                    or self.train_cb.isChecked()):
                # ★ 自動分身／自動召喚**不開掛機也要動**（使用者 2026-08-13
                #   改的規則：手動打王時要它們自己補）。它們靠掃描拿玩家物件
                #   ／屬性／kind=4 清單，所以這時恢復慢檔自動掃。
                #   不牴觸「掃周圍怪物改成按鈕」那條要求 —— 純看清單、兩個都
                #   沒勾時照樣完全不自動掃。
                # ★ 自動練技也一樣：藥水見底判斷要物品陣列表頭（InvWorker
                #   跟著掃描走）、記練技原地要玩家座標 —— 都靠這檔慢掃。
                gap = IDLE_SCAN_GAP
            else:
                if not asked:
                    self._since_scan = min(self._since_scan, IDLE_SCAN_GAP)
                gap = SCAN_NOW
        else:
            gap = REFRESH_GAP
        if self._since_scan >= gap and not self._waiting:
            self._since_scan = 0.0
            self._waiting = True
            self._wait_t = 0.0
            # ★ 掛機中卻沒目標 → 定期（FULL_HUNT_GAP）要求全掃當保險。
            #   唯讀實測熱掃幾乎都能當拍看到新怪（v3：新出現 0 件落在熱區
            #   外），這只是「等重生」期間順便讓熱區清單保持新鮮。
            if self.run_cb.isChecked() and self._cur is None:
                self._ask_full("沒目標，全掃當保險")
            full, self._want_full = self._want_full, False
            # ⚠ 請求沒被接下（工作執行緒還沒建好／分頁重載中）就**當場把閂放掉**
            #   —— 不然沒有人會來放，掃描就此停擺（看門狗雖然也擋得住，
            #   但那要白等 5 秒）。
            if self._on_scan(self.pid, full) is False:
                self._waiting = False

        # 趴趴GO 倒數（顯示在「回程補給」那列最右邊）。
        # ⚠ 要放在所有 return 之前 —— 補給／死亡回程那兩段都會直接 return，
        #   放後面就永遠不會更新。
        self._update_jump_countdown()

        if not self.run_cb.isChecked():
            # ★ 自動分身／自動召喚獨立於掛機（使用者 2026-08-13：手動打王
            #   不開掛機也要能補）。補給／死亡回程／巡迴換頻是掛機才有的
            #   狀態，這條路不會經過，所以不必讓路。
            self._companion_tick()
            # ★ 自動練技也獨立於掛機（互斥）：開關看門狗＋藥水見底＋補給趟
            #   全在它自己的心跳裡（見 _train_tick）。
            self._train_tick(dt)
            # ★ 自動換球同樣獨立於掛機：技能球在**練技**時也一樣會滿，
            #   手動打王時也會 —— 觸發條件是「球滿了」，不是「在掛機」。
            self._ball_tick(dt)
            return

        # ★ 死亡回程要在最前面：死亡／復活／傳送期間 state 常常是 None、
        #   也絕不能讓巡迴換頻道插進來，所以整個 tick 都讓給它。
        if self._death_tick(dt):
            return

        # ★ 巡迴換頻道要在「state is None」的檢查**之前** —— 換頻道會斷線重連，
        #   那幾秒 state 本來就是 None，放後面的話狀態機會停在半路。
        if self._tick_rotation(dt):
            return

        # ★ 角色死了就自動停：不然會對著空氣一直送技能鍵。
        # HP 走 app/game/player.py（跨 5 台驗證過的定位）。
        # ⚠ 它的 read() 刻意不做數值檢查，HP 歸零照樣讀得到 —— 早期版本把
        #   「HP > 0」寫進合理性檢查，結果角色一死就回 None，死亡永遠測不到。
        #
        # ⚠⚠ **必須在 `state is None` 的提早 return 之前**：死亡偵測用的是
        #   stats（角色屬性物件），跟狀態物件是兩回事。以前放在後面，掃描
        #   抓不到狀態物件的那段期間**連死了都不知道**（不停機、不通知）。
        self._hp_t += dt
        if self._hp_t >= HP_CHECK_GAP:
            self._hp_t = 0.0
            # ★★★ 位址剛失效（換頻道／換地圖／重連／重生）就**當場自己補回來**，
            #   不要空等下一次全掃。角色屬性物件掛在狀態物件底下的固定位置，
            #   `locate_fast()` 只要兩次讀取（實測 0.024~0.116ms），而全掃是
            #   195~231ms —— 差了三個數量級。
            #   ⚠⚠ 實測（2026-08-09，五台 10Hz 全程採樣換頻道）：靠全掃補回來
            #     要 **1.8~10.7 秒**，那段期間血魔百分比停在舊值、**連死了都
            #     偵測不到**（死亡偵測讀的就是這個物件）。
            #   ⚠ 這裡只准用 `locate_fast()`，**不可以叫 `locate()`** ——
            #     那支對不上時會全掃，畫面執行緒會卡 0.2 秒。
            #     捷徑對不上就維持 None，照舊等 ScanWorker 那條全掃的路。
            if not self.stats:
                self.stats = player.locate_fast(self.sc)
                if self.stats:
                    self._keys.stats = self.stats   # 送鍵執行緒學技能 ID 也要
            if self.stats:
                st = player.read(self.sc, self.stats)
                if st is not None:
                    # ⛔ 血魔百分比（_hp_pct/_mp_pct 與 MaxTracker）跟著坐下休息
                    #   一起移除 —— 只有休息門檻在用它。
                    # ★ HP 比上一拍低 → 有怪在打我（`_under_attack()` 的來源）。
                    #   挑目標時「冷卻中的怪要不要解禁」靠這個硬保險，
                    #   交戰欄位會失靈（見 [[foe-field-unreliable]]）。
                    #   ⚠ 這裡讀的是**原始的** st.hp，不受「最大 HP 暫時等於
                    #     現值」那個暫態影響（那個暫態只會弄壞百分比）。
                    if 0 < st.hp < self._hp_prev:
                        self._hp_drop_t = time.monotonic()
                    self._hp_prev = st.hp
                    if st.hp <= 0:
                        # ★ 勾了「死亡自己回練功區」→ 不停機，交給死亡回程
                        #   模式（3 秒後送「回標記點」封包 → 活了接著打）。
                        #   沒勾就維持原樣：自動停止掛機。
                        # ★ 兩種都**不通知**（使用者 2026-08-07 要求：這頁
                        #   只通知藥水用完和裝備壞掉）。只有死亡回程卡住
                        #   （_death_fail）那種「掛機停了」才通知。
                        if self.sup_revive_cb.isChecked():
                            self._start_death_return()
                        else:
                            self._stop_with(
                                f"☠ 角色死亡 → 已自動停止掛機"
                                f"（累計擊殺 {self._kills} 隻）")
                        return
                else:
                    self.stats = None       # 物件搬家了，等下次掃描重新定位

        # ★ 補給中就整個讓開 —— 那段時間是天使精靈在開車，我們不能同時下指令。
        #
        # ⚠⚠⚠ **一定要在 `state is None` 的提早 return 之前**（跟 `_death_tick`
        #   同一個理由）。補給整趟都在換地圖（回程道具 → 城裡、趴趴GO → 練功區），
        #   而換地圖時狀態物件會搬家，`self.state` 有好幾秒是 None ——
        #   放在後面的話那段期間 `_supply_tick` 根本不會跑：
        #     · 「回到原地圖了沒」不檢查 → 回來了也不知道
        #     · `_supply_t` 不累加 → **連 10 分鐘逾時的保險都不會響**
        #   結果就是畫面永遠停在「趴趴GO 傳送中…」而且沒有任何人管
        #   （使用者 2026-08-09 回報）。補給期間我們完全不碰狀態物件
        #   （不寫目標、不送鍵），本來就不需要它。
        if self._supply_tick(dt):
            return

        # ★★ 看住精靈的「自動攻擊」。**刻意放在 `state is None` 之前**：
        #   它走的是精靈變數樹，跟狀態物件無關，而最需要它的時刻正是
        #   「剛換完地圖／剛換完頻道」—— 那時 self.state 有好幾秒是 None，
        #   放在後面就等於在最危險的那幾秒不看管（見 _af_tick）。
        self._af_tick(dt)

        if self.state is None:
            return

        # ★ 自動分身＋自動召喚（見 _companion_tick）。
        # ⚠ 位置刻意放在**補給之後、打怪之前**：
        #   交棒給精靈時不要插隊按鍵，但打怪中該補還是要補
        #   —— buff／召喚斷掉的損失比少打一下大。
        self._companion_tick()
        # ★ 自動換球（見 _ball_tick）。放這裡的理由跟上面同一套：不搶補給的路，
        #   但打怪中球滿了就要換 —— 換球只是一包，不影響出手節奏。
        self._ball_tick(dt)

        # ★ 該不該回去補給。壞裝照勾選；藥水**全自動**（2026-08-19 使用者要求，
        #   沒有勾選）：見底（≤robot.POTION_LOW）→ 店裡買得到就跑回程補給、
        #   買不到就通知＋翼回城＋停機。幾秒看一次就夠。
        # ★ 裝備壞掉看**全身穿著的**（0~11 格，不含背包）：任何一件耐久 ≤ 1
        #   就算（使用者 2026-08-07 要求，以前只看武器）。走 bag.py 那條
        #   遊戲自己的容器路徑，**不需要 self.inv**（那是藥水/回程在用的）。
        self._gear_t += dt
        if self._gear_t >= GEAR_CHECK_GAP:
            self._gear_t = 0.0
            # None = 讀不到容器（換地圖中…）→ 不觸發也不解除，下次再看
            broken = bag.worn_broken(self.sc)
            gear = self.sup_gear_cb.isChecked()
            # ★ 藥水見底清單（不通知——買得到會自動補給，通知只在
            #   _dry_stop 那種「店裡沒賣、要停機」才發；2026-08-19 使用者定）。
            dry = self._check_dry()
            # ★ 見底那幾組裡有「補給店沒賣」的（精靈頁放活動藥水這類非賣品）
            #   → 買不到，跑補給也是白燒一張翼：通知＋翼回城＋直接停機。
            #   設定暫時讀不到（plan=None）或那組讀成空清單就先照常跑補給
            #   （買水那步會再對一次帳；一直買不進來有 _dry_trips 煞車兜底）。
            if dry and self._ensure_mover():
                plan = robot.potion_buy_ids(self._mover, self.sc, self.pid)
                bad = [d for w, d in dry
                       if plan is not None and plan.get(w)
                       and not any(supply.shop_sells(t)
                                   for t in plan.get(w, ()))]
                if bad:
                    self._dry_stop(bad)
                    return
            if (gear or dry) and self._ensure_mover():
                # ★ 壞裝與見底清單都是這一拍剛算好的，直接傳進去別再算一次
                #   （各要走一趟容器，上百次記憶體讀取）。
                why = robot.supply_needed(self._mover, self.sc, self.inv,
                                          gear, True, True, self.pid,
                                          broken=broken, dry=dry)
                if why and self._start_supply(why):
                    return
            if broken:
                names = "、".join(it.name for it in broken)
                self._stop_with(f"🔧 裝備已損壞（{names}）→ 已自動停止掛機"
                                f"（累計擊殺 {self._kills} 隻）")
                self.notify(f"裝備已損壞（{names}），掛機已自動停止。")
                return

        self._walk_t += dt
        me = self.my_pos()

        # 目前在哪張地圖 —— 巡邏點要靠它決定「這個點是不是這張圖的」。
        # 換圖很少發生，半秒讀一次就夠（心跳是 10ms 一拍）。
        self._scene_t += dt
        if self._scene_t >= SCENE_SAMPLE:
            self._scene_t = 0.0
            was = self._scene
            self._scene = self._read_scene()
            # ⚠ 換地圖時導航一定要重來：算好的路線是**這張圖的座標**，
            #   在別張圖上完全沒有意義（會照著它往一個不相干的方向走）。
            if was != self._scene:
                self._nav.reset()

        # ★★ 「角色正在走路嗎」——**直接讀遊戲的動畫狀態**（'Run' / 'Wait'），
        #   不要再隔 0.3 秒比一次位置。移動中一律不重下移動指令（會把多點路徑
        #   砍掉、原地來回），所以這個判斷慢多少，每個路段就多空等多少。
        #   實測同一條來回路線：比位置 28% 的時間站著不動，讀狀態只剩 10%。
        #   （見 entity.is_walking 的實測數字。）
        # ⚠ 讀不到玩家物件時保留上一次的判斷，不要當成「停著」——
        #   那會在掃描空窗期狂送移動指令。
        if self.player:
            self._moving = entity.is_walking(self.sc, self.player)

        if self._cur is None:
            # ★ 追怪不限距離；「周圍完全沒有選中的怪」時才去巡邏點找（使用者要求）
            # ★ 只走「現在這張圖」的巡邏點 —— 座標在每張地圖都從 0 開始算，
            #   拿別張圖的點來走會往一個完全不相干的地方衝（使用者要求擋掉）。
            #   分流不算不同圖（天使學園 41/141/241 是同一張），見 scene.map_key。
            # ★ 有設巡邏點就巡、沒設就不巡（2026-08-13 起沒有開關）
            if not (self._spots and me):
                return
            # ⚠ 讀不到目前地圖時**寧可不動**：無法確認就走，等於有機會拿 A 圖的
            #   座標在 B 圖亂衝。停下來並把原因寫在狀態列，比默默走錯好。
            if self._scene is None and any(s[2] is not None for s in self._spots):
                self.status.setText(
                    "⛔ 讀不到目前在哪張地圖 → 不移動"
                    "（遊戲改版位移？巡邏點的地圖比對停用中）")
                return
            here = [n for n, s in enumerate(self._spots) if self._spot_here(s[2])]
            if not here:
                self.status.setText(
                    f"⛔ {scene.scene_name(self._scene)} 沒有巡邏點"
                    f"（已存的 {len(self._spots)} 點都在別張地圖）→ 不移動")
                return
            if self._spot_i not in here:
                # 上次要去的點不在這張圖（剛換圖）→ 從這張圖的第一個開始
                self._spot_i = here[0]
            sx, sy, _sid = self._spots[self._spot_i]
            d = math.hypot(me[0] - sx, me[1] - sy)
            if d <= SPOT_SLACK:
                # 到了這個點還是沒怪 → 換下一個點繼續找（只在這張圖的點裡輪）
                self._spot_i = here[(here.index(self._spot_i) + 1) % len(here)]
                self._nav.reset()
                self._walk_t = WALK_GAP        # 下一拍就往新的點走
                self.status.setText(
                    f"巡邏點 {self._spot_i + 1} 沒怪 → 前往下一個"
                    f"（{scene.scene_name(self._scene)} 共 {len(here)} 點）")
                return
            # ★★ 走巡邏點交給 app/game/navigate.py：它讀地形圖算最短路，
            #   一個轉折點一個轉折點走過去（純讀記憶體，不問遊戲的尋路）。
            # ⚠ 它自己會判斷角色在不在走路、要不要重下指令，所以這裡**不要**
            #   再加 _moving / _walk_t 的節流，會互相打架。
            if not self._ensure_mover():
                self.status.setText("⛔ 移動跳板沒裝上 → 不移動")
                return
            note = self._nav.step(self.sc, self._mover, self.player, sx, sy)
            if self._nav.stuck:
                # ★ 真的到不了就換下一個點（舊版沒有這道，會站到天亮）
                nxt = here[(here.index(self._spot_i) + 1) % len(here)]
                stuck_msg = (f"⛔ 走不到巡邏點 {self._spot_i + 1}"
                             f" ({sx:.0f},{sy:.0f})")
                self._nav.reset()
                if nxt == self._spot_i:
                    self._stop_with(stuck_msg + " → 這張圖只有這一個點，已停止掛機")
                    self.notify(stuck_msg + "，掛機已停止。")
                    return
                self._spot_i = nxt
                self.status.setText(stuck_msg + f" → 改去巡邏點 {nxt + 1}")
                return
            self.status.setText(
                f"周圍沒有選中的怪 → 前往巡邏點 {self._spot_i + 1}"
                f"（{scene.scene_name(self._scene)} 第 "
                f"{here.index(self._spot_i) + 1}/{len(here)} 點）"
                f" ({sx:.0f},{sy:.0f})　還有 {d:.0f} 格　{note}")
            return

        m = self._cur
        hp = self._atk.hp

        mp = entity.read_pos(self.sc, m.addr)
        dist = math.hypot(mp[0] - me[0], mp[1] - me[1]) if (mp and me) else None
        # 施放封包帶目標的格子座標 —— 順移那類對地技能沒有座標發不動。
        # ⚠ 攔包看到遊戲自己送的是 0（`0x664627(技能, 目標, 0, 0, 0)`），
        #   我照著改成 0 試過，**使用者實跑沒有變好，而且順移會廢掉**，
        #   所以改回來。帶座標實測不影響傷害（同一批怪交替測 3 對 3 都打得到）。
        if mp:
            self._keys.pos = (round(mp[0]), round(mp[1]))

        # 接近規則（改用封包攻擊後，接近**完全由我們自己走**，不靠按鍵）：
        #   ① 中間有障礙物（尋路點數 > 1）→ 走到怪臉上
        #      隔著牆時「直線距離近」是假的，站在原地打不到。
        #   ② 沒障礙物、超出攻擊範圍 → 走到範圍的九成
        #   ③ 進到範圍內才送三連包
        # ⚠ 讀不到座標（dist is None）算「不在範圍內」—— 位置不明就別亂送封包，
        #   那多半是怪的物件已經被回收了。
        #
        # ★★★ 「跟這隻怪之間有沒有地形」「走不走得到」**查我們自己讀的地形圖**
        #   （app/game/terrain.py），不再問遊戲的尋路。
        #
        #   為什麼換掉（不是因為遊戲算錯 —— 實測 80 個「怪站得住的格子」，
        #   遊戲的答案跟地形圖 100% 一致）：問題出在**問不到**與**貴**。
        #     · 尋路要跟攻擊搶那個唯一的指令槽，搶不到就回 -1，我們只能
        #       沿用上一次的答案 —— 而攻擊執行緒實測可以把槽佔到 82%。
        #       怪一直在走，0.2 秒前的答案本來就過期了。
        #     · 一次 5~6ms，五台一起跑就是持續佔著槽不放。
        #   地形圖：直線檢查 0.003ms、近距離 A* 0.2~0.8ms、**永遠問得到**，
        #   而且完全不佔指令槽（純記憶體查表）。
        #
        # ⚠⚠ **只有這裡可以寫 _path_pts**。以前 _walk_toward() 的回傳值也會寫進來，
        #   但那是「走到某個中繼點」的路徑點數，跟「我跟這隻怪之間有沒有地形」
        #   根本是兩回事 —— 只要走過一次要繞路的路徑（點數 > 1），就會被當成
        #   隔著地形，攻擊距離縮成 2 格，於是 3~10 格的怪既不打、也走不到，
        #   一路卡到 10 秒逾時（監控實際抓到的距離 3.6 / 5.4 / 9.6 / 10.2）。
        # 怪會走動，所以每 PATH_GAP 重算一次，不是只在「還不知道」時算。
        # ⛔ 「貼身（≤3 格）一律當直線可通」的捷徑 2026-08-19 拿掉了（使用者
        #   回報：怪在障礙物對面卻站在對面隔牆打）。薄牆／柵欄隔開 2~3 格的
        #   兩格是存在的，硬當可通＝walk_near 直線推牆＋站著空放到逾時換怪。
        #   貼身照樣走下面的地形圖判斷：真的可通（絕大多數貼身）行為跟以前
        #   一樣；真被薄牆隔開時 _way 會給出繞過去的路。
        #   當年設捷徑防的「殘留舊判斷把攻擊距離壓成 2 格」已經不存在 ——
        #   blocked 現在只決定走多近（keep），攻擊距離看各招自己的射程。
        #   成本也沒變：同樣是 _path_gap（0.2 秒）節奏才碰地形圖。
        self._path_t += dt
        if (self._path_t >= self._path_gap and mp is not None and me
                and dist is not None):
            self._path_t = 0.0
            plan_t0 = time.perf_counter()
            # ⚠ 地圖快取的身分檢查放在這裡、不要放在每一拍：它要讀列指標
            #   陣列（約 720 bytes）＋場景編號，一次約 0.07ms —— 心跳是
            #   10ms 一拍、五台分身，每拍都問等於每秒白花 35ms。
            grid = self._maps.get(self.sc)
            mtile = (int(me[0]), int(me[1]))
            ttile = (int(mp[0]), int(mp[1]))
            # ★★★ 沒有地形圖時**什麼都不猜**（2026-08-10 使用者指定）：
            #   不走直線、也**不再退回遊戲自己的尋路** —— 那條路只會沿著
            #   「往目標的直線」取中繼點，遇到擋在中間的地形就是一路推著牆走
            #   （使用者實拍：卡在牆邊直到周圍怪物重生）。
            #   地形圖實測 5 台 4 張圖 150/150 次全讀得到，讀不到就是換圖那
            #   一瞬間 —— 那一瞬間本來就不該走路。
            self._no_grid = "" if grid is not None else (
                self._maps.why or "讀不到地形圖")
            if grid is None:
                # ⚠ 這裡**不准動 _unreach**：那是「這隻怪走不到」的計數，
                #   我們自己讀不到圖不能算在怪頭上（會把牠冷凍起來）。
                self._path_pts, self._way = 0, []
                self._line_clear = False
            elif grid.clear_line(mtile, ttile):
                self._path_pts, self._way, self._unreach = 1, [], 0
                self._line_clear = True
            else:
                self._line_clear = False
                # 直線被擋 → 算一條繞過去的路。
                # ⛔ **上限拿掉了**（原本是直線距離的 3 倍）。那道上限是
                #   「卡在牆邊」的真正根因：繞路超過 3 倍就算不出來 → _way 空
                #   → 走路退回遊戲的尋路 → 直線推牆。而地形複雜的地方
                #   （沙漠的岩層、峽谷）繞 4~6 倍是常態。
                #   ★ 不必怕慢：`_candidates` 已經先用連通區泛洪把「真的走不到」
                #     的怪整批刷掉了，所以這裡的 A* 一定找得到路，成本跟路徑
                #     長度成正比（實測整張圖最壞 12~19ms，一般 0.2~0.8ms）。
                #     A* 最貴的情況是「走不到」要把整片展開完 —— 那個情況
                #     在這裡已經不存在。
                wp = grid.waypoints(mtile, ttile)
                if wp:
                    self._path_pts = max(2, len(wp))
                    self._way = [(x + 0.5, y + 0.5) for x, y in wp]
                    self._unreach = 0
                else:
                    self._path_pts, self._way = 0, []
                    # ★★ 「連續幾次算不出路徑」**只能在這裡數** —— 這裡才是
                    #   真的重算了一次。以前放在每一拍都跑的地方，而判定每
                    #   PATH_GAP(0.2s) 才更新，於是「連續 3 次」實際變成
                    #   「連續 3 個心跳」= 30 毫秒：只要失敗一次，30ms 後就
                    #   把那隻怪丟掉並加黑名單。症狀是一直換目標、跑來跑去
                    #   卻沒進帳（實測 3 分鐘裡 39% 的時間在空轉）。
                    self._unreach += 1
            # ★★ **重算節奏自我調節**（2026-08-10，跟著「拿掉繞路上限」一起加）。
            #   拿掉上限之後，極遠的目標 A* 真的會變貴：實測整張圖最遠的可走格
            #   （巨木梯道 337 格的路）要 48.6ms —— 每 0.2 秒重算一次、五台
            #   一起跑就是把 GUI 執行緒吃掉（畫面凍住，見 [[qt-ui-pitfalls]]）。
            #   規則：**這一次花了多久，就休息 PATH_BUDGET 倍**，也就是規劃
            #   路徑最多只准佔 1/PATH_BUDGET 的時間。近距離（0.1~0.8ms）算完
            #   仍然是 PATH_GAP 的節奏，完全沒變；只有超遠的目標會放慢到
            #   最多 PATH_GAP_MAX 重算一次 —— 而那種距離下，一秒前算的路
            #   跟現在算的幾乎一樣（我們也才走了幾格）。
            self._path_gap = min(max(PATH_GAP,
                                     (time.perf_counter() - plan_t0)
                                     * PATH_BUDGET),
                                 PATH_GAP_MAX)
        # ⛔ 這裡以前還有一段「地形圖讀不到 → 改問遊戲的尋路（mover.path_to）」。
        #    **整段刪掉了**（2026-08-10 使用者指定）。它是唯一還會把走路交回
        #    遊戲的地方，而遊戲那條在 walk_route 裡是「沿直線取中繼點、
        #    失敗就 ±40°/±70°」—— 凹地形一定推牆。現在沒有地形圖就不走路，
        #    狀態列會寫原因（self._no_grid）。
        # ⚠ blocked 只決定「要走多近」，**不能拿來擋攻擊**。
        #   之前寫成 `in_range = … and not blocked`，結果隔著地形的怪就算已經
        #   走到牠臉上（實測 1.1 格）也永遠不送封包 —— 角色走過去然後發呆，
        #   就是使用者回報的「走過去卻不打」「旁邊有怪也不打」。
        self._keys.mode = self.mode
        blocked = self._path_pts > 1
        # ★★★ 攻擊距離**跟著現在那招的射程走**（讀遊戲自己的技能表）。
        #   ⚠⚠ 這是「雪狐卡住」的真正根因：以前一律用 ATTACK_PACKET_RANGE
        #     (12 格) —— 那是在**法師**身上量的（電擊術射程 12）。近戰的
        #     破甲劈擊射程只有 1，站 3~11 格照樣被判 in_range，於是站著空打
        #     到 10 秒逾時，換一隻再空打（使用者看到的「卡住」）。
        #   實測（雪狐 82 級、關掉官方精靈、判準是怪自己的血量）：
        #       站 2.0 / 2.2 格 → 3 秒打死（100%）
        #       站 7.4 / 8.7 / 11.4 格 → 12~20 秒、101~168 次出手，**零傷害**
        #   換算：有效歐氏距離 ≈ 射程 + 1（斜角相鄰算一格）。
        #   查不到射程（改版新技能、快捷欄讀不到）就退回舊的 12 格。
        # ★★★ **攻擊距離不再有「一個數字」**（使用者 2026-08-10 指定）：
        #   打不打得到由**每一招各自**比自己的射程（KeyWorker.reach_of /
        #   in_range_of_any）。這裡只剩下一個「走多近」要決定 —— 走位本來就
        #   只能站在一個位置上，所以它照**最短**射程走進去（走到那裡，輪替裡
        #   每一招才都用得到）。⚠ 這是**走位**的數字，不是攻擊距離。
        rng = self._keys.min_range
        reach_walk = (ATTACK_PACKET_RANGE if rng is None
                      else min(ATTACK_PACKET_RANGE, float(rng) + 1.0))
        # ★★★ 「最後一段交給客戶端自己走」（使用者的點子，2026-08-06 實測驗證）
        #   短射程技能走的是「叫遊戲的快捷鍵」那條路，而**遊戲自己會走過去**：
        #   實測雪狐站 9.8 格、我們一步移動指令都沒下，只呼叫快捷鍵 ——
        #   角色自己走了 8.5 格、貼到 1.4 格、把怪打死（血 100→0）。
        #   所以近戰不必由我們貼到 1.4 格：走到 10 格就停手，剩下讓遊戲走。
        #   好處是不再跟客戶端搶走位（那正是「卡在 2.2 格」「卡進怪身體」的來源）。
        # ⚠ memory 有一條「補按技能鍵讓角色接近會打架」——那是**同時還在下
        #   我們自己的移動指令**時的結論。只讓客戶端走就很順，兩邊一起走才會卡。
        # ⚠ 交棒只適用於快捷鍵那條路：對地技能與 >8 格的技能走封包，
        #   封包**不會**讓客戶端走過去（那是快捷鍵函式自己做的事）。
        handoff = bool(self._keys.handoff and not blocked
                       and not self._handoff_fail)
        # ★★★ 攻擊距離**永遠照技能射程** ——「隔著地形」只決定走多近（keep），
        #   **不再**把射程壓成近戰 2 格。
        #   ⚠⚠ 舊寫法 `MELEE_RANGE if blocked` 是遠程「盯怪幾秒不出手」的
        #   根因（2026-08-07 黑狐實錄兩段）：沙漠這種小凸起多的地形尋路很常
        #   回多點，但「路徑要繞」跟「技能被擋線」是兩回事 —— 射程 12 的
        #   黑狐被判「打不到」，只走路不出手，追到 2 格又卡進 [2,3) 死區呆
        #   10 秒。現在：一邊走近一邊照打；真的被牆擋線（零傷害）先由
        #   PUSH_IN_SECS(3 秒) 貼身繞打自救，最後 STUCK_ENGAGED(15 秒)
        #   換怪收尾 —— 打不打得到只有目標的血知道（實錘）。
        #   ⚠ 這裡以前寫「交給 4 秒零傷害快篩」，那條 2026-08-10 刪了（見它的說明）。
        # 「打得到嗎」＝**有沒有任何一招打得到**（每一招各自比自己的射程）。
        # ⚠ 交棒那一輪例外：那時候本來就是「站得遠、叫快捷鍵讓遊戲自己走過去」，
        #   用射程去擋會把交棒整個廢掉（見 KeyWorker.client_walk）。
        in_range = (dist is not None and dist <= HANDOFF_RANGE if handoff
                    else self._keys.in_range_of_any(dist))
        # ⚠ 交棒的保險：交出去之後如果一直沒真的接戰（>3 秒還在技能射程外、
        #   而且沒掉過血），就收回來自己走 —— 免得客戶端因為地形之類走不到，
        #   我們卻站在 10 格外一直空按（那又變成使用者最討厭的發呆）。
        if handoff and dist is not None:
            if dist <= reach_walk or self._hurt:
                self._handoff_t = 0.0
            else:
                self._handoff_t += dt
                if self._handoff_t >= HANDOFF_WAIT:
                    self._handoff_fail = True    # 這一隻改回自己走
                    self._dbg(f"交棒逾時（{HANDOFF_WAIT:.0f} 秒還在 "
                              f"{dist:.1f} 格）→ 改由我們自己走過去")
        # ★★ 走到多近：**完全由射程決定**（介面上的「接戰距離」已移除）。
        #   隔著地形就貼臉；否則停在「打得到的距離再往內留餘裕」。
        # ⚠⚠ 餘裕要夠大（使用者指出的）：停在 reach−1（法師 11、射程 12）
        #   等於**站在射程邊緣**，怪往外走一步就出界 —— 那一瞬間我們還在送
        #   施放，就是「超出射程還在放技能」，而且會一直重走、很卡。
        #   遠程留 2 格（法師停 10；2026-08-07 試過留 3 停 9，同晚使用者又
        #   改回 10 —— 地形擋線的呆站由 STUCK_ENGAGED(15 秒) 接手，
        #   不必犧牲一格距離）、近戰留 0.6 格（射程 1 → reach 2.0 → 停 1.4，
        #   再多留就進不了近戰射程了）。下限 MIN_GAP：更近會卡進怪的身體。
        # ⚠⚠ 這是**走位**用的距離（走到那裡每一招都用得到），跟「打不打得到」
        #   完全分開 —— 後者是每一招各自判斷的，這裡不代表任何一招的射程。
        reach_keep = HANDOFF_RANGE if handoff else reach_walk
        margin = 2.0 if reach_keep >= 6.0 else 0.6
        # ⚠ `self._push_in` ＝ 站定打了 PUSH_IN_SECS 秒零傷害（技能被地形
        #   擋線的實錘症狀）→ 跟「隔著地形」同款處置：貼身走過去打。
        keep = (MELEE_RANGE if (blocked or self._push_in)
                else min(max(reach_keep - margin, move.MIN_GAP),
                         reach_keep - 0.5))

        # ★ 地形圖說「到不了」→ 換一隻（使用者定的規則）：牠站在走不進去的角落
        #   （水裡、圍起來的平台），我們走不過去、隔著地形也多半打不到。
        # ⛔ 舊條件裡的 `dist <= PATHFIND_RANGE`（25 格）**拿掉了**。那道門檻是
        #   為**遊戲的尋路**設的 —— 它一次只算得出約 30~40 格，更遠一律回 0，
        #   那是「太遠要接力」不是「到不了」，所以不能當真。現在判定改成
        #   我們自己的 A*（整張圖，沒有距離上限），算不出來就是**真的到不了**，
        #   再拿距離去擋只會讓 25 格外走不到的怪永遠不被放棄 ——
        #   那正是「一路走到牆邊卡著」的另一半原因（10 秒逾時前它會一直推牆）。
        # ⚠⚠ **已經打傷的怪絕不放棄**，而且要連續 UNREACH_HITS 次算不出來
        #   才算數 —— 怪會走動，打鬥中某一瞬間牠站到走不進去的格子，
        #   路徑就會變 0。少了這兩條會變成「打一下就換下一隻」，
        #   結果一路結仇、被圍毆致死（使用者實際遇到）。
        # ⚠ 貼在身上的**永遠不算走不到**：都走到牠旁邊了，還需要走去哪？
        if (self._unreach >= UNREACH_HITS and not self._hurt
                and dist is not None and dist > NO_PATH_NEED):
            self._cool_unreach(m.eid)
            self._atk.hold_off()
            self._cur = None
            self._keys.eid = None
            if not self._pick_next():
                self._keys.set_on(False)
                self._since_scan = SCAN_NOW
            self.status.setText(f"「{m.name}」走不到（卡在地形裡？）→ 換一隻")
            self._dbg(f"放棄「{m.name}」eid={m.eid:#x}：尋路連續 "
                      f"{UNREACH_HITS} 次算不出（{dist:.1f} 格）")
            return
        # ★★ 走路目標**一律就是怪本身**，繞路完全交給 walk_route()：
        #   它會對怪尋路，然後照 _approach_point() 的規則決定這一趟走到哪
        #   —— 路徑 2 點以上就走下一個轉角、只剩 1 點才朝怪推進到接戰距離。
        # ⛔ 這裡曾經自己算「路徑倒數第二點再沿最後一段推進」。**拿掉了**：
        #   那等於叫遊戲重新規劃一條到那個點的路（可能走別條），
        #   而且跟 walk_route 的逐點走法互相打架（使用者指出的）。
        gx, gy, gkeep = (mp[0], mp[1], keep) if mp else (None, None, keep)
        gd = (math.hypot(gx - me[0], gy - me[1])
              if (me and gx is not None) else None)
        # ⚠ 走路的條件**不能再要求「不在範圍內」** —— 遊戲自己打怪時就是
        #   一邊走一邊打（雪狐那次攔到 6 包移動 + 4 包動作 + 3 包施放）。
        #   要求不在範圍內的話，近戰會站在 10 格外一直送打不到的施放封包。
        # 要超過 gkeep **再多 WALK_SLACK 格**才走 —— 沒有這個容差的話，
        # 角色停在定位附近時 gd 只比 gkeep 多一點點，每個 WALK_GAP 就再推
        # 一小步，看起來就是「打一打又往前一格」（使用者回報的不流暢）。
        # ⚠⚠ 但**打不到的時候容差要失效**，否則會出現死區：
        #   攻擊範圍 12 格、走路門檻 11+1.5=12.5 格 → 怪停在 12.3 格時
        #   既不打也不走，就是「朝一個方向發呆」（監控抓到 3 次，
        #   距離全是 12.3~12.4）。
        # ⛔ 「太近也要下移動指令」（貼身持續微調／後退站位）拿掉了 ——
        #   使用者 2026-08-07 指定：貼身走位交棒給客戶端，被貼上來就站著打。
        # ⚠⚠ 容差的上限是「**還沒出射程**就要開始重新靠近」：
        #   以 reach−0.5 當界線，怪走到射程邊緣前 0.5 格我們就動身，
        #   才不會出現「已經打不到了卻還站著送封包」（使用者指出的症狀）。
        #   近戰 reach 2.0、停 1.4 → 容差 0.3（超過 1.7 就走）
        #   法師 reach 12、停 10  → 容差 1.0（超過 11.0 就走）
        # ⚠⚠ 這裡的 `reach−0.5` **不能拿來當「打得到」的保證** —— reach 是
        #   技能表寫的射程（12），而實測真正打得出傷害只到約 11.0 格。
        #   遠程真正的界線是 WALK_SLACK 那個 1.0（見它的說明與
        #   [[ranged-dead-band]]），不是這道 reach−0.5。
        slack = min(WALK_SLACK, max(0.3, reach_keep - 0.5 - gkeep))
        need_walk = gd is not None and (
            gd > gkeep + slack or (not in_range and gd > gkeep))
        # ★ 還在趕路（離目標 > FAR_ENOUGH）就用短冷卻，貼身微調維持 0.4 秒。
        walk_gap = (WALK_GAP_FAR if (gd is not None and gd > FAR_ENOUGH)
                    else WALK_GAP)
        if (me and not self._moving
                and self._walk_t >= walk_gap and need_walk):
            # ⚠ 這個回傳值**不能**寫進 _path_pts —— 它是「走到中繼點」的路徑
            #   點數，不是「跟怪之間有沒有地形」（見上面那段說明）。
            self._walked_ok = self._walk_toward(gx, gy, me, gkeep) > 0

        # 兩條執行緒對「現在是不是用封包打」要有共識：
        # 寫目標那條要據此決定「寫不寫血量」——
        #   封包攻擊：**不寫**，血量交給遊戲，讀到 0 就是死亡訊號
        #   按鍵攻擊：**要寫**，遊戲出手前會檢查 +0x2DC > 0，不餵就不打
        self._atk.packets = bool(self._keys.mode == MODE_PACKET
                                 and self._keys.packets and self._keys.skill
                                 and self._keys.mover is not None)
        # 選定封包送出去之後，才開始算「多久沒看到血量 = 屍體」
        self._atk.engaged = self._keys.selected
        # 出手執行緒自己也會用這兩個再驗一次距離（見 KeyWorker.step）
        self._keys.player = self.player
        # ⚠ `reach` 只在交棒那一輪有值：交棒時出手不看單招射程，只看這個
        #   總距離。平常是 0 ＝ **沒有單一攻擊距離**，每一招在 step() 裡
        #   各自比自己的射程（使用者指定）。
        self._keys.reach = HANDOFF_RANGE if handoff else 0.0
        self._keys.client_walk = handoff
        self._keys.set_on(in_range)

        # ★ 為什麼沒在打？把原因記下來給狀態列 —— 使用者回報「鎖定一隻怪發呆」，
        #   發呆一定是「不在範圍內、又沒有在走過去」，但成因有好幾種，
        #   直接標出來才不必猜。
        # ★★ 等首發技能：這段是**故意不出手**的（使用者要的「第一下一定要是
        #   那一招」），所以底下所有「沒進展就換一隻」的計時器都要凍住。
        waiting_opener = self._keys.open_wait > 0.0
        # 跳過首發（SP 不夠／讀不到）要**講出來**，不能安靜地發生。
        if self._keys.open_note:
            self.status.setText(self._keys.open_note)
            self._dbg(self._keys.open_note)
            self._keys.open_note = ""
        if in_range and dist is not None and dist <= keep:
            self._why = ""
        elif in_range:
            self._why = (f"⛰ 零傷害疑似擋線 → 繞過去貼身（停 {keep:.1f} 格）"
                         if self._push_in
                         else f"打得到，同時走近到 {keep:.1f} 格")
        elif dist is None:
            self._why = "⚠ 讀不到座標"
        elif self._mover is None or not self._mover.active:
            self._why = "⚠ 移動跳板沒裝上"
        elif self._no_grid:
            # 沒有地形圖就不走路（不再交給遊戲的尋路撞牆）——大聲說出來。
            self._why = f"⚠ {self._no_grid} → 這一拍不走位"
        elif not self._walked_ok:
            self._why = "⛔ 走不過去"
        elif blocked:
            self._why = (f"⛰ 隔著地形 → 沿路徑走到 ({gx:.0f},{gy:.0f})"
                         if gx is not None and len(self._way) >= 2
                         else "⛰ 隔著地形，走近一點")
        else:
            self._why = "→ 走進攻擊範圍"
        if waiting_opener:
            self._why = (f"⏳ 等首發技能 {self._opener_label()} 冷卻好"
                         f"（{self._keys.open_wait:.0f} 秒）")

        # ★ 診斷紀錄：每秒把這一拍的**決策**寫進檔案。
        #   從外面只看得到「站太遠」，看不到為什麼不走 —— 這幾輪我猜了太多次。
        #   環境變數 AO_FARM_LOG=1 才會開，平常完全不做事。
        self._dbg_t += dt
        if _FARM_LOG and self._dbg_t >= 1.0:
            self._dbg_t = 0.0
            try:
                with open(f"farm_debug_{self.account}.log", "a",
                          encoding="utf-8") as fh:
                    fh.write(
                        f"{time.strftime('%H:%M:%S')} "
                        f"離怪 {dist if dist is None else round(dist,1)} "
                        f"停 {keep:.1f} 打得到={in_range} "
                        f"路徑點={self._path_pts} 走不到次數={self._unreach} "
                        f"要走={need_walk} 走成功={self._walked_ok} "
                        f"貼身繞打={int(self._push_in)} "
                        f"移動中={self._moving} 卡住={self._stuck:.1f}s "
                        f"血={hp} 冷卻中={len(self._killed)} "
                        f"｜{self._why or '正常攻擊中'}\n")
            except OSError:
                pass

        # ⛔ 這裡曾經加過「讀不到座標超過 N 秒就換一隻」—— 拿掉了。
        #    那是用 timeout 蓋過症狀，而且量過根本沒發生：
        #    掃描 100 輪，狀態與玩家物件**都是 100/100 剛好命中 1 個**。

        # 卡住偵測（次要保險，不是主要機制）：目標已經是最近的一隻，
        # 正常情況下不是打得到就是角色正在走過去。若血量不掉、玩家座標也不動，
        # 代表這隻走不過去（隔著地形之類），換一隻。
        # ⚠ 用上面那個「隔 0.3 秒取樣」的結果，不要拿相鄰兩拍比 ——
        #   心跳 10ms，角色每拍才走 0.09 格，那樣比永遠都是「沒在動」，
        #   於是走路途中也會一直累積卡住秒數，走到一半就被判定走不過去換怪。
        # ★ 怪掉血 = 這個距離打得到 → 記下來當作「不用再靠近」的門檻。
        #   這是**唯一**不必知道各角色射程、也不必量測的辦法。
        if 0 < hp < self._last_hp:
            self._hurt = True          # 打傷過的怪就不要再放棄（見上面）
            self._push_in = False      # 傷害進得來＝沒被擋線，不必再貼身繞打
        # ⚠⚠ **用「離錨點的淨位移」判斷有沒有前進**，不要用 self._moving。
        #   撞牆時角色會原地抖動（實測約 0.5~0.6 格），而 self._moving 讀的是
        #   遊戲的動畫狀態 —— 撞著牆它照樣是 'Run'，那個計時器就永遠歸零，
        #   唯讀監控實拍卡了 32 秒都沒觸發（見 STUCK_EPS 的說明）。
        #   「有沒有真的前進」只能看位移。
        if me and (self._anchor is None
                   or math.hypot(me[0] - self._anchor[0],
                                 me[1] - self._anchor[1]) > STUCK_EPS):
            self._anchor = me          # 真的移動了 → 重設錨點
            self._stuck = 0.0
        elif (0 < hp < self._last_hp) or self._last_hp < 0:
            self._stuck = 0.0          # 在掉血 = 有進展，站著打也算
        else:
            self._stuck += dt
        self._last_hp = hp
        # ⚠⚠ 等首發的那幾秒不算「沒進展」：怪本來就不會掉血。不凍住的話
        #   「15 秒沒進展」會把牠丟掉換一隻，換完又從頭等首發 ——
        #   變成**永遠不出手**的迴圈（見 [[frozen-tick-state-machines]]）。
        if waiting_opener:
            self._stuck = 0.0
        # ⚠ 讀不到地形圖的那幾拍也要凍住：那時我們**故意不走路**，
        #   不能把自己的讀取失敗算成「這隻怪走不過去」而把牠冷凍起來
        #   （見 [[frozen-tick-state-machines]]：別讓保險機制去罰無辜的目標）。
        if self._no_grid:
            self._stuck = 0.0
        # ★★ 還沒打傷牠之前，冒出更近的怪就改打那隻（使用者要求）。
        #   放在 15 秒耐心之前：能換就換，不必等它逾時。
        if self._switch_closer(m, dist):
            return
        # ★★ 「沒進展」要等多久才放棄，**打得到的時候要有耐心**。
        #   STUCK_SECS(10 秒) 是為「走不過去」設計的；套在交戰上剛好是災難：
        #   等級差得多時命中率低，一隻要打十幾秒才會倒 —— 實測（雪狐 82 級
        #   打 91/93 級）擊殺耗時 9.7 / 9.8 / 10.8 秒，**門檻正好卡在中位數**，
        #   於是快贏的前一刻自己放棄，還把那隻丟進 8→120 秒的遞增冷卻。
        #   同場同怪 60 秒對照：官方掛（85 級）鎖 6 殺 6，我們鎖 6 只殺 2。
        # ⚠ 冷卻也要換成短的（NOHP_MEMORY）：這不是「走不到」，用遞增冷卻
        #   會把整片打得到的怪一隻隻凍起來，越打越沒得打。
        engaged = bool(in_range and self._keys.selected)
        # ★★ 站定 PUSH_IN_SECS 秒、打得到、選定也送了、血卻一滴不掉 ——
        #   十之八九是技能被地形擋線（怪在障礙物對面）。別傻站到 15 秒換怪：
        #   改成**繞過去貼身打**（keep 壓到 MELEE_RANGE，下一拍起照 blocked
        #   同款走位：有 _way 走 _way 繞，直線可走就直走；邊走邊照打，
        #   擋線一消失第一發就有傷害、掉血就解除）。詳見 PUSH_IN_SECS。
        # ⚠ 交棒中不觸發：那時走位是客戶端自己在走，我們再下移動指令會打架
        #   （交棒走不動自有 HANDOFF_WAIT 3 秒收回來，收回來之後這裡才接手）。
        # ⚠ dist 要驗 None：in_range_of_any(None) 刻意回 True（讀不到座標
        #   不代表打不到），engaged 不保證有距離。
        if (engaged and not self._push_in and not handoff
                and self._stuck >= PUSH_IN_SECS
                and dist is not None and dist > NO_PATH_NEED):
            self._push_in = True
            self.status.setText(
                f"「{m.name}」打得到卻 {PUSH_IN_SECS:.0f} 秒零傷害"
                f"（隔著障礙物？）→ 繞過去貼身打")
            self._dbg(f"零傷害 {PUSH_IN_SECS:.0f} 秒（{dist:.1f} 格）"
                      f"→ 貼身繞打「{m.name}」eid={m.eid:#x}")
        limit = STUCK_ENGAGED if engaged else STUCK_SECS
        if self._stuck >= limit:
            if engaged:
                self._killed[m.eid] = time.monotonic() + NOHP_MEMORY
            else:
                self._cool_unreach(m.eid)  # 走不過去，用遞增冷卻
            self._atk.hold_off()
            self._cur = None
            self._keys.eid = None
            if not self._pick_next():
                self._keys.set_on(False)
                self._since_scan = SCAN_NOW
            self.status.setText(
                f"「{m.name}」{limit:.0f} 秒沒進展"
                + ("（打不中？）" if engaged else "（走不過去？）") + " → 換一隻")
            self._dbg(f"放棄「{m.name}」eid={m.eid:#x}：{limit:.0f} 秒沒進展"
                      + ("（交戰中）" if engaged else "")
                      + (f"（距離 {dist:.1f} 格）" if dist is not None else ""))
            return

        # ⛔ 這裡不再每 0.2 秒重寫一次「掛機中：…」——
        #    使用者明講那行一直在跳、看了很煩。狀態列只保留**有事發生**時的
        #    訊息（換目標、走不過去、武器壞了、角色死亡、巡邏中…）。

    # ------------------------------------------------------------------
    # -- 設定的保存與載入（每個帳號各自一份）----------------------------
    #
    # ★ 用**帳號名**當 key，不能用 PID —— 玩家關掉重開遊戲 PID 就變了
    #   （見 [[memory-re-pitfalls]] 第 10 條）。
    # ★ 「開始掛機」刻意**不存**：使用者明講「那個每次都要是關閉的，
    #   因為我不能幫他打開」—— 程式一開就自己開打太危險。
    def _key(self, field: str) -> str:
        # ★ 前綴要跟分頁綁在一起，兩個掛機分頁的設定才不會互相覆蓋。
        return f"{self._prefix}.{self.account}.{field}"

    def _seed_from_default_tab(self) -> None:
        """這個帳號第一次用這個分頁 → 把「自動掛機」那邊的設定整份帶過來。

        ⚠ 沒有這段的話，新分頁對使用者來說就是「設定全不見了」——
          實際踩過：在新分頁掛機時角色不回原點，看起來像「沒在算路徑」，
          其實是那份設定裡原點是 null、「沒怪時回原點」也沒勾。
        帶過來之後才照常載入，所以使用者之後在這個分頁改的設定不受影響。
        """
        if self._prefix == "farm":
            return
        src = config.get(f"farm.{self.account}", None)
        if not isinstance(src, dict):
            return
        mine = config.get(f"{self._prefix}.{self.account}", None)
        mine = mine if isinstance(mine, dict) else {}
        changed = False
        for k, v in src.items():
            # 只補「沒有」或「存成 null」的欄位 —— 使用者在這個分頁明確設過的
            # （例如把「沒怪時回原點」關掉）就不要覆蓋回去。
            if mine.get(k, None) is None:
                config.set(self._key(k), v)
                changed = True
        if changed:
            config.save()

    def _load_settings(self) -> None:
        self._seed_from_default_tab()
        g = config.get
        for name in g(self._key("monsters"), []) or []:
            self._add_name(str(name))
        # 技能鍵：新格式是清單（"vks"）；沒有就吃舊的單鍵設定（"vk"）——
        # 使用者原本選好的鍵不能憑空變回預設。
        vks = g(self._key("vks"), None)
        if not vks:
            vks = [g(self._key("vk"), DEFAULT_KEY)]
        picked = {int(v) for v in vks}
        for cb, vk, _ in self._key_cbs:
            cb.setChecked(vk in picked)
        # 首次攻擊（0 = 不指定）。存的是鍵碼，用 findData 找回位置 ——
        # 找不到（設定壞了）就退回「不指定」，不要當掉。
        idx = self.open_box.findData(int(g(self._key("opener_vk"), 0) or 0))
        self.open_box.setCurrentIndex(idx if idx >= 0 else 0)
        # ⛔ 舊的 "move"（自動走過去）/"patrol"/"back" 不再讀：走位永遠開、
        #    巡邏＝有設巡邏點就巡（見「移動與巡邏」群組移除處）。
        self.boss_cb.setChecked(bool(g(self._key("boss_only"), False)))
        self.notify_cb.setChecked(bool(g(self._key("notify_on"), True)))
        self.buff_cb.setChecked(bool(g(self._key("auto_buff"), False)))
        # 學過的分身技能編號 —— 有存就不用再按 F12 學一次
        self._buff = buff.AutoBuff(
            BUFF_KEY, int(g(self._key("buff_skill"), 0) or 0))
        # 召喚技能不用存：每次掛機都直讀快捷欄 F11（零副作用、當場拿到）
        self.summon_cb.setChecked(bool(g(self._key("auto_summon"), False)))
        self.ball_cb.setChecked(bool(g(self._key("auto_ball"), False)))
        self.rot_cb.setChecked(bool(g(self._key("rotate"), False)))
        self.sup_gear_cb.setChecked(bool(g(self._key("supply_gear"), False)))
        # ⛔ 舊的 supply_potion / supply_hp / supply_mp 不再讀：藥水補給
        #    全自動（2026-08-19 使用者要求，勾選框已刪）。
        self.sup_jump_cb.setChecked(bool(g(self._key("supply_jump"), False)))
        self.sup_revive_cb.setChecked(
            bool(g(self._key("supply_revive"), False)))
        self.rot_every.setValue(float(g(self._key("rot_every"), 30.0)))
        self.rot_stay.setValue(float(g(self._key("rot_stay"), 60.0)))
        # ⛔ 舊的 "range"（接戰距離）不再讀：停多遠改成照技能射程自動算。
        # ⛔ 舊的 rest_hp_on / rest_hp / rest_mp_on / rest_mp（坐下休息）不再讀：
        #   功能 2026-08-09 移除。舊設定檔裡那幾個鍵留著不管，不會有影響。
        # 巡邏點。兩種舊格式都要吃得下，不然使用者設好的點會憑空消失：
        #   最舊：只有一個「原點」home = [x, y]
        #   舊  ：spots = [[x, y], ...]        ← 沒有地圖，載進來標成「未標記」
        #   現在：spots = [[x, y, 場景編號], ...]
        spots = g(self._key("spots"), None)
        if not spots:
            home = g(self._key("home"), None)
            spots = [home] if (isinstance(home, (list, tuple))
                               and len(home) == 2) else []
        self._spots = []
        for p in spots:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            sid = int(p[2]) if len(p) > 2 and p[2] is not None else None
            self._spots.append((float(p[0]), float(p[1]), sid))
        self._refresh_spots()
        if g(self._key("notify"), "sound") == "telegram":
            self.rb_tg.setChecked(True)
        self.tg_id.setText(str(g(self._key("tg_id"), "") or ""))

    def _on_robot_pref(self, _on: bool) -> None:
        """會連動精靈設定的勾選變動（趴趴GO回地圖／死亡回練功區／壞裝觸發）。

        一改就存；**掛機中**勾上還會立刻把精靈設定調到位，不必重開掛機。
        取消勾選只代表「我們不再管」—— 不會把遊戲裡的設定改回去。
        """
        self._save_settings()
        if self._loading or not self.run_cb.isChecked():
            return
        if not (self._mover is not None and self._mover.active):
            return
        notes = robot.apply_prefs(
            self._mover, self.sc,
            jump_back=self.sup_jump_cb.isChecked(),
            revive_mark=self.sup_revive_cb.isChecked())
        # ★ 保證購買清單有天使之翼×50（只加翼，不開精靈補給旗標）。
        #   藥水補給全自動（2026-08-19）→ 不再看勾選，一律確保。
        note = robot.ensure_buy_item(
            self._mover, self.sc, recall.RECALL_ITEM, robot.BUY_KEEP_WINGS)
        if note:
            notes.append(note)
        if notes:
            self.status.setText("精靈設定：" + "、".join(notes))

    def _sel_vks(self) -> list[int]:
        """勾選的技能鍵（照 F1→F12 順序）。"""
        return [vk for cb, vk, _ in self._key_cbs if cb.isChecked()]

    def _label_keys(self) -> None:
        """把「技能鍵」選單與「首次攻擊」下拉標上快捷欄現在放什麼。

        技能格 → F1（電擊術Ⅳ）、物品格 → F5（物品：高效紅藥水）、
        空格 → F7（空）。讀不到快捷欄（還沒進遊戲／改版位移）就維持
        素的 F1~F12，不亂標。純讀取，開著掛機點開也沒差。

        ⚠ 只改字（setText/setItemText），**不重建清單** —— 重建會讓順序跳動
          （見 [[qt-ui-pitfalls]]）。兩個元件展開時各自呼叫這一支。
        """
        try:
            cells = quickbar.read_page(self.sc, self._qb_ui.page())
        except Exception:                      # noqa: BLE001
            cells = None
        for i, (cb, _, label) in enumerate(self._key_cbs):
            text = label
            if cells is not None:
                c = cells[i]
                if c is None:
                    text = f"{label}（空）"
                elif c.is_skill:
                    nm = skills.name_of(c.value) or f"技能{c.value}"
                    # 位移／補血／buff 這類放進攻擊循環只會幫倒忙，標出來
                    # （循環本來就會跳過它們，見 KeyWorker.skip_note）
                    if not skills.is_attack(c.value):
                        nm += "・非攻擊"
                    text = f"{label}（{nm}）"
                elif c.is_item:
                    nm = itemname.of(c.value)
                    text = f"{label}（物品：{nm}）" if nm else f"{label}（物品）"
            cb.setText(text)
            # 首次攻擊的下拉：第 0 項是「不指定」，之後才照 F1~F12。
            self.open_box.setItemText(i + 1, text)

    def _opener_label(self) -> str:
        """狀態列用的首發技能名稱：F3（破甲劈擊Ⅳ）；查不到就只寫鍵名。"""
        vk = self._keys.opener_vk
        if not vk:
            return ""
        key = (f"F{vk - quickbar.VK_F1 + 1}"
               if quickbar.VK_F1 <= vk < quickbar.VK_F1 + quickbar.SLOTS
               else f"鍵{vk:#x}")
        sid = self._keys.skills.get(vk)
        nm = skills.name_of(sid) if sid else ""
        return f"{key}（{nm}）" if nm else key

    def _opener_changed(self) -> None:
        """首次攻擊改了：推給攻擊執行緒，並且**當場把鎖打開**。

        ⚠ 不重設的話，改設定的當下正在等的那一隻會繼續等舊的那一招。
        """
        self._keys.opener_vk = int(self.open_box.currentData() or 0)
        self._keys._open_eid = None
        self._keys.open_wait = 0.0
        # 掛機中臨時設／取消首發 → 照需求裝卸（自動分身要用的話不會被卸掉）
        self._sync_castwatch()
        self._save_settings()

    def _sync_key_btn(self) -> None:
        """按鈕字樣＝勾了哪些鍵；勾太多就縮寫，別把整條列撐爆。"""
        labels = [lab for cb, _, lab in self._key_cbs if cb.isChecked()]
        if not labels:
            self.key_btn.setText("選技能鍵")
        elif len(labels) <= 4:
            self.key_btn.setText("、".join(labels))
        else:
            self.key_btn.setText(f"{labels[0]} 等 {len(labels)} 鍵")

    def _keys_changed(self) -> None:
        """勾選變了：更新按鈕字樣、推給攻擊執行緒；掛機中就立刻重讀快捷欄。"""
        vks = self._sel_vks()
        self._sync_key_btn()
        self._keys.vks = vks or [DEFAULT_KEY]   # 全不勾＝退回預設 F2
        if self.run_cb.isChecked():
            self._keys.stats = self.stats
            self._keys.begin_learning()
        self._save_settings()

    def _save_settings(self) -> None:
        if self._loading:
            return
        s = config.set
        s(self._key("monsters"), self.wanted())
        s(self._key("vks"), self._sel_vks())
        s(self._key("opener_vk"), int(self.open_box.currentData() or 0))
        s(self._key("boss_only"), self.boss_cb.isChecked())
        s(self._key("notify_on"), self.notify_cb.isChecked())
        s(self._key("auto_buff"), self.buff_cb.isChecked())
        s(self._key("buff_skill"), int(self._buff.skill or 0))
        s(self._key("auto_summon"), self.summon_cb.isChecked())
        s(self._key("auto_ball"), self.ball_cb.isChecked())
        s(self._key("rotate"), self.rot_cb.isChecked())
        s(self._key("supply_gear"), self.sup_gear_cb.isChecked())
        # ⛔ supply_hp / supply_mp 不再存：藥水補給全自動（勾選框已刪）。
        s(self._key("supply_jump"), self.sup_jump_cb.isChecked())
        s(self._key("supply_revive"), self.sup_revive_cb.isChecked())
        s(self._key("rot_every"), float(self.rot_every.value()))
        s(self._key("rot_stay"), float(self.rot_stay.value()))
        s(self._key("spots"), [[x, y, sid] for x, y, sid in self._spots])
        s(self._key("notify"), "telegram" if self.rb_tg.isChecked() else "sound")
        s(self._key("tg_id"), self.tg_id.text().strip())
        config.save()

    def _wire_saving(self) -> None:
        """所有設定一改就存 —— 不要讓使用者每次都重設一遍。

        （技能鍵不在這裡：勾選框接的是 _keys_changed，那裡自己會存。）
        """
        self.picked.model().rowsInserted.connect(self._save_settings)
        self.picked.model().rowsRemoved.connect(self._save_settings)
        self.boss_cb.toggled.connect(self._save_settings)
        self.rot_cb.toggled.connect(self._save_settings)
        self.rot_every.valueChanged.connect(self._save_settings)
        self.rot_stay.valueChanged.connect(self._save_settings)
        self.rb_tg.toggled.connect(self._save_settings)
        self.tg_id.editingFinished.connect(self._save_settings)

    # ------------------------------------------------------------------
    def notify(self, msg: str) -> None:
        """送警報通知。設定是這個分身自己的（通知列在頁面最上面）。

        ⚠ 受「啟用通知」總開關管（使用者要求）。關掉只是不送通知，
          該停的還是會停 —— 停機原因照樣寫在狀態列。
        """
        if self._notifier is None or not self.notify_cb.isChecked():
            return
        who = f"{self.account}（{self.char_name}）"
        note = self._notifier.fire(who, msg)
        self.status.setText(self.status.text() + f"　[{note}]")

    def _stop_with(self, msg: str) -> None:
        self.run_cb.setChecked(False)
        self.status.setText(msg)


class FarmTab(ClientWatchMixin, BaseTab):
    """自動掛機。

    出手方式由頁面上的「攻擊型態」決定（遠程送封包、近戰按鍵），
    所以不需要兩個分頁 —— 之前那個「自動練功按鍵」分頁已經移除。

    分身列表**背景自動對帳**（沒有「重新偵測分身」按鈕了）：關掉的收走、
    新開的補上、沒動的分頁完全不碰 —— 掛機中的分頁絕不重建。
    勾勾的意向記憶見 ClientWatchMixin（存程式記憶體，不進 config）。
    """

    TAB_TITLE = "自動掛機"
    ORDER = 5
    ATTACK_MODE = MODE_PACKET         # 頁面上的「攻擊型態」會覆寫這個
    SETTINGS_PREFIX = "farm"          # 設定存在 config 的哪個前綴底下

    def build_ui(self) -> None:
        self._pages: dict[int, CharFarmPage] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        self._worker: ScanWorker | None = None
        self._inv: InvWorker | None = None    # 找物品陣列表頭（AOB 全掃，很慢）
        # 每個分身一對（寫入執行緒, 送鍵執行緒）—— 收掉那台時只停它自己的
        self._keys: dict[int, tuple[_Paced, _Paced]] = {}
        self._watch_init()
        # ★「自動練技」的意向記憶（帳號→bool），跟 run_cb 的 _intent 同一套
        #   規則：只記使用者親手點的、不進 config、登入驗完名字自動接回去 ——
        #   遊戲重開會把「原地重複練習技能」關掉，靠這條把練技整包接回來
        #   （接回後 _train_push 看門狗會把練習技能推回開）。
        self._train_intent: dict[str, bool] = {}

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.found = QLabel("尚未偵測")
        self.found.setStyleSheet("color: #9aa2b8;")
        bar.addWidget(self.found)
        # ★ AOB 自動定位的結果。平常是空的；遊戲改版讓位址位移時會在這裡說出來，
        #   不然使用者只會看到「怪怪的」卻不知道發生什麼事（上次改版就是這樣）。
        self.locate_lbl = QLabel("")
        bar.addWidget(self.locate_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        # 切到另一台分身就立刻刷一次「周圍怪物」（見 _refresh_shown）
        self.tabs.currentChanged.connect(lambda _i: self._refresh_shown())
        root.addWidget(self.tabs, 1)

        # ⛔ 這裡原本有一大段說明文字，使用者要求全部拿掉 ——
        #    每個控制項自己的滑鼠提示已經寫得夠清楚了。

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    def _show_locate(self) -> None:
        """把 AOB 自動定位＋資料表戳記的結果顯示出來（沒事就不顯示）。"""
        moved, failed = locate.moved(), locate.failed()
        stale = tablestamp.check()   # 遊戲換版但寫死資料表還沒重新核對
        parts, color = [], ""
        if failed:
            # ⚠ 函式位址驗不過會被清成 0＝該功能停用（不是沿用舊值），
            #   資料位址才是沿用舊值。見 app/game/locate.py 檔頭。
            parts.append(
                f"⚠ 有 {len(failed)} 個遊戲位址定位失敗（相關功能已停用）："
                + "、".join(failed[:3]))
            color = "#e0b040"
        elif moved:
            parts.append(f"偵測到遊戲改版，已自動重新定位 {len(moved)} 個位址")
            color = "#7fc97f"
        if stale:
            # 位址跟得上 ≠ 資料表跟得上：AOB 救位址，救不了抄來的內容
            #（射程/地圖編號）。這條要一直亮到重新核對＋蓋章為止。
            parts.append("⚠ " + stale)
            color = "#e0b040"
        text = "　".join(parts)
        self.locate_lbl.setText(text)
        self.locate_lbl.setToolTip(text)   # 條太長被截掉時滑鼠移上去看全文
        self.locate_lbl.setStyleSheet(f"color: {color};" if color else "")

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        self._watch_start()          # 第一次切過來才開始對帳（懶載入照舊）
        self._refresh_shown()

    def _refresh_shown(self) -> None:
        """切到某台分身（或切回這個分頁）時，**立刻**刷一次「周圍怪物」。

        不然要等最多一個刷新間隔才更新，切過去的第一眼看到的
        是離開前那一拍的清單 —— 中間換過地圖的話整批都是舊圖的怪。
        ★ 只是把「距離上次掃描」推到門檻以上，下一拍心跳（10ms）就會送請求；
          不繞過 `_waiting` 那個閂，也不會多送重複的請求。
        """
        page = self.tabs.currentWidget()
        if isinstance(page, CharFarmPage):
            page._since_scan = SCAN_NOW

    def _found_note(self) -> None:
        self.found.setText(f"偵測到 {len(self._pages)} 個分身"
                           if self._pages else "找不到分身")

    def _ensure_workers(self) -> None:
        """共用的兩條工作執行緒建一次就一直留著（分身歸零也不收 —— 閒著沒成本，
        再開分身直接接上）。"""
        if self._worker is None:
            self._worker = ScanWorker()
            self._worker.done.connect(self._on_scan_done)
            # 掃描讓路給攻擊：掃描是大量記憶體讀取，攻擊只要準時。
            self._worker.start(QThread.LowPriority)
        if self._inv is None:
            self._inv = InvWorker()
            self._inv.found.connect(self._on_inv_found)
            self._inv.start(QThread.LowestPriority)

    # -- 意向記憶（掛機＋練技，兩者互斥所以要一起記）---------------------
    def _note_farm_intent(self, page) -> None:
        """使用者親手點「開始掛機」→ 記掛機意向；勾上時練技會被互斥放掉，
        練技意向也要跟著清 —— 不清的話重連後兩個都想接手，練技後接手會把
        使用者剛開的掛機又關掉。"""
        self._note_intent(page)
        if page.run_cb.isChecked() and page.account:
            self._train_intent[page.account] = False

    def _note_train_intent(self, page) -> None:
        """使用者親手點「自動練技」→ 記練技意向；同上，勾上時清掛機意向。"""
        if page.account:
            self._train_intent[page.account] = page.train_cb.isChecked()
            if page.train_cb.isChecked():
                self._intent[page.account] = False

    def _maybe_resume(self, page) -> None:
        """接回意向：先讓 mixin 接掛機，再接練技。

        兩個意向互斥地記（見上面兩支 note），所以不會同時為 True；
        真的都 True（不該發生）也是後接手的練技贏 —— toggle 會把掛機放掉。
        """
        super()._maybe_resume(page)
        if (self._train_intent.get(page.account) and self._name_ok(page)
                and not page.train_cb.isChecked()):
            page.train_cb.setChecked(True)

    def _client_new(self, w) -> None:
        """接上一台新分身。開失敗就先跳過 —— 下一拍對帳會再試。"""
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
        except Exception:                      # noqa: BLE001
            return
        # ★ 接上就用 AOB 掃一次，把所有寫死的遊戲位址換成當下正確的
        #   —— 遊戲改版會讓它們整批位移（見 app/game/locate.py）。
        #   只做一次（五台載的是同一份 angel.dat），失敗就保留原值。
        try:
            locate.warm(sc)
        except Exception:                      # noqa: BLE001
            pass                               # 定位是加分項，壞掉不能擋住掛機
        self._scanners[w.pid] = sc
        self._ensure_workers()
        tgt, keys = TargetWorker(sc), KeyWorker(w.hwnd, sc)
        tgt.start(QThread.HighPriority)
        keys.start(QThread.HighPriority)
        self._keys[w.pid] = (tgt, keys)
        acct = charname.account_from_title(w.title)
        # ⚠ 只查預讀快取，**不要在這裡掃記憶體**（GUI 執行緒；全掃一台
        #   1.1~1.8 秒）。新開的還在登入畫面讀不到名字 → 先用帳號當標籤，
        #   mixin 的背景重讀解出真名後會自己換掉。見 app/core/preload.py。
        nm = preload.name_of(w.pid, account=acct)
        # notifier 傳 None → 每個分頁自己建一個，讀自己那一列的設定
        page = CharFarmPage(w.pid, w.hwnd, w.title, sc, self._request_scan,
                            tgt, keys, None, acct, nm,
                            self.ATTACK_MODE, self.SETTINGS_PREFIX)
        page._notifier.failed.connect(self.found.setText)
        page.run_cb.clicked.connect(lambda _on, p=page: self._note_farm_intent(p))
        page.train_cb.clicked.connect(
            lambda _on, p=page: self._note_train_intent(p))
        self._pages[w.pid] = page
        self.tabs.addTab(page, nm or acct or str(w.pid))
        self._show_locate()
        self._found_note()
        self._maybe_resume(page)

    def _client_gone(self, pid: int) -> None:
        """收掉一台已關閉的分身（只動這一台；其他台的掛機完全不受影響）。

        順序照 on_close 那套：放勾勾 → 還跳板 → 停警報 → 停它自己的兩條
        執行緒 → 拆分頁 → 關 scanner（執行緒沒收乾淨就不關，理由同 on_close）。
        """
        page = self._pages.pop(pid)
        try:
            page.run_cb.setChecked(False)      # 遊戲已關，停止流程的讀寫會自己失敗
        except Exception:                      # noqa: BLE001
            pass
        try:
            page.train_cb.setChecked(False)    # 練技收尾的寫入失敗也會被吞掉
        except Exception:                      # noqa: BLE001
            pass
        # ⚠ 一定要還掉移動 hook；用 release() 不要 stop()（PID 共用，見 move.acquire）
        if page._mover is not None:
            try:
                move.release(page.pid, page)
            except Exception:                  # noqa: BLE001
                pass
            page._mover = None
        # castwatch 也要收（行程已不在，寫回會失敗被吞掉 —— 重點是把
        # per-pid 共用表裡這個 pid 的項目清掉，免得 PID 重用時借到殭屍 hook）
        page._release_castwatch()
        if page._notifier is not None:
            try:
                page._notifier.stop()
            except Exception:                  # noqa: BLE001
                pass
        stuck = False
        for th in self._keys.pop(pid, ()):
            th.stop()
            if not th.wait(5000):
                stuck = True
        i = self.tabs.indexOf(page)
        if i >= 0:
            self.tabs.removeTab(i)
        page.deleteLater()
        sc = self._scanners.pop(pid, None)
        # ⚠ 執行緒沒停乾淨／背景還在讀名字 → scanner 不准關（控制碼會被回收
        #   給別的物件用），寧可漏關，行程結束 OS 自然會收。
        if sc is not None and not stuck and not self._sc_busy(pid):
            try:
                sc.close()
            except Exception:                  # noqa: BLE001
                pass
        self._found_note()

    def _request_scan(self, pid: int, full: bool = False) -> bool:
        """把掃描請求排進工作執行緒。**回傳有沒有真的排進去**。

        ⚠ 回 False 時呼叫端要自己把「正在等結果」放掉 —— 沒排進去就不會有
          結果回來，等下去等於那台分身的掃描永遠停在這裡（見 tick 的看門狗）。
        """
        page = self._pages.get(pid)
        if page is None or self._worker is None:
            return False
        if full:
            self._worker.force_full(pid)
        self._worker.request(pid, page.sc)
        return True

    def _on_scan_done(self, s: Scan) -> None:
        page = self._pages.get(s.pid)
        if page is None:
            return
        page.apply_scan(s)
        # 表頭還沒找到（或失效）就請 InvWorker 去找 —— 那是 AOB 全掃，
        # 絕不能放在怪物清單那條執行緒上。
        if s.inv is None and self._inv is not None:
            self._inv.request(s.pid, page.sc)

    def _on_inv_found(self, pid: int, head: int) -> None:
        if self._worker is not None:
            self._worker.set_inventory_head(pid, head)
        page = self._pages.get(pid)
        if page is not None:
            page.inv = head

    def _tick(self) -> None:
        dt = TICK_MS / 1000.0
        for page in list(self._pages.values()):
            try:
                page.tick(dt)
            except Exception as exc:               # noqa: BLE001
                self._page_failed(page, exc)

    def _page_failed(self, page, exc: Exception) -> None:
        """一台分身的心跳丟例外 → **只停那一台**，不要把整個工具箱帶走。

        ⚠⚠ tick 掛在 QTimer 上（UI 執行緒），未捕捉的例外會直接走到
          main.py 的全域攔截 → 訊息框 → **程式關閉**。使用者實際遇到的那次
          是遊戲被關掉，`move.call_sync` 讀跳板讀到 ERROR_PARTIAL_COPY(299)，
          結果所有分身一起被關掉（crash.log 有完整呼叫鏈）。
          那個根因已經在 move.py／`_check_game_gone` 修掉了，這裡是最後一道
          防線：**任何**沒想到的例外都只該讓一台停用，不該關掉整個程式。
        ★ 不是安靜地吞掉：那一台的勾勾放掉、狀態列寫紅字、完整 traceback
          照樣寫進 crash.log（路徑也顯示在下面那一列）。
        """
        path = crashlog.record(f"掛機分頁 心跳例外（PID {page.pid}）", exc)
        try:
            page.halt(f"⚠ 這台分身發生未預期的錯誤，已停止：{exc}")
        except Exception:                          # noqa: BLE001
            pass                                   # 停用流程本身也不准再炸
        self.found.setText(
            f"⚠ 有一台分身出錯已停止（PID {page.pid}）"
            + (f"　紀錄檔：{path}" if path else ""))

    # ------------------------------------------------------------------
    def _teardown(self) -> None:
        for page in self._pages.values():
            page.run_cb.setChecked(False)
            # ⚠ 一定要還掉移動 hook —— 不還原 IAT 就等於在遊戲裡留了一段跳板。
            #   ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
            #     能量晶化分頁可能還在用（見 move.acquire）。
            if page._mover is not None:
                move.release(page.pid, page)
                page._mover = None
            # ⚠⚠ castwatch 的 inline hook 也**必須**還（無條件，不走
            #   _sync_castwatch —— 自動分身勾著的話它會判定「還要」而留著）。
            #   不還原的話遊戲裡留著我們的 jmp，下次開工具箱 AOB 掃不到
            #   INBOUND_FN → 自我監察誤當改版把程式關掉
            #   （2026-08-19 使用者實際踩到，0.4.39 的回歸）。
            page._release_castwatch()
            # ⚠ 警報也要停：BeepThread／QMediaPlayer 還在響的話，物件被回收
            #   時等於「執行緒還在跑就被解構」，而且聲音會一直放到關掉程式。
            if page._notifier is not None:
                try:
                    page._notifier.stop()
                except Exception:                    # noqa: BLE001
                    pass
        stuck = False
        threads = [t for t in (self._worker, self._inv) if t]
        for pair in self._keys.values():
            threads += list(pair)
        for th in threads:
            th.stop()
            if not th.wait(5000):
                stuck = True
        self._worker = None
        self._inv = None
        self._keys = {}
        self.tabs.clear()
        self._pages = {}
        # ⚠⚠ 有執行緒沒收乾淨就**不要關控制碼**：牠可能正卡在
        #   ReadProcessMemory 裡（AOB 全掃跑很久），控制碼在腳下被關掉，
        #   最壞的情況是那個控制碼值被系統回收給別的物件用。
        #   寧可留著不關（行程結束時作業系統自然會收）。
        if stuck:
            self._scanners = {}
            return
        for pid, sc in self._scanners.items():
            if self._sc_busy(pid):             # 背景讀名字中，理由同上
                continue
            try:
                sc.close()
            except Exception:
                pass
        self._scanners = {}

    def on_close(self) -> None:
        self._timer.stop()
        self._watch_stop()
        self._teardown()
