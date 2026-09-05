"""自動刷副本：照腳本一步一步跑，路上有怪先清光。

    勾「自動刷副本」→ 選一份腳本 → 立刻開始跑。

## 規格（使用者 2026-09-01 定案）

* 技能怎麼放**照自動掛機那樣**：勾 F 鍵、直讀快捷欄、照 F1→F12 輪流施放
  —— 所以這裡直接重用掛機的 `KeyWorker`／`TargetWorker`／`ScanWorker`，
  不另外寫一套出手邏輯（寫第二套就會有一套跟不上）。
* **去腳本點位的路上有怪，先殺光再走**。
* **結束＝整份腳本跑完，而且周圍沒有任何怪物**（使用者定的收工條件）。
* 組隊這一版不做。

## 打怪流程＝掛機頁那一套的**複本**（使用者 2026-09-04 定案）

> 「改成跟掛機一樣，不過是複製一份幾乎一樣的不要共用；
>   超過 30 格要跳過；不管怎樣不會把怪物加黑名單，就換隻就好」

所以本檔 `_candidates`／`_pick_next`／`_engage`／`_switch_closer`／
`_walk_toward`／`_fight` 是從 farm_tab 抄過來的（挑最短**路徑**的怪、每 0.2 秒
問地形圖有沒有隔地形、隔地形就沿繞路點貼臉、**邊走邊打**、零傷害 3 秒貼身
繞打、沒進展 15 秒換一隻…），⛔ **不 import 那邊的函式與數字** —— 兩邊要能
各自改，改一邊不會拖垮另一邊。三個刻意的差別：
  · **沒有任何冷卻／黑名單**（掛機的 `_killed`、`_cool_unreach` 全部不抄）：
    放棄就換一隻（有別隻時先挑別隻），沒別隻就再問同一隻。
  · **血量歸零就當作打死了**（使用者 2026-09-05）：副本裡的柱子死掉屍體會留
    一段時間、動畫狀態不變 'Dead'，只看狀態會一直對屍體出手。所以挑目標
    （`_live_monsters`／`_candidates`）與正在打的那隻（`_fight`）都多看一眼
    實體 +0x288 的血量（`entity.read_live_hp`），恰好 0 ＝ 屍體、不挑不打；
    −1（沒交戰）照打。⛔ 不是黑名單 —— 每拍當場重讀，沒有記任何 eid。
  · 直線超過 `MAX_CHASE`(30) 的怪整個不看（不追、也不算在「殺光了沒」裡）。
  · 走不走得到用本頁的 `_can_reach`（薄牆規則，見下），不用掛機的 nearest_open 放寬。

## ⚠⚠ 副本＝好幾塊互不相通的地方拼起來，靠傳點連（使用者 2026-09-02 提醒）

> 「副本很容易掃到另一個地區的怪物無法到達這點要注意」
> 「副本是多個地圖組合再一起靠傳點連接這點要注意」

實測吞噬之間 1 一張圖切出 **7 塊互不相通的區域**（最厚的牆超過 5 格）。
怪物是**跟著玩家串流進來的**，隔壁區的怪照樣會出現在掃描結果裡 —— 沒有處理
的話會變成：挑最近的一隻 → 尋路說到不了 → 換一隻 → 又挑到同一隻 →
**永遠在挑目標，腳本一步都不會前進**（就是使用者說的「跟自動戰鬥互卡」）。

所以出手之前先問地形圖：**只打「站在我這一區」的怪**（`Grid.reachable()`
泛洪一次 6ms）。打不到的怪不算數 —— 不然「都沒怪才算到點位」這條規矩
會被隔壁區的怪永遠卡住。

⚠⚠ **不能用距離判**（使用者 2026-09-02 特別提醒）：

> 「怪物可能跟我只有隔一牆但距離只有 3~2 格，這種的要注意排除」

判的是「它站的那一格在不在我這一區」。而且怪站在**可走格**上卻不在我這一區
時就直接排除，⛔ 不可以再看它旁邊那一圈 —— 隔一格薄牆的怪，鄰居格很容易
剛好落在我這一區，那樣就會把隔牆的怪誤判成打得到（見 `_can_reach`）。
⚠ 讀不到地形圖時**不過濾**（安全退化：寧可多打，不要因為讀不到就不出手）。

⛔ **不記黑名單**（使用者同日明令）：

> 「不要加黑名單，一直問能不能走到他那邊就好，
>   都問一輪沒有怪物能走到就算殺光」

門會解開，這一秒走不到的怪下一秒可能就打得到。所以每一拍重新問一輪，
打不到就換一隻（只有還有別隻時才跳過剛放棄的那隻），並立刻重讀地形。
⛔ 而且**沒有任何「幾秒沒到就壞掉」的計時**（使用者 2026-09-02 定案）——
慢就慢、卡住就一直試，出口是使用者自己取消勾選。

## 到點位跟自動戰鬥的先後（使用者 2026-09-02 定案）

> 「到點位不要跟自動戰鬥互卡，在殺怪物就不要跑點位，都沒怪到點位才算到」
> 「跑路徑的時候要把周圍怪物殺光（不包含無法到達的），要周圍完全沒有
>   怪物才可以繼續跑」

一拍只做一件事：**走得到的怪還有一隻就先打**（腳本完全不動），
問了一輪一隻都走不到才跑腳本。所以「到點位」這件事天生就發生在
「周圍走得到的怪都清光」的時候。

⚠ 也因為這樣，**腳本不需要「清光周圍的怪」這種步驟**（製作頁的按鈕已經
  拿掉，使用者 2026-09-02 指出它是多餘的）。舊腳本裡的 `clear` 照樣跑，
  行為只是「原地確認 CLEAR_SETTLE 秒都沒怪」。

## 三種一定要大聲停下來的情況（CLAUDE.md：只准大聲停用或安全退化）

1. **腳本跟眼前這張圖對不上**（場景編號或地圖指紋）→ 取消勾選並說原因。
   官方改過地圖還照舊腳本盲走，就是「安靜地做錯事」。
2. **對話那一步找不到對應外觀的物件** → 停下來。⛔ 不可以「就近點一個」——
   點錯東西比不點危險。
3. **讀不到狀態物件／玩家物件** → 這一拍什麼都不做（不寫記憶體、不出手）。
"""
from __future__ import annotations

import math
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidgetAction,
)

from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.core.notifier import Notifier
from app.game import (dungeon, entity, itemname, jumpmap, locate, mapobj,
                      move, navigate, player, portal, produce, quickbar, robot,
                      scene, scenery, sell, skills, supply, talkwnd, team,
                      terrain)
from app.tabs.base_tab import GROUP_AUTO, BaseTab
from app.tabs.farm_tab import (_NOTIFY_PAGES, DEFAULT_KEY, KeyWorker,
                               MODE_PACKET, ScanWorker, SKILL_KEYS,
                               TargetWorker)

TICK_MS = 100
# 掃描節奏。★ 使用者 2026-09-02：「趕路掃描改成可接受最快，這個很糟糕，
#   不管是打怪還是刷副本」——**兩檔都用掛機頁驗過的 0.15 秒**
#   （farm_tab.REFRESH_GAP：掃一次 35ms、執行緒跑 LowPriority、不影響畫面，
#   本來就是為了搶怪才從 0.3 收到 0.15 的）。趕路慢半拍＝走過去才打怪。
SCAN_FAST = SCAN_SLOW = 0.15
# 走到腳本點位算「到了」的容忍半徑（格）。
ARRIVE = 1.8
# ⚠⚠ `navigate.ARRIVE` 是 3.0 —— **尋路器在 3 格以內就什麼都不做**。
#   我們的門檻比它嚴（點位 1.8、傳點 2.5），中間那一段沒人負責，就會變成
#   「說還有 2.2 格卻不動」（使用者 2026-09-02 實遇，手動走一步才過）。
#   那一段改成直接送走路（`walk_exact`，不尋路）——就是 move.walk_near 檔頭
#   寫的「站在 2.2 格卡 8.2 秒」同一個老坑。
NAV_DEAD = navigate.ARRIVE
# 對話那一步要先靠多近才點（格）。
# ⚠⚠ 2026-09-02 使用者實遇：本來是 3.0，剛好等於 `navigate.ARRIVE` ——
#   結果站在**上一個點位**（離對話點 2.4 格）就開始點，人根本沒走過去，
#   然後「在上個點位一直打對話」。⛔ 太遠發互動包＝人沒到、對話開不起來
#   （補給點 NPC 那邊踩過同一個坑，見 supply 的「先走到位才發互動包」）。
#   → 收緊到 1.8，最後那一段用 `walk_near` 自己走（尋路器 3 格內不動）。
TALK_NEAR = 1.8
# 走到對話點時要留幾格（物件常常站在不可走的格上，不能走到它身上）。
TALK_KEEP = 1.2
# 腳本裡的座標跟現場物件對得起來的最大誤差（格）。
PROP_TOL = 3.0
# 送完一個對話動作之後等多久再看下一頁。
# ★ 對話動作之間的節奏（點→選項→確定）。使用者 2026-09-03：「把間隔直接刪掉，
#   不給輸入；預設 0.5；不會跟使用者說」→ 製作頁的欄位拿掉、腳本裡舊的 gap
#   一律忽略，就用這一個值。
MENU_GAP = 0.5
# 按了確定之後連續這麼多輪都沒換頁 ＝ 這段對話走完了（那些全域關掉還會
# 留著，只能靠「不再變化」判結束，見 talkwnd.page 的說明）。
TALK_SETTLE = 2
# 按了確定、視窗還在 → 隔多久補送一次（[[confirm-and-resend]]：送出去
# 不代表做到了；補送確定是安全的，翻頁了就是翻頁）。
CLOSE_RETRY = 1.5
# ★★ 2026-09-03 使用者實機卡死：「按確定 → 視窗還在 → 再按」**永遠出不來**。
#   現場驗屍（黑狐 遺落之地分流6）：畫面上**根本沒有對話框**，但
#   `WND_MESSAGE` 還指著一個 id 對得上的視窗物件、遊戲自己的 `ismessageend`
#   也回「還沒結束」（那個 byte [視窗+0x148] ＝ 0）——**殘留狀態**，
#   來源是之前跟 NPC 講過話沒把對話框 destroy 掉（見 supply.leave_npc）。
#   → 按了這麼多次確定、對話頁一格都沒變，就當它是殘留：destroy 掉、當結束。
CLOSE_GIVEUP = 6
# ★★ 2026-09-03 「確定」改成跟遊戲的確定鈕一樣＝messageclose＋**destroy**
#   （見 talkwnd.close_page）。後果：按完確定視窗**當場**不見；如果伺服器接著
#   還有下一頁，它會過一小段時間（一趟來回）用 ShowMessageWnd **重新建一個**。
#   所以「視窗不見了」在剛按完確定的那一小段時間**不等於對話結束** —— 要等
#   CLOSE_GRACE 秒沒有新視窗出現，才算真的走完（不然過場頁後面還有選項的
#   腳本會被誤判成「對話關掉了但選項沒送到」而停機）。
CLOSE_GRACE = 1.6
# 「有沒有視窗」要**叫進遊戲**問（call_sync 會等它做完），
# 所以答案快取這麼久，⛔ 不要每拍問。
WND_TTL = 0.3
# ★★ 點下去之後這麼久都沒有任何對話反應 → **再點一次**（使用者 2026-09-02
#   回報「最後一個石頭雕像點不到」）。點一次就不管是不對的：遊戲是「自己走
#   過去才開對話」，路上被怪打斷、被人擋住、剛好在走都會讓那一下落空 ——
#   跟補給點 NPC 那套「沒開就再點」同一個道理（見 supply 的 DIALOG_* 說明）。
# ⚠ 使用者 2026-09-02：「對話沒有就沒有，等 6 秒幹嘛，就一直試一直動就好」
#   —— 本來 6 秒才重試一次，等得太久。收到 1 秒。
#   ⚠ 安全性不變：底下那條「**沒有更靠近才累加**」還在，所以遊戲正在自己
#     走過去的期間**一次都不會插手**，收快的只有「真的停住不動」那種。
CLICK_RETRY = 1.0
# ⚠⚠ 「站穩了才點」最多等這麼久，超過就照點（**不是逾時停機，是照樣做**）。
#   出處 [[self-supply-buy]] 的老坑：人擠人／被推的時候 `is_walking` 會**恆為
#   True**，「等停穩」那道閘就永遠不會過 —— 看起來就是「站在它旁邊卻不點」。
STILL_WAIT = 1.5
# ⚠⚠ 但「點了沒反應」的計時**只在人站著沒動**時才累加：點下去（0x05）之後
#   **遊戲會自己走過去**才開對話（製作頁的「點點看」能成功就是靠這個）。
#   人正在往它靠近的期間如果我們去重點／重下走路指令，就會把遊戲那趟
#   自動接近打斷 —— 使用者 2026-09-02 回報「設定時點得到、跑腳本點不到、
#   滑鼠點也可以」的差別就在這裡：製作頁點完沒有別人再叫它走路。
#   只要「離那個物件又更近了」就把計時歸零（＝正在進行中，別插手）。
CLICK_PROGRESS = 0.5       # 離目標又近了這麼多格就算有進展
# ★★ 重點之前先**往那個物件靠上去**，一次比一次近，最後直接穿過去
#   （使用者 2026-09-02：「如果點了沒反應要調整位置往對話物件靠上去」）。
#   ⚠ 這是補給點 NPC 驗過的招（見 memory self-supply-buy：「確認沒開就往
#     NPC 身上靠甚至穿過；站著點有一半機率白站，先動腳再點」）——
#   站著不動一直重點是沒有用的。
NUDGE_KEEP = (1.2, 0.6, 0.0)
# 收工前要「連續這麼久都掃不到怪」才算真的沒怪了。
# ⚠ 實體是跟著玩家串流進來的，一拍掃不到不代表沒有。
CLEAR_SETTLE = 3.0
# ★★★ 全自動循環（使用者 2026-09-03 定案）：
#   組隊 → 刷（飛／撞入口／跑腳本）→ 腳本跑完且周圍沒怪 → **回程補給**（跟掛機
#   同一套 supply.run_full_supply）→ 趴趴GO 回離入口最近的傳送點 → 退組再組隊 → 循環。
#   「從第幾步開始」有選 → **只跑單輪**（不組隊、不補給、跑完就停）。
#   自動組隊兩種：綁定分身（刷副本這隻當隊長、均分、分身自動同意；分身只要在隊伍
#   裡就好，人在哪不管）／遊戲自動組隊（遊戲裡自己設定，我們只做「退組 → 等隊伍
#   名單出現人」）。
PARTY_MODES = (("none", "不組隊"), ("bind", "綁定分身"), ("auto", "遊戲自動組隊"))
LEAVE_GAP = 1.0            # 退組沒清空就每隔這麼久再送一次
INVITE_GAP = 2.0           # 邀請沒進隊就每隔這麼久再邀一次
JOIN_GAP = 0.5             # 分身每隔這麼久按一次「同意」
TEAM_NOTE = 3.0            # 等組隊時狀態列多久刷一次
# ---------------------------------------------------------------------------
# 打怪流程的數字 —— **從掛機頁抄一份**（使用者 2026-09-04：「複製一份幾乎一樣的
# 不要共用」）。⛔ 不要改成 import farm_tab 的：兩邊要能各自調。
# 每一個的來龍去脈見 farm_tab 同名常數的說明（實測數字都在那邊）。
# ---------------------------------------------------------------------------
# ★★★ 直線超過這麼多格的怪整個不看（使用者 2026-09-04：「超過 30 格要跳過」）。
#   病灶：往怪物走 → 走到一半怪掉出遊戲的串流範圍（掃不到）→ 改去點位 →
#   一靠近又掃到 → 再追 …… 無限輪迴。9/4 純讀：開闊地圖掃得到 37 格外的怪，30 安全。
MAX_CHASE = 30.0
WALK_GAP = 0.4                  # 貼身微調多久送一次移動
WALK_GAP_FAR = 0.30             # 趕路（離目標 > FAR_ENOUGH）時的冷卻
FAR_ENOUGH = 6.0
WALK_SLACK = 1.0                # 超過停留距離再多這麼多格才走（不然打一打又往前一格）
PATH_GAP = 0.2                  # 重算「跟目標之間有沒有地形」的最短間隔
PATH_BUDGET = 20.0              # 規劃路徑最多佔 1/20 的時間
PATH_GAP_MAX = 1.0
UNREACH_HITS = 3                # 尋路連續這麼多次算不出 → 這隻走不到，換一隻
STUCK_SECS = 10.0               # 沒交戰：沒掉血也沒前進這麼久 → 換一隻
STUCK_ENGAGED = 15.0            # 交戰中（打得到、選定也送了）要等這麼久才放棄
STUCK_EPS = 4.0                 # 離錨點淨位移超過這麼多格才算「真的在走」（撞牆抖 0.5）
PUSH_IN_SECS = 3.0              # 站定零傷害這麼久 → 貼身繞打（技能被地形擋線）
SWITCH_GAIN = 3.0               # 新目標路徑要短這麼多格才值得換（防乒乓）
SWITCH_GAP = 1.0
ATTACK_PACKET_RANGE = 12.0      # 攻擊封包真正有效的最遠距離（遊戲固定值）
HANDOFF_RANGE = 12.0            # 交棒：走到這麼近就叫快捷鍵讓遊戲自己走過去
HANDOFF_WAIT = 3.0              # 交棒後這麼久還沒接戰 → 收回來自己走
NO_PATH_NEED = 3.0              # 比這個近就不算走不到、也不算擋線
NEAR_WALK = 4.0                 # 比這個近就不尋路，直接朝目標走（walk_near）
MELEE_RANGE = 2.0               # 隔地形／貼身繞打時走到這麼近
FULL_HUNT_GAP = 3.0             # 沒目標時多久要求一次全掃當保險
GONE_SCANS = 2                  # 目標連續這麼多拍不在掃描裡（物件也沒了）才算沒了
# ⛔⛔ **沒有任何冷卻／黑名單**（使用者 2026-09-02、2026-09-04 兩次明令）：
#   掛機頁的 KILL_MEMORY／NOHP_MEMORY／UNREACH_MEMORY 那一整套這裡都沒有。
#   放棄一隻＝換一隻（有別隻時先挑別隻，`_last_gave_up`），沒別隻就再問同一隻。
# ★★ 順移判定（傳點那一步的完成訊號，使用者 2026-09-02 定案）：
#   「人被傳走不會換地圖，有順移就算吧，有時候傳點之間也很短」
#   —— 所以不能用「離傳點多遠」判，要用**一拍之間跳了多少**：
#   跑步一拍（0.1 秒）最多動 0.6 格，跳這麼多格只可能是被搬過去的。
JUMP_TILES = 3.0
# 兩次取樣隔太久就分不出是走的還是傳的（畫面卡一下就會誤判）→ 這一拍不判。
JUMP_MAX_GAP = 0.35
# 傳到的位置跟腳本記的出口差這麼多格就當「傳到別的地方」，大聲停下。
LAND_TOL = 8.0
# ★★ 順移要「從傳點上跳走」才算是傳點搬的（2026-09-05 使用者實機：無限塔第 41／52
#   步人還在 10 多格外走過去，伺服器把位置拉回幾格（[[whitefox-rollback]] 那種丟包
#   修正）→ 一拍跳 ≥3 格 → 被當成「傳點把人送到別的地方」大聲停下，其實根本沒踩到傳點）。
#   → 跳之前站的位置離腳本記的傳點要在這個距離內；不在＝不是傳點搬的，不算、繼續走。
#   ⚠ 這不是「離傳點多遠判完成」（那條 ⛔）：判完成仍看一拍跳了多少；這裡只擋
#     「人根本不在傳點上」的跳動。落點對得上 `land` 的一律算（觸發範圍比記的寬也吃得到）。
PORTAL_FROM = 6.0
# ★★ 地形圖多久重讀一次（秒）。使用者 2026-09-02：
#   「地圖之間有可能會用牆壁隔開，解謎之後會打開又會變成聯通，
#     所以記憶體地圖要即時刷新」
#   —— 機關開門會**就地改掉可走格**，而 terrain.Cache 的身分只看
#   (地圖物件, 寬高, 場景編號, 列指標)，門開了那四樣全都沒變 → 不重讀就
#   一直拿舊的牆算路。一次重讀 7~8ms，兩秒一次完全不痛。
GRID_REFRESH = 2.0
# ⛔ **沒有黑名單**（使用者 2026-09-02 明令）：
#   「不要加黑名單，一直問能不能走到他那邊就好，
#     都問一輪沒有怪物能走到就算殺光」
#   —— 因為門會解開，這一秒走不到的怪下一秒可能就打得到了。
#   打不到就換一隻（有別隻的話），並**立刻重問一次地形**，不記時間。
# 重要提示在狀態列上要停留幾秒（不然同一拍的走路訊息會馬上蓋掉）。
NOTICE_SECS = 6.0
# ★ 通知（使用者 2026-09-05：「自動刷副本死掉或出問題也要通知，跟自動掛機一樣」）。
#   · 停機原因開頭是這幾個符號＝「出問題」→ 送通知；「已停止」「換了腳本」那種
#     使用者自己按的不送。⚠ 只有**開跑之後**的停機才通知（`_started`）：
#     開跑前的檢查（沒選分身、腳本讀不進來）人就在電腦前按，響警報是吵人。
#   · 通知那一列的設定**跟自動掛機共用同一份**（`farm.notify_on` 等，見
#     farm_tab._nkey）—— 使用者只要填一次 Telegram 群組。
PROBLEM_MARKS = ("⛔", "⚠", "☠")
NOTIFY_PREFIX = "farm"
# ★ 角色死亡：HP ≤ 0（跟掛機頁同一個訊號 `player.read().hp`）。每 DEATH_POLL 秒
#   讀一次、連續 DEATH_HITS 次才算（單次讀到 0 可能是物件搬家瞬間的垃圾值）。
DEATH_POLL = 0.5
DEATH_HITS = 2
# 算出來的「我這一區」小於這麼多格就當錨點抓錯了（碎片區）→ 這一輪不篩選。
# ⚠ 跟 dungeon.rooms() 的 min_cells 同一個道理：實測這張圖有 9/4/2 格的
#   零星角落，錨在那上面會把整張圖的怪都判成走不到。
MIN_REGION = 20
# ★★ 傳點站上去卻沒被搬走時的補救（使用者 2026-09-02 定案）：
#   「偵測走過去然後要有突然位移或換圖，如果失敗就在傳送點每 5 秒送一次，
#     如果 3 分鐘都這樣就結束跳通知警告使用者」
# ★ 人在別的圖（天使學園之類）→ 先用**天使趴趴GO**飛到入口那張圖
#   （使用者 2026-09-02 問：「我在天使學園開自動刷副本會用趴趴GO飛過去嗎」）。
#   實測表裡 `地底廣場(LV70~80)副本進入點` 就是一個合法目的地。
#   ⚠ 送出到人真的過去約 1 秒；這麼久還沒到就再送一次（跟撞入口一樣無限重試）。
FLY_RESEND = 8.0
PORTAL_NEAR = 2.5          # 站到這麼近就算「已經在傳點上」，開始補送
# ★★ 使用者 2026-09-02：「進副本不是站在傳送口等傳送，而是要一直打進傳送點
#   封包」——所以站上去之後**主動送 0x0D**（`portal.enter`，就是遊戲自己
#   踩上去會送的那一包），不是站著等。
#   ⚠ 遊戲那支有去重欄（物件 +0x208 記著上一個踩上來的人），所以站著不動
#     它自己**永遠不會送第二次**；我們主動送就沒有這個限制。
# ⚠ 使用者 2026-09-02：「打太快了，5 秒打一次封包就好」——1 秒太密。
PORTAL_POKE = 5.0          # 每幾秒對傳點主動送一次 0x0D
# ⛔ 沒有「撐多久就放棄」這種東西（使用者 2026-09-02：「不會有幾秒沒到就
#   壞掉，那個拔掉」）—— 傳點過不去就一直打，出口是取消勾選。


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


_SQRT2 = math.sqrt(2.0)


def _pick(items):
    """從掃到的東西裡挑「腳本說的那一個」＝**離記的位置最近的那一個**。

    ★★ 使用者 2026-09-02 定案：**不比外觀**。
      機關被啟動過之後外觀編號會換（實測遺落之地 60335「靜態-廢棄機器人2」
      → 60301「門開關火不給點」，同一格、同一個東西），比外觀就會變成
      「找不到」而整趟停掉。場景物件不會移動，**位置**才是它的身分。
      腳本裡的 `model` 只留著給人看（清單上顯示名字用）。
    ⚠ 但**看不見的場景標記點（TAG，SP_ATTRIB_HIDE）要排掉** —— 那些是
      伺服器用的位置標記，一站就掃到一堆，點它們沒有意義。
    `scenery.nearby` / `portal.nearby` 給的已經是**由近到遠**，取第一個就好。
    """
    return [x for x in items if not mapobj.hidden(x.model)][:1]


class DungeonTab(BaseTab):
    TAB_TITLE = "自動刷副本"
    GROUP = GROUP_AUTO
    ORDER = 6                        # 排在副本腳本製作（6）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._hwnds: dict[int, int] = {}
        self._titles: dict[int, str] = {}   # pid → 視窗標題（看分流用）
        self._mover = None
        self._pid = None
        self._sc = None
        self._script = None
        self._keys = None            # KeyWorker
        self._atk = None             # TargetWorker
        self._loading = False        # 正在把設定讀回畫面（這期間不要回存）
        self._qb_sc = None           # 技能鍵標名字用的 Reader（跟著分身換）
        self._qb_ui = None
        self._scan = ScanWorker()
        self._scan.done.connect(self._on_scan)
        self._scan.start()
        # ⚠ 尋路跟我們自己**共用同一份**地形快取：兩份的話門開了只有一邊
        #   會跟上，另一邊拿舊的牆算路（使用者 2026-09-02 提醒門會開關）。
        self._maps = terrain.Cache()
        self._nav = navigate.Navigator(self._maps)
        self._reset_run()

        root = QVBoxLayout(self)

        # 通知列（最上面，跟自動掛機那一頁同一套、同一份設定）
        nbar = QHBoxLayout()
        self.notify_cb = QCheckBox("啟用通知")
        self.notify_cb.setChecked(True)
        self.notify_cb.setToolTip(
            "通知總開關：角色死亡、刷副本出狀況停下來時通知。\n"
            "這一列跟自動掛機那一頁是同一份設定，改一邊全部跟著改。\n"
            "關掉只是不通知，該停還是會停。")
        self.notify_cb.toggled.connect(self._save_notify)
        nbar.addWidget(self.notify_cb)
        nbar.addSpacing(10)
        nbar.addWidget(QLabel("通知方式"))
        self.rb_sound = QRadioButton("音效警報")
        self.rb_tg = QRadioButton("Telegram")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_sound)
        grp.addButton(self.rb_tg)
        self.rb_sound.setChecked(True)
        self.rb_sound.toggled.connect(self._save_notify)
        self.rb_tg.toggled.connect(self._save_notify)
        nbar.addWidget(self.rb_sound)
        nbar.addWidget(self.rb_tg)
        self.tg_id = QLineEdit()
        self.tg_id.setPlaceholderText("Telegram 群組/房間 ID")
        self.tg_id.setFixedWidth(220)
        self.tg_id.editingFinished.connect(self._save_notify)
        nbar.addWidget(self.tg_id)
        self.test_btn = QPushButton("測試通知")
        self.test_btn.setToolTip("立刻送一則測試通知，確認設定會不會通。")
        self.test_btn.clicked.connect(
            lambda: self.notify("這是一則測試通知。"))
        nbar.addWidget(self.test_btn)
        nbar.addStretch(1)
        root.addLayout(nbar)
        self._prefix = NOTIFY_PREFIX
        self._notifier = Notifier(
            self, "⚠ 自動刷副本警報",
            lambda: ("telegram" if self.rb_tg.isChecked() else "sound",
                     self.tg_id.text()))
        _NOTIFY_PAGES.add(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        b = QPushButton("重新整理")
        b.setToolTip("重新列出目前開著的遊戲分身。")
        b.clicked.connect(lambda: self.reload_instances(force_names=True))
        bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        sbar = QHBoxLayout()
        sbar.addWidget(QLabel("腳本"))
        self.files = QComboBox()
        self.files.setFixedWidth(240)
        self.files.setToolTip("在「副本腳本製作」那一頁做出來的腳本。")
        self.files.currentIndexChanged.connect(self._on_file_changed)
        sbar.addWidget(self.files)
        b = QPushButton("重新整理")
        b.setToolTip("重新讀取腳本資料夾。")
        b.clicked.connect(self._reload_files)
        sbar.addWidget(b)
        sbar.addStretch(1)
        root.addLayout(sbar)

        g = QGroupBox("攻擊（跟自動掛機同一套）")
        a = QHBoxLayout(g)
        a.addWidget(QLabel("技能鍵"))
        # 跟掛機同樣的做法：勾幾個 F 鍵，照 F1→F12 輪流放；鍵上放什麼直讀
        # 快捷欄（空格／物品格自動略過）。⚠ 勾選框要包在 QWidgetAction 裡，
        # 不然點一下選單就關了，沒辦法一次勾多個。
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
        for cb, _vk, _lab in self._key_cbs:
            cb.toggled.connect(self._keys_changed)
        # ★ 點開選單的當下把每個鍵標上「現在放什麼」：F1（電擊術Ⅳ）
        #   —— 跟自動掛機那頁同一套（使用者 2026-09-02：「怎麼沒寫技能名稱，
        #   不能只有 F1 F2 這樣」）。純讀快捷欄，開著跑也沒差。
        km.aboutToShow.connect(self._label_keys)
        self.key_btn.setMenu(km)
        self._sync_key_btn()
        a.addWidget(self.key_btn)
        a.addStretch(1)
        root.addWidget(g)

        rbar = QHBoxLayout()
        # ★ 使用者 2026-09-02：「改一下自動刷副本，我可以選擇從哪開始」
        rbar.addWidget(QLabel("從第"))
        self.start_box = QComboBox()
        self.start_box.setFixedWidth(210)
        self.start_box.setToolTip(
            "從腳本的第幾步開始跑（做腳本／卡住重跑時很方便）。\n"
            "⚠ 前面的步驟會直接跳過 —— 該開的門沒開就會走不到。")
        self.start_box.currentIndexChanged.connect(self._save_settings)
        rbar.addWidget(self.start_box)
        rbar.addWidget(QLabel("步開始"))
        rbar.addSpacing(12)
        self.run_cb = QCheckBox("自動刷副本")
        self.run_cb.setToolTip(
            "勾起來就照腳本開始跑：路上有怪先清光，再去下一個點位。\n"
            "整份腳本跑完而且周圍沒有怪 = 這一趟結束。")
        self.run_cb.toggled.connect(self._on_run_toggled)
        rbar.addWidget(self.run_cb)
        rbar.addStretch(1)
        self.prog = QLabel("－")
        rbar.addWidget(self.prog)
        root.addLayout(rbar)

        pbar = QHBoxLayout()
        pbar.addWidget(QLabel("自動組隊"))
        self.party_box = QComboBox()
        for data, label in PARTY_MODES:
            self.party_box.addItem(label, data)
        self.party_box.setToolTip(
            "綁定分身：開跑先把你跟綁定的分身都退組，再由你邀請它（均分）；\n"
            "遊戲自動組隊：先退組，等遊戲自己配到隊伍才開跑（要先在遊戲裡打開）。\n"
            "每一趟刷完 → 回程補給 → 趴趴GO回入口 → 退組再組隊 → 循環。")
        self.party_box.currentIndexChanged.connect(self._on_party_changed)
        pbar.addWidget(self.party_box)
        pbar.addWidget(QLabel("綁定分身"))
        self.partner_box = QComboBox()
        self.partner_box.setFixedWidth(240)
        self.partner_box.setToolTip("「綁定分身」模式要組的那一台（要跟你在同一個分流）。")
        self.partner_box.currentIndexChanged.connect(self._save_settings)
        pbar.addWidget(self.partner_box)
        pbar.addStretch(1)
        root.addLayout(pbar)

        self.steps = QListWidget()
        self.steps.setSelectionMode(QListWidget.NoSelection)
        root.addWidget(self.steps, 1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self._notifier.failed.connect(self.status.setText)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)
        self._reload_files()
        self._load_settings()
        self._load_notify()

    # ------------------------------------------------------------------
    # 分身與腳本
    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()
        self._reload_files()
        self._load_settings()

    def reload_instances(self, force_names: bool = False) -> None:
        self._stop(quiet=True)
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._hwnds.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                            # noqa: BLE001
                continue
            try:
                locate.warm(sc)
            except Exception:                            # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            self._scanners[w.pid] = sc
            self._hwnds[w.pid] = w.hwnd
            self._titles[w.pid] = w.title
            self.who.addItem(
                f"{preload.name_of(w.pid, sc, acc, force=force_names)}"
                f"（{acc}）", w.pid)
        # ★ 挑回上次用的那一台（使用者 2026-09-02：設定要記在使用者那邊）
        last = str(config.get("dungeon.last_account", "") or "")
        if last:
            self.who.blockSignals(True)
            for i in range(self.who.count()):
                if f"（{last}）" in self.who.itemText(i):
                    self.who.setCurrentIndex(i)
                    break
            self.who.blockSignals(False)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        self._refresh_partner_box()
        self._load_settings()

    # ------------------------------------------------------------------
    # 設定（存在使用者那邊的 config.json，使用者 2026-09-02 要求）
    # ------------------------------------------------------------------
    def _key(self, field: str) -> str:
        """設定鍵。⚠ 帶帳號 —— 每個分身各自記自己的腳本／技能鍵／起始步驟。"""
        return f"dungeon.{self._account()}.{field}"

    def _account(self) -> str:
        """目前選的分身帳號（拿不到就用 'default'，設定照樣存得住）。"""
        txt = self.who.currentText() or ""
        if "（" in txt and txt.endswith("）"):
            return txt[txt.rindex("（") + 1:-1]
        return txt or "default"

    def _save_settings(self) -> None:
        """把這一頁的設定寫回 config。⚠ `config.set` 不寫檔，要接 `save()`
        （[[config-set-needs-save]]，已經復發兩次）。"""
        if self._loading:
            return
        config.set(self._key("script"), self.files.currentText())
        config.set(self._key("vks"), self._picked_keys())
        config.set(self._key("start"), int(self.start_box.currentIndex()))
        config.set(self._key("party"), self.party_box.currentData() or "none")
        config.set(self._key("partner"),
                   self.partner_box.currentText().split("（")[-1].rstrip("）")
                   if self.partner_box.currentIndex() >= 0 else "")
        config.set("dungeon.last_account", self._account())
        config.save()

    def _load_settings(self) -> None:
        """把設定讀回畫面。⚠ 讀的期間要擋住 `_save_settings`（不然邊讀邊寫
        會把還沒讀到的欄位用預設值蓋掉）。"""
        self._loading = True
        try:
            name = str(config.get(self._key("script"), "") or "")
            i = self.files.findText(name)
            if i >= 0:
                self.files.setCurrentIndex(i)
            self._refresh_start_box()
            mode = str(config.get(self._key("party"), "none") or "none")
            j = self.party_box.findData(mode)
            self.party_box.setCurrentIndex(j if j >= 0 else 0)
            self._refresh_partner_box()
            want = str(config.get(self._key("partner"), "") or "")
            if want:
                for k in range(self.partner_box.count()):
                    if f"（{want}）" in self.partner_box.itemText(k):
                        self.partner_box.setCurrentIndex(k)
                        break
            vks = config.get(self._key("vks"), None)
            if isinstance(vks, list) and vks:
                for cb, vk, _lab in self._key_cbs:
                    cb.setChecked(vk in vks)
                self._sync_key_btn()
            st = int(config.get(self._key("start"), 0) or 0)
            if 0 <= st < self.start_box.count():
                self.start_box.setCurrentIndex(st)
        finally:
            self._loading = False

    def _refresh_start_box(self) -> None:
        """把「從第幾步開始」的下拉重建成目前這份腳本的步驟。

        ⚠ 重建會觸發 currentIndexChanged → 先擋訊號，不然會把剛讀回來的
          設定又覆蓋掉（[[qt-ui-pitfalls]] 那條「高頻清單不可 clear()」的
          鄰居坑：重建清單一定要擋訊號）。
        """
        path = self.files.currentData()
        steps = []
        sc = None
        if path:
            from pathlib import Path
            sc, _why = dungeon.load(Path(path))
            steps = sc.steps if sc else []
        # ★ 選了腳本就把流程列在下面，不必等按「自動刷副本」（使用者 2026-09-03）。
        #   跑的時候不動（那時清單是進度，由 _refresh_steps 自己刷）。
        if not self.run_cb.isChecked():
            self._script = sc
            self._i = 0
            self._refresh_steps()
        keep = self.start_box.currentIndex()
        self.start_box.blockSignals(True)
        self.start_box.clear()
        for i, st in enumerate(steps):
            self.start_box.addItem(f"{i + 1}. {dungeon.describe(st)}"[:60], i)
        if not steps:
            self.start_box.addItem("1", 0)
        if 0 <= keep < self.start_box.count():
            self.start_box.setCurrentIndex(keep)
        self.start_box.blockSignals(False)

    def _on_file_changed(self) -> None:
        self._stop("換了腳本")
        self._refresh_start_box()
        self._save_settings()

    def _on_who_changed(self) -> None:
        # 換分身＝換一個人的設定（腳本／技能鍵／起始步驟各記各的）
        self._stop("換了分身")
        self._load_settings()

    def _on_party_changed(self) -> None:
        self.partner_box.setEnabled(self.party_box.currentData() == "bind")
        self._save_settings()

    def _refresh_partner_box(self) -> None:
        """綁定分身的候選＝其他開著的分身（排掉自己）。"""
        keep = self.partner_box.currentText()
        me = self.who.currentData()
        self.partner_box.blockSignals(True)
        self.partner_box.clear()
        for i in range(self.who.count()):
            pid = self.who.itemData(i)
            if pid != me:
                self.partner_box.addItem(self.who.itemText(i), pid)
        k = self.partner_box.findText(keep)
        if k >= 0:
            self.partner_box.setCurrentIndex(k)
        self.partner_box.blockSignals(False)
        self.partner_box.setEnabled(self.party_box.currentData() == "bind")

    def _reload_files(self) -> None:
        keep = self.files.currentText()
        self.files.blockSignals(True)
        self.files.clear()
        for p in dungeon.list_scripts():
            self.files.addItem(p.stem, str(p))
        i = self.files.findText(keep)
        if i >= 0:
            self.files.setCurrentIndex(i)
        self.files.blockSignals(False)
        if not self.files.count():
            self.status.setText(
                "腳本資料夾是空的 —— 先去「副本腳本製作」做一份")

    def _keys_changed(self) -> None:
        self._sync_key_btn()
        if self._keys is not None:
            self._keys.vks = self._picked_keys()
        self._save_settings()

    def _picked_keys(self) -> list[int]:
        vks = [vk for cb, vk, _lab in self._key_cbs if cb.isChecked()]
        return vks or [DEFAULT_KEY]

    def _sync_key_btn(self) -> None:
        """按鈕字樣＝勾了哪些鍵；勾太多就縮寫，別把整條列撐爆。"""
        labels = [lab for cb, _vk, lab in self._key_cbs if cb.isChecked()]
        if not labels:
            self.key_btn.setText("選技能鍵")
        elif len(labels) <= 4:
            self.key_btn.setText("、".join(labels))
        else:
            self.key_btn.setText(f"{labels[0]} 等 {len(labels)} 鍵")

    def _label_keys(self) -> None:
        """把選單上的 F1~F12 標成「F1（電擊術Ⅳ）」—— 跟掛機頁同一套。

        技能格 → F1（電擊術Ⅳ）、物品格 → F5（物品：高效紅藥水）、
        空格 → F7（空）。讀不到快捷欄（還沒進遊戲／改版位移）就維持素的
        F1~F12，**不亂標**。純讀取。

        ⚠ 只改字（setText），不重建清單 —— 重建會讓順序跳動（[[qt-ui-pitfalls]]）。
        ⚠ 這一頁的分身是可以換的，所以 Reader 每次照「現在選的那台」開，
          不像掛機頁綁死一顆（換了分身還用舊的＝標到別隻角色的技能）。
        """
        sc = self._scanners.get(self.who.currentData())
        cells = None
        if sc is not None:
            if getattr(self, "_qb_sc", None) is not sc:
                self._qb_sc, self._qb_ui = sc, quickbar.Reader(sc)
            try:
                cells = quickbar.read_page(sc, self._qb_ui.page())
            except Exception:                            # noqa: BLE001
                cells = None
        for i, (cb, _vk, label) in enumerate(self._key_cbs):
            text = label
            if cells is not None and i < len(cells):
                c = cells[i]
                if c is None:
                    text = f"{label}（空）"
                elif c.is_skill:
                    nm = skills.name_of(c.value) or f"技能{c.value}"
                    # 位移／補血／buff 放進攻擊循環只會幫倒忙，標出來
                    # （循環本來就會跳過它們）
                    if not skills.is_attack(c.value):
                        nm += "・非攻擊"
                    text = f"{label}（{nm}）"
                elif c.is_item:
                    nm = itemname.of(c.value)
                    text = f"{label}（物品：{nm}）" if nm else f"{label}（物品）"
            cb.setText(text)

    # ------------------------------------------------------------------
    # 開始／停止
    # ------------------------------------------------------------------
    def _reset_run(self) -> None:
        self._started = False        # 真的開跑了沒（開跑後的停機才通知）
        self._stats = None           # 角色屬性基準（讀 HP 判死亡用）
        self._death_poll = 0.0
        self._dead_hits = 0          # HP ≤ 0 連續讀到幾次
        self._i = 0                  # 目前跑到第幾步
        self._step_t = 0.0           # 這一步跑多久了
        self._menu_i = 0             # 對話選項送到第幾個
        self._menu_t = 0.0
        self._full_req_t = 0.0       # 補救全掃的節流
        self._talk_sig = None        # 上一輪看到的對話簽章（換頁偵測）
        self._talk_same = 0          # 簽章連續幾輪沒變
        self._talk_did = ""          # 這一頁做過什麼（"opt"／"close"）
        self._talk_seen = False      # 這一步**看過**對話視窗開起來沒
        self._close_t = 0.0          # 按了確定之後等多久了
        self._close_n = 0            # 確定按了幾次還是沒換頁（見 CLOSE_GIVEUP）
        self._gone_t = 0.0           # 按了確定之後視窗不見多久了（見 CLOSE_GRACE）
        self._page_ended = None      # 按確定那一刻這頁是不是最後一頁（talkwnd.message_ended）
        self._wnd = None             # 上一次問到的「有沒有對話視窗」
        self._wnd_t = 0.0            # 那是什麼時候問的
        self._talk_base = None       # 點下去那一刻的簽章（判「有沒有點到」）
        self._click_t = 0.0          # 點了多久還沒反應（只在沒更靠近時累加）
        self._still_t = 0.0          # 等「站穩」等了多久
        self._click_best = None      # 點下去之後離那個物件最近到過幾格
        self._nudge = 0              # 往物件靠上去第幾次了
        self._clicked = False        # 這一步的物件點過了嗎
        self._wait_left = 0.0
        self._last = None            # 最近一次掃描結果
        self._scan_t = 0.0
        self._cur = None             # 正在打的怪
        self._state = None
        self._player = None
        self._empty_since = 0.0      # 連續多久掃不到怪
        self._done = False
        self._map_key = None         # 現在**應該**在哪一張圖（走過傳點可能換）
        self._pos_prev = None        # 上一拍的位置（順移偵測用）
        self._pos_t = 0.0
        self._jumped = None          # 這一拍有沒有順移：有＝跳之前站的位置 (x, y)
        self._grid_t = 0.0           # 還有多久重讀地形圖
        # ---- 全自動循環（見 PARTY_MODES 的說明）----
        self._loop = False           # 要不要循環（「從第幾步」有選＝單輪不循環）
        self._cycle = "go"           # go（飛／撞入口／跑）／supply／back／team
        self._party = "none"         # 組隊模式
        self._team_sub = ""          # leave／invite／wait
        self._team_t = 0.0           # 下一次送退組／邀請的倒數
        self._join_t = 0.0           # 分身下一次按同意的倒數
        self._team_note_t = 0.0
        self._ppid = None            # 綁定分身 pid
        self._psc = None
        self._pmover = None
        self._partner_name = ""
        self._rounds = 0             # 循環跑了幾趟
        self._supply_thread = None
        self._supply_result = None
        self._supply_progress = ""
        self._supply_gen = 0
        self._unreach_t = 0.0        # 「沒有路」連續多久了（等門開）
        self._blocked_last = False   # 上一拍是不是卡在「沒有路」
        self._notice = ""            # 要停留幾秒的提示
        self._poke_t = 0.0           # 還有多久對傳點補送一次互動
        # "fly"＝趴趴GO去入口那張圖、"enter"＝撞入口、"run"＝跑腳本
        self._phase = "run"
        self._enter_t = 0.0          # 撞入口撞多久了
        self._fly = None             # 要飛去哪個傳送點（jumpmap.Entry）
        self._fly_t = 0.0            # 還有多久重送一次趴趴GO
        self._fly_total = 0.0        # 飛了多久了（只拿來顯示）
        self._enter_acted = None     # 入口對話「已經動過」的那一頁（防重複送）
        self._notice_t = 0.0
        self._reach = None           # 「我這一區」走得到的格子（None＝沒有圖）
        self._reach_n = 0            # 上次那一區有幾格（拿來看門開了沒）
        self._grid = None            # 上次讀到的地形圖（判斷怪站的是不是可走格）
        # ---- 打怪流程（掛機頁的複本，見檔頭）：這些全是「跟目前目標綁在一起」
        #      的狀態，換目標一律走 _engage() 整組重設 ----
        self._last_gave_up = None    # 剛換掉的那一隻（有別隻時先挑別隻；⛔ 不是黑名單）
        self._me = None              # 這一拍我的位置（挑目標／算路徑用）
        self._left_out = (0, 0)      # 上一輪不打的怪：(超過 MAX_CHASE, 走不到)
        self._stuck = 0.0            # 沒掉血、也沒前進多久了
        self._anchor = None          # 卡住偵測的錨點（淨位移 > STUCK_EPS 才重設）
        self._last_hp = -1           # 上一拍看到的目標血量
        self._hurt = False           # 這隻打傷過了（打傷過的絕不換、絕不放棄走不到）
        self._push_in = False        # 零傷害 → 正在貼身繞打
        self._path_pts = -1          # 跟目標之間的路徑點數（-1 還沒算、1 直線通、>1 隔地形）
        self._line_clear = False     # 地形圖這一拍親口說直線可通
        self._no_grid = ""           # 讀不到地形圖的原因（有字＝這一拍不走位）
        self._path_t = 0.0
        self._path_gap = PATH_GAP
        self._way = []               # 隔地形時的繞路點
        self._unreach = 0            # 尋路連續算不出幾次
        self._switch_t = 0.0
        self._handoff_fail = False
        self._handoff_t = 0.0
        self._near_fail = 0
        self._near_from = None
        self._gone = 0               # 目標連續幾拍不在掃描裡
        self._walked_ok = True
        self._walk_t = 0.0
        self._why = ""               # 為什麼沒在打（狀態列）

    def _on_run_toggled(self, on: bool) -> None:
        if not on:
            self._stop("已停止")
            return
        pid = self.who.currentData()
        path = self.files.currentData()
        if pid is None:
            self._stop("先選一台分身")
            return
        if not path:
            self._stop("先選一份腳本")
            return
        from pathlib import Path
        script, why = dungeon.load(Path(path))
        if script is None:
            self._stop(f"⚠ 腳本讀不進來：{why}")
            return
        sc = self._scanners.get(int(pid))
        if sc is None:
            self._stop("這台分身不見了")
            return
        # ★★ 現在人在哪？（使用者 2026-09-02：「自動刷副本那邊就會看，
        #   如果在副本裡面會直接執行 json 開始跑；如果不在就會去撞副本傳點」）
        here = scene.map_key(scene.current_id(sc))
        ent = script.entrance or {}
        fly = None
        if here is not None and script.scene is not None and here != script.scene:
            if not ent:
                self._stop(f"⛔ 你不在「{scene.scene_name(script.scene)}」，"
                           f"這份腳本也沒記入口傳送點 —— 先進副本再開，"
                           f"或在製作頁按「這是進副本的入口」記一次。")
                return
            if here != ent.get("scene"):
                # ★ 在別的圖（天使學園之類）→ 先用天使趴趴GO飛到入口那張圖。
                ex, ey = (ent.get("to") or [None, None])[:2]
                fly = jumpmap.nearest(ent["scene"], ex, ey)
                if fly is None:
                    self._stop(
                        f"⛔ 你在「{scene.scene_name(here)}」（{here}），"
                        f"而趴趴GO沒有到「{scene.scene_name(ent.get('scene'))}」"
                        f"的傳送點 —— 自己走過去再開。")
                    return
                phase = "fly"
            else:
                phase = "enter"            # 在入口那張圖 → 先去撞傳點
        else:
            # ★ 已經在副本裡 → 照舊比對地圖指紋，對不上就大聲停用。
            grid = self._maps.get(sc)
            ok, why = dungeon.check_map(script, grid, scene.current_id(sc),
                                        scene.map_key)
            if not ok:
                self._stop(f"⛔ {why}")
                return
            phase = "run"
        try:
            self._mover = move.acquire(int(pid), injector.process_path(int(pid)),
                                       self)
        except Exception as exc:                         # noqa: BLE001
            self._stop(f"⚠ 無法安裝跳板：{exc}")
            return
        self._pid, self._sc, self._script = int(pid), sc, script
        self._reset_run()
        # ★ 從第幾步開始（使用者 2026-09-02）。⚠ 只在「已經在副本裡」時有效；
        #   還要先撞入口的話，進去之後照樣從這一步開始。
        self._i = max(0, min(self.start_box.currentIndex(),
                             len(script.steps) - 1)) if script.steps else 0
        self._phase = phase
        self._fly = fly
        self._map_key = here
        # ★★★ 全自動循環（使用者 2026-09-03）：「從第幾步」有選 → 只跑單輪。
        self._loop = self._i == 0
        self._party = (self.party_box.currentData() or "none") if self._loop else "none"
        if self._party == "bind":
            ppid = self.partner_box.currentData()
            psc = self._scanners.get(ppid) if ppid is not None else None
            if psc is None or ppid == int(pid):
                self._stop("⛔ 綁定分身：先在「綁定分身」選另一台開著的分身")
                return
            mine = charname.channel_from_title(self._titles.get(int(pid), ""))
            his = charname.channel_from_title(self._titles.get(ppid, ""))
            if mine and his and mine != his:
                # 跨分流組不了隊（實測：邀請送得出去、對方永遠收不到）
                self._stop(f"⛔ 綁定分身在「{his}」、你在「{mine}」—— 跨分流組不了隊，"
                           "先把它換到同一個分流")
                return
            try:
                self._pmover = move.acquire(ppid, injector.process_path(ppid), self)
            except Exception as exc:                     # noqa: BLE001
                self._stop(f"⚠ 綁定分身裝不了跳板：{exc}")
                return
            self._ppid, self._psc = ppid, psc
            self._partner_name = self.partner_box.currentText().split("（")[0].strip()
        if self._party != "none":
            self._cycle = "team"
            self._team_begin()
        self._refresh_steps()
        self._atk = TargetWorker(sc)
        self._atk.died.connect(self._on_died)
        self._atk.packets = True
        self._atk.start()
        self._keys = KeyWorker(self._hwnds.get(int(pid), 0), sc)
        self._keys.mode = MODE_PACKET
        self._keys.packets = True
        self._keys.mover = self._mover
        self._keys.vks = self._picked_keys()
        self._keys.start()
        self._started = True
        if phase == "fly":
            self.status.setText(
                f"「{script.name}」：先用趴趴GO飛去「{fly.name}」，"
                f"再去撞入口 —— {dungeon.describe_entrance(ent)}")
        elif phase == "enter":
            self.status.setText(
                f"「{script.name}」：先去撞入口傳送點 —— "
                f"{dungeon.describe_entrance(ent)}")
        else:
            self.status.setText(
                f"開始跑「{script.name}」共 {len(script.steps)} 步")

    def _stop(self, why: str = "", quiet: bool = False) -> None:
        if self._keys is not None:
            self._keys.set_on(False)
            self._keys.eid = None
            self._keys.stop()
            self._keys.wait(500)
            self._keys = None
        if self._atk is not None:
            self._atk.stop()
            self._atk.wait(500)
            self._atk = None
        if self._mover is not None:
            # ★ 停機也要把對話框收掉，不然人會帶著框走來走去。
            try:
                talkwnd.close_window(self._mover, self._sc)
                supply.leave_npc(self._mover, self._sc)
            except Exception:                            # noqa: BLE001
                pass
        if self._pmover is not None and self._ppid is not None:
            try:
                move.release(self._ppid, self)
            except Exception:                            # noqa: BLE001
                pass
            self._pmover = None
        self._supply_gen += 1            # 背景補給還在跑的話，結果不要再收
        if self._mover is not None and self._pid is not None:
            # ★ release() 不是 stop()：跳板是同一個 PID 共用的。
            try:
                move.release(self._pid, self)
            except Exception:                            # noqa: BLE001
                pass
        self._mover = None
        self._nav.reset()
        if self.run_cb.isChecked():
            self.run_cb.blockSignals(True)
            self.run_cb.setChecked(False)
            self.run_cb.blockSignals(False)
        started, self._started = self._started, False
        if why and not quiet:
            self.status.setText(why)
            # ★ 開跑之後因為「出問題」停下來 → 通知（使用者 2026-09-05）
            if started and why[:1] in PROBLEM_MARKS:
                self.notify(why)

    # ------------------------------------------------------------------
    # 掃描
    # ------------------------------------------------------------------
    def _on_scan(self, s) -> None:
        if self._sc is None or s.pid != self._pid:
            return
        self._last = s
        self._state, self._player = s.state, s.player

    def _my_pos(self):
        if not self._player:
            return None
        # ⚠⚠ `s.player` 是 `entity.snapshot()` 掃 VT_PLAYER 找到的位址，
        #   **已經是實體本體**（＝`pathfinder_this() + 8`）—— 這裡再 +8 讀到的
        #   是後面 8 bytes，實測回 (0.0, 0.0)：順移偵測、走到沒、傳點落點驗證
        #   全部拿 (0,0) 在算。2026-09-02 修。
        #   （`bag.player_entity()` 回的才是 pf 那一種，那邊要 +8，見
        #     [[entity-coordinates]]「差 8 bytes」那條。）
        return entity.read_pos(self._sc, self._player)

    def _live_monsters(self) -> list:
        """掃描結果裡還活著的怪。⚠ 屍體會在清單裡賴很久，一定要濾掉。

        ★ 死活看兩個訊號，任一個成立就是屍體（使用者 2026-09-05）：
          · 動畫狀態 'Dead'（一般怪）
          · **血量歸零**（`Entity.hp_zero`，實體 +0x288 恰好 0）—— 副本裡的
            柱子死掉屍體會留一段時間、動畫狀態**不會**變 'Dead'，只看狀態
            就會一直對著屍體出手。血量 −1（沒交戰）一律當活的。
        """
        if self._last is None:
            return []
        return [m for m in self._last.mons if not m.dead and not m.hp_zero]

    # -- 這一區走得到哪裡 ---------------------------------------------
    def _refresh_grid(self, me, dt: float) -> None:
        """定期重讀地形圖，並算出「我這一區」走得到哪些格子。

        ★★ 為什麼要定期重讀（使用者 2026-09-02）：機關解開會**把牆變成路**，
          但 `terrain.Cache` 的身分是 (地圖物件, 寬高, 場景編號, 列指標)，
          門開了那四樣一個都沒變 —— 不主動丟快取就會一直拿舊的牆算路，
          「解完謎卻說走不到」。所以每 GRID_REFRESH 秒 `drop()` 一次。
        ⚠ 讀不到圖就把 `_reach` 設成 None ＝**不過濾**（安全退化）：
          寧可多打幾隻打不到的，也不要因為讀不到圖就完全不出手。
        """
        self._grid_t -= dt
        stale = self._grid_t <= 0
        if stale:
            self._grid_t = GRID_REFRESH
            self._maps.drop()
        cell = (int(me[0]), int(me[1]))
        # 沒過期、而且我還站在上次算的那一區裡 → 直接沿用
        if not stale and self._reach is not None and cell in self._reach:
            return
        grid = self._maps.get(self._sc)
        if grid is None:
            self._grid = None
            self._reach, self._reach_n = None, 0
            return
        # ⚠⚠ 錨點要挑**最大的那一區**，不是「第一個問得到的」（2026-09-02
        #   修「不打怪」的第三個病灶）：角色常常站在地形圖標成不可走的格上
        #   （貼牆、石頭邊、剛落地），這時要問旁邊那圈；照順序取第一個問得到
        #   的，很可能拿到一個幾格大的**碎片區**（實測這張圖有 9/4/2 格的
        #   零星角落）—— 一旦 `_reach` 變成碎片，**每一隻怪都會被判成走不到**，
        #   症狀就是「完全不理怪物，直接走點位」。
        got = grid.reachable(*cell)
        if got is None:
            best = None
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                cand = grid.reachable(cell[0] + dx, cell[1] + dy)
                if cand and (best is None or len(cand) > len(best)):
                    best = cand
            got = best
        self._grid = grid
        if not got:
            self._reach, self._reach_n = None, 0
            return
        if len(got) < MIN_REGION:
            # 算出來只有幾格 ＝ 錨點多半錯了（碎片區）。⚠ 這種時候**不過濾**，
            #   不要拿一個顯然不對的可達區把全部的怪都判掉（安全退化）。
            self._notify(f"⚠ 算出來的可走區只有 {len(got)} 格，看起來不對 "
                         f"→ 這一輪不篩選怪物")
            self._reach, self._reach_n = None, 0
            return
        # 這一區的格數變了 ＝ 門開了／關了，講出來（使用者要看得到）
        if self._reach is not None and len(got) != self._reach_n:
            self._notify(f"地形變了：這一區從 {self._reach_n} 格變成 "
                         f"{len(got)} 格（機關開門／關門？）")
        self._reach, self._reach_n = got, len(got)

    def _can_reach(self, pos) -> bool:
        """那個位置**現在**走得到嗎（每次都重新問，⛔ 不記黑名單）。

        ⚠⚠ 使用者 2026-09-02 特別提醒的坑：
        > 「怪物可能跟我只有隔一牆但距離只有 3~2 格，這種的要注意排除」

        所以判定是「它站的那一格在不在**我這一區**」，**不是**看距離：
        · 怪站在可走格上但不在我的可達集合裡 → **隔壁區，直接排除**
          （⛔ 這裡不可以再看它旁邊那一圈 —— 隔一格薄牆的怪，鄰居格很可能
            剛好落在我這一區，那樣就會把「隔牆的怪」誤判成打得到）。
        · 怪站在**不可走格**上（貼牆、卡在門框、飛行怪）→ 它本來就不會在
          任何一區裡，這時才看旁邊一圈有沒有我走得到的格子。
        ⚠ 沒有可達集合（讀不到地形圖）一律回 True＝不過濾（安全退化）。
        """
        if self._reach is None:
            return True
        x, y = int(pos[0]), int(pos[1])
        if (x, y) in self._reach:
            return True
        if self._grid is not None and self._grid.walkable(x, y):
            return False              # 站在可走格卻不在我這一區 ＝ 隔壁區
        return any((x + dx, y + dy) in self._reach
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    # ------------------------------------------------------------------
    # 主迴圈
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if not self.run_cb.isChecked() or self._sc is None:
            return
        dt = TICK_MS / 1000.0
        sc = self._sc

        # 掃描節奏：有怪要快、趕路可以慢
        self._scan_t -= dt
        if self._scan_t <= 0:
            self._scan_t = SCAN_FAST if self._cur else SCAN_SLOW
            self._scan.request(self._pid, sc)

        if self._state is None or self._player is None:
            # ⚠ 讀不到本體就什麼都不做：不寫記憶體、不出手。
            self._say("等掃描定位（狀態／玩家物件）…")
            return
        me = self._my_pos()
        if me is None:
            self._say("讀不到自己的位置，這一拍不動")
            return
        self._me = me

        # ⓪-00 死了嗎？（使用者 2026-09-05：死掉要通知）—— 死了就停＋通知
        if self._check_death(dt):
            return

        # ⓪-0 全自動循環的外圈（補給／飛回入口／組隊）—— 這幾段自己會換圖，
        #   不能給下面的「地圖變了就停」抓到。
        if self._cycle_tick(dt):
            return

        # ⓪ 人被搬走了嗎？順移要**每一拍**都採樣，不然錯過那一下就看不到了。
        self._jumped = self._check_jump(me)
        # 換圖了嗎？—— 座標是**跟著地圖**的，圖一換舊座標全部沒有意義。
        #   只有 `portal` 那一步准許換圖（有些傳點確實會換圖）；其他時候換圖
        #   ＝被傳走／死亡回城，繼續跑就是拿別張圖的座標亂走。
        if self._check_map_change():
            return

        # ⓪-2 地形圖定期重讀（機關會開門）＋算出「我這一區」走得到哪裡
        self._refresh_grid(me, dt)

        # ① 先處理怪 —— 使用者定的規矩：路上有怪先殺光再去點位
        #    ⚠ 這裡回 True 就整拍不跑腳本 ＝「在殺怪就不跑點位」；
        #      回 False 代表**打得到的怪一隻都不剩**，才輪到腳本。
        #    ⛔ **只在副本裡面打**（使用者 2026-09-02：「進入副本前不要
        #      自動打怪」）——去副本路上遇到什麼都不理，直接趕路。
        if self._phase == "run" and self._fight(me, dt):
            return
        if self._phase != "run" and self._keys is not None:
            self._keys.set_on(False)      # 趕路途中確保不會出手
            self._keys.eid = None

        # ② 沒怪了 → 還在別張圖就先飛過去，在入口那張圖就去撞入口
        if self._phase == "fly":
            self._go_fly(dt)
            return
        if self._phase == "enter":
            self._go_entrance(me, dt)
            return
        if self._i >= len(self._script.steps):
            self._finish(dt)
            return
        self._run_step(me, dt)

    # -- 進副本 -------------------------------------------------------
    def _go_fly(self, dt: float) -> None:
        """人在別張圖：用天使趴趴GO飛到入口那張圖。

        使用者 2026-09-02 問：「我在天使學園開自動刷副本，會用天使趴趴GO
        飛過去最近地方嗎」—— 會（本來不會，這一支就是為此加的）。
        ⚠ 挑的是**離腳本記的入口最近**的那個傳送點（`jumpmap.nearest`），
          不是隨便第一個 —— 一張圖常有「入口」與「副本進入點」兩個落點。
        ⚠ 跟撞入口同一條規矩：**無限重試、不通知**（送不出去多半是暫時性的，
          剛戰鬥完／指令槽忙）。到了沒有是靠換圖偵測認的。
        """
        self._fly_total += dt
        self._fly_t -= dt
        if self._fly is None:                 # 理論上不會（開跑時就挑好了）
            self._stop("⛔ 沒有可以飛的傳送點")
            return
        if self._fly_t > 0:
            self._say(f"趴趴GO去「{self._fly.name}」…等落地"
                      f"（已試 {self._fly_total / 60.0:.1f} 分鐘）")
            return
        self._fly_t = FLY_RESEND
        ok, why = jumpmap.teleport(self._mover, self._sc, self._fly.jump_id)
        self._say(f"趴趴GO去「{self._fly.name}」（{why}）"
                  f"　已試 {self._fly_total / 60.0:.1f} 分鐘")

    def _go_entrance(self, me, dt: float) -> None:
        """還在外面：走去入口傳送點，撞進去。

        使用者 2026-09-02 定案：
        > 「如果在副本裡面會直接執行 json 開始跑；如果不在就會去撞副本傳點，
        >   如果撞了沒效就會每 5 秒送一次傳送直到成功，然後執行 json」
        > 「無限嘗試不需要通知」（同日補充）

        ⚠ 進去了是靠 `_check_map_change()` 認的（場景會變）——這裡只負責
          「走過去 ＋ 撞不進去就補送」。
        """
        ent = self._script.entrance or {}
        gx, gy = ent.get("to", [0, 0])
        self._enter_t += dt
        # ⛔⛔ **這一階段不設上限、也不通知**（使用者 2026-09-02 明令：
        #   「無限嘗試不需要通知」）。進不去副本是**暫時性失敗**——副本有
        #   冷卻、有人在門口擋、剛好被打斷都會這樣，重試一定會過；
        #   出口是使用者自己把勾選拿掉（[[transient-failure-auto-retry]]）。
        #   ⚠ 只有進到副本**之後**的傳點才有 3 分鐘上限＋通知（那是走不通
        #   的訊號，不是等一下就好）。
        mins = self._enter_t / 60.0
        tail = f"（已試 {mins:.1f} 分鐘）"
        if _d((gx, gy), me) > PORTAL_NEAR:
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._walk_onto(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    # ⛔ 這裡不用 `_blocked()`：那支等過寬限就會停，跟「無限
                    #   嘗試」衝突。改成重讀地形繼續試（人牆散開就走得到）。
                    self._grid_t = 0.0
                    self._say(f"去入口 ({gx}, {gy})：現在算不出路，重讀地形再試"
                              f"{tail}")
                    return
            self._say(f"去入口傳送點 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}{tail}")
            return
        # ★ 已經站在入口上 → **一直打進傳送點封包**（不是站著等）
        self._nav.reset()
        menu = [n for n in (ent.get("menu") or []) if n]
        gap = MENU_GAP
        # ★★ 有些副本門口是**撞上去它自己跳對話**，還要選第 1 項才進得去
        #   （使用者 2026-09-02：「要去撞他自己會產生對話，所以點點看沒用」）
        #   —— 所以這裡是「一邊打 0x0D，一邊看對話有沒有跳出來」，
        #   ⛔ 不是用點的（0x05 對這種門口沒有用）。
        # ⚠⚠ 這裡**不能**用「簽章跟一開始不一樣才處理」（2026-09-02 使用者實遇
        #   「在門口一直打封包，也沒按 1」）：第一次讀到的時候對話**已經開著**
        #   （撞過一次了／上一輪留下的），那一頁就被當成基準，之後永遠「沒變」，
        #   於是只剩打封包。→ 改成**看內容**：這一頁有選項就送，
        #   只用「這一頁動過了沒」（`_enter_acted`）防重複。
        # ★ 一樣先問「到底有沒有視窗」：確定沒有就別看那些殘留的全域，
        #   直接照節奏繼續撞門（讀不到＝None 才退回看內容）。
        if self._wnd_open(dt) is False:
            pg = None
        else:
            pg = talkwnd.page(self._sc)
        if pg is not None and pg.sig != self._enter_acted:
            if pg.has_options and menu:
                n = menu[min(self._menu_i, len(menu) - 1)]
                if n not in pg.options:
                    self._stop(f"⛔ 入口要選第 {n} 項，"
                               f"但這一頁只有 {list(pg.options)} —— 停下來")
                    return
                if sell.talk(self._mover, supply.talk_option(n)):
                    self._enter_acted = pg.sig
                    self._menu_i = min(self._menu_i + 1, len(menu))
                    self._menu_t = gap
                    # ⚠ 剛動過對話就別馬上又去撞門（撞一次會把進度歸零，
                    #   變成「送一項→撞→再送一項」空轉）——讓伺服器先回。
                    self._poke_t = PORTAL_POKE
                    self._say(f"入口對話：已送第 {n} 項"
                              f"（{self._menu_i}/{len(menu)}）{tail}")
                else:
                    self._say(f"入口對話：第 {n} 項送不出去，重試{tail}")
                return
            if pg.has_options and not menu:
                # ⛔ 跳出選項但腳本沒記要選哪一項 —— 絕不亂選。
                self._stop(f"⛔ 入口跳出 {len(pg.options)} 個選項，"
                           f"但腳本沒有記要選第幾項 —— 停下來")
                return
            if self._menu_i > 0:
                # 選項送完之後那一頁沒有選項 → 按確定翻過去
                ok, why = talkwnd.close_page(self._mover, self._sc)
                self._enter_acted = pg.sig
                self._menu_t = gap
                self._poke_t = PORTAL_POKE
                self._say(f"入口對話：沒有選項的那一頁 → 按確定"
                          f"（{'送出' if ok else why}）{tail}")
                return
        # 對話沒動靜 → 照節奏再撞一次（撞了才會跳對話）
        self._menu_t -= dt
        if self._menu_t > 0:
            self._say(f"站在入口上等對話…{tail}")
            return
        self._poke_t -= dt
        if self._poke_t > 0:
            self._say(f"站在入口上打封包…{tail}")
            return
        self._poke_t = PORTAL_POKE
        # 重撞一次＝對話從頭走，剛剛那一頁的「動過了」也作廢（可以再送一次）
        self._menu_i = 0
        self._enter_acted = None
        note = self._send_portal((gx, gy), ent.get("model"), "入口")
        self._say(f"{note}…{tail}")

    def _check_jump(self, me):
        """這一拍人有沒有被「搬」過去（順移）。回**跳之前站的位置** (x, y)，沒有回 None。

        ★ 傳點的完成訊號就是它（使用者 2026-09-02：「人被傳走不會換地圖，
          有順移就算吧，有時候傳點之間也很短」）—— 用距離門檻會漏掉短傳點，
          用速度就分得出來：跑步一拍最多 0.6 格，順移一拍好幾十格。
        ⚠ 兩次取樣隔太久（畫面卡住、剛開始跑）一律**不判**，只重設基準 ——
          寧可漏一次（下一拍還會再看），不要誤判成傳送了就跳下一步。
        ★ 回跳之前的位置是給傳點那一步驗「是不是**從傳點上**跳走的」（`PORTAL_FROM`）
          —— 伺服器拉回位置也是一拍跳好幾格，人不在傳點上就不算。
        """
        now = time.monotonic()
        prev, prev_t = self._pos_prev, self._pos_t
        self._pos_prev, self._pos_t = me, now
        if prev is None or now - prev_t > JUMP_MAX_GAP:
            return None
        return prev if _d(prev, me) >= JUMP_TILES else None

    def _check_map_change(self) -> bool:
        """換圖了就處理掉。回 True＝這一拍不要再往下跑。

        ⚠ `allow_scan=False`：這是 10 Hz 的心跳，全掃備援 0.3 秒會卡住畫面。
          讀不到就當「不知道」跳過 —— 讀不到 ≠ 換圖了。
        """
        here = scene.map_key(scene.current_id(self._sc, allow_scan=False))
        if here is None or self._map_key is None or here == self._map_key:
            return False
        if self._phase == "fly":
            # 飛到了：落在入口那張圖 → 去撞入口；直接落在副本裡 → 開跑。
            ent = self._script.entrance or {}
            self._map_key = here
            self._drop_target()
            self._nav.reset()
            self._grid_t = 0.0
            if here == self._script.scene:
                self._phase = "run"
                self._scan.force_full(self._pid)
                self._notify(f"飛到副本裡了（{scene.scene_name(here)}）→ 跑腳本")
            elif here == ent.get("scene"):
                self._phase = "enter"
                self._enter_t = 0.0
                self._poke_t = 0.0
                self._notify(f"飛到「{scene.scene_name(here)}」→ 去撞入口")
            else:
                self._say(f"飛到了「{scene.scene_name(here)}」，"
                          f"不是要去的那張圖 —— 再飛一次")
                self._fly_t = 0.0            # 立刻重送
            return True
        if self._phase == "enter":
            # ★ 還在外面撞入口：換到腳本那張圖就是進來了。
            if here != self._script.scene:
                self._stop(f"⛔ 從入口進到的是「{scene.scene_name(here)}」"
                           f"（{here}），不是腳本的"
                           f"「{scene.scene_name(self._script.scene)}」—— 停下來")
                return True
            grid = self._maps.get(self._sc)
            ok, why = dungeon.check_map(self._script, grid,
                                        scene.current_id(self._sc),
                                        scene.map_key)
            if not ok:
                self._stop(f"⛔ 進來了，但{why}")
                return True
            self._map_key = here
            self._phase = "run"
            # ⚠⚠ 換圖之後怪是**重新配置**的，掃描的「熱區」還是舊圖那一塊 →
            #   不強制全掃的話最久要等 FULL_EVERY(30 秒) 才看得到怪，
            #   症狀就是「進副本完全不打怪一直往點位走」（使用者 2026-09-02）。
            self._scan.force_full(self._pid)
            self._drop_target()
            self._nav.reset()
            self._grid_t = 0.0            # 換圖了 → 地形與可達區重算
            self._notify(f"進副本了（{scene.scene_name(here)}）→ 開始跑腳本")
            return True
        step = (self._script.steps[self._i]
                if self._i < len(self._script.steps) else {})
        if step.get("do") != dungeon.PORTAL:
            self._stop(
                f"⛔ 地圖變了（{scene.scene_name(self._map_key)} → "
                f"{scene.scene_name(here)}），但第 {self._i + 1} 步不是傳點 "
                f"—— 被傳走還是死亡回城了？停下來，不拿舊座標亂走")
            return True
        want = step.get("scene")
        if want is not None and want != here:
            self._stop(
                f"⛔ 第 {self._i + 1} 步的傳點應該到"
                f"「{scene.scene_name(want)}」（{want}），"
                f"實際到了「{scene.scene_name(here)}」（{here}）—— 停下來")
            return True
        # 到了新的圖：座標系換了，正在走的路線與正在打的怪全部作廢。
        self._map_key = here
        self._scan.force_full(self._pid)      # 新圖的怪是新配置的，熱區要重建
        self._drop_target()
        self._nav.reset()
        self._empty_since = 0.0
        self._say(f"第 {self._i + 1} 步　已經傳到"
                  f"「{scene.scene_name(here)}」")
        self._next()
        return True

    # -- 打怪 ---------------------------------------------------------
    # ------------------------------------------------------------------
    # 打怪流程 —— 掛機頁（farm_tab）的**複本**（使用者 2026-09-04 定案：
    #   「改成跟掛機一樣，不過是複製一份幾乎一樣的不要共用；
    #     超過 30 格要跳過；不管怎樣不會把怪物加黑名單，就換隻就好」）
    # 跟掛機那份的差別只有三處（其餘逐段照抄，理由見 farm_tab 同名函式）：
    #   · 沒有任何冷卻／黑名單：放棄就換一隻（有別隻時先挑別隻），沒別隻就再問同一隻。
    #   · 直線超過 MAX_CHASE 的怪整個不看。
    #   · 走不走得到用本頁的 _can_reach（薄牆規則），不用 nearest_open 放寬。
    # ⛔ 不 import 掛機那邊的函式與數字 —— 兩邊要能各自改。
    # ------------------------------------------------------------------
    def _mover_ok(self) -> bool:
        return self._mover is not None and bool(getattr(self._mover, "active", False))

    def _candidates(self) -> list:
        """照規則挑出「現在打得了」的怪，**照直線距離排序**（近→遠）。

        回 [(直線距離, 怪, 牠這一拍的座標)]；順便把不打的怪各是為什麼記進
        `_left_out`（狀態列要講得出來）。死活／座標**當場重讀**（read_live），
        掃描時記的早就過期了。
        """
        me = self._me
        pool: list = []
        far = unreach = 0
        for m in self._live_monsters():
            if not m.eid:
                continue                 # eid=0 挑到整條攻擊鏈都會空轉
            # ★ 血量也當場重讀：0 ＝ 打死了（柱子屍體狀態不會變 'Dead'）
            alive, st, p, hp = entity.read_live_hp(self._sc, m)
            if not alive or st == "Dead" or hp == 0 or p is None:
                continue
            d = _d(p, me) if me is not None else 0.0
            if d > MAX_CHASE:
                far += 1
                continue
            if not self._can_reach(p):
                unreach += 1
                continue
            pool.append((d, m, p))
        pool.sort(key=lambda t: t[0])
        self._left_out = (far, unreach)
        return pool

    def _targets(self) -> list:
        """**現在**打得了的活怪（清怪／休息／到點位的判定用）。每次都重新問。"""
        return [m for _dd, m, _p in self._candidates()]

    def _left_out_note(self) -> str:
        """上一輪不打的怪各是為什麼（給狀態列，一定要講得出來）。"""
        far, unreach = self._left_out
        parts = []
        if far:
            parts.append(f"{far} 隻超過 {MAX_CHASE:.0f} 格")
        if unreach:
            parts.append(f"{unreach} 隻走不到（隔壁區／沒有路）")
        return "、".join(parts)

    def _path_cost(self, grid, me, pos, max_cost: float | None = None
                   ) -> float | None:
        """從我這裡走到 pos 的**實際路徑長度**（格）；走不到／超過上限回 None。"""
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
        """候選裡**路徑最短**的那一隻 → (路徑長度, 直線距離, 怪)；沒有回 None。

        直線距離永遠 ≤ 路徑長度：照直線排序後，手上最好的路徑長度已經 ≤ 下一隻
        的直線距離就收手。讀不到地形圖 → 退回直線最近（安全退化）。
        """
        if grid is None or not me:
            for d, mon, _pos in pool:
                if cap is not None and d >= cap:
                    break
                return (d, d, mon)
            return None
        best = None
        for d, mon, pos in pool:
            limit = best[0] if best is not None else cap
            if limit is not None and d >= limit:
                break
            c = self._path_cost(grid, me, pos, max_cost=limit)
            if c is not None and (best is None or c < best[0]):
                best = (c, d, mon)
        return best

    def _pick_next(self) -> bool:
        """挑**路徑最短**的一隻接著打；挑不到回 False。⛔ 沒有黑名單。"""
        pool = self._candidates()
        if not pool:
            return False
        # 剛換掉的那一隻只在「還有別隻」時往後排 —— 不是黑名單，只是先換一隻。
        if self._last_gave_up is not None and len(pool) > 1:
            pool = [t for t in pool if t[1].eid != self._last_gave_up] or pool
        grid, me = self._grid, self._me
        best = self._nearest_by_path(pool, grid, me)
        if best is None and grid is not None:
            # 有地形圖卻每一隻都算不出路 ＝ 真的沒有走得到的怪，這一輪不挑
            #（⛔ 不退回直線最近：那等於明知走不到還鎖上去）。
            return False
        if best is None:
            d, mon, _p = pool[0]         # 沒地形圖 → 照直線挑
        else:
            _cost, d, mon = best
        self._engage(d, mon)
        return True

    def _engage(self, d: float, mon) -> None:
        """鎖定這一隻：所有「跟目標綁在一起」的狀態整組重設，再通知兩條執行緒。
        換目標**只准走這一支**。"""
        self._cur = mon
        self._stuck = 0.0
        self._anchor = self._me
        self._path_pts = -1
        self._line_clear = False
        self._no_grid = ""
        self._path_t = PATH_GAP                   # 下一拍就算
        self._path_gap = PATH_GAP
        self._way = []
        self._unreach = 0
        self._hurt = False
        self._push_in = False
        self._switch_t = 0.0
        self._handoff_fail = False
        self._handoff_t = 0.0
        self._near_fail = 0
        self._near_from = None
        self._gone = 0
        self._walked_ok = True
        self._why = ""
        self._last_hp = -1
        self._empty_since = 0.0
        # ★ 打起來了 → 正在數的「休息」作廢（使用者 9/2：清乾淨才算進入休息）
        self._wait_left = 0.0
        self._atk.attack(self._state, mon)
        self._keys.eid = mon.eid
        self._keys.set_on(True)
        self._say(f"鎖定「{mon.name}」　距離 {d:.1f} 格")

    def _switch_closer(self, cur, dist: float | None) -> bool:
        """趕路途中冒出**路徑明顯更短**的怪就改打牠；真的換了回 True。
        ⚠ 打傷過的絕不換；要短 SWITCH_GAIN 格以上才換（防乒乓）；SWITCH_GAP 節流。"""
        now = time.monotonic()
        if self._hurt or dist is None or now < self._switch_t:
            return False
        self._switch_t = now + SWITCH_GAP
        pool = self._candidates()
        me, grid = self._me, self._grid
        cur_pos = next((p for _dd, m2, p in pool if m2.eid == cur.eid), None)
        cur_cost = self._path_cost(grid, me, cur_pos)
        if grid is None:
            cur_cost = dist
        elif cur_cost is None:
            cur_cost = float("inf")
        best = self._nearest_by_path(
            [t for t in pool if t[1].eid != cur.eid], grid, me,
            cap=None if cur_cost == float("inf") else cur_cost - SWITCH_GAIN)
        if best is None or best[0] > cur_cost - SWITCH_GAIN:
            return False
        cost2, d2, m2 = best
        self._notify(f"改打更近的：「{cur.name}」實走 {cur_cost:.1f} 格 → "
                     f"「{m2.name}」實走 {cost2:.1f} 格")
        self._atk.hold_off()
        self._cur = None
        self._keys.eid = None
        self._engage(d2, m2)
        return True

    def _walk_toward(self, gx: float, gy: float, me, keep: float) -> int:
        """往 (gx,gy) 走，在距離 keep 格處停。回路徑點數（0 = 走不了）。

        近距離（< NEAR_WALK）且直線可通 → walk_near 直走不尋路（連兩次沒動就改尋路）；
        其他 → walk_route 交**我們自己算的**點（隔地形＝繞路點 _way、直線可通＝目標那格）。
        ⛔ 沒有路徑點就不走（不再退回遊戲的尋路撞牆）。
        """
        if not self._mover_ok():
            return 0
        gd = math.hypot(gx - me[0], gy - me[1])
        if gd <= keep:
            return 0
        if (gd < NEAR_WALK and self._path_pts <= 1 and self._near_fail < 2
                and self._line_clear):
            if self._near_from is not None and me:
                if math.hypot(me[0] - self._near_from[0],
                              me[1] - self._near_from[1]) < 0.3:
                    self._near_fail += 1
                else:
                    self._near_fail = 0
            self._near_from = me
            ok = self._mover.walk_near(self._sc, self._player, gx, gy, keep)
            self._walk_t = 0.0
            return 1 if ok else 0
        self._near_from = None
        pts = self._way or ([(int(gx) + 0.5, int(gy) + 0.5)]
                            if self._line_clear else None)
        if not pts:
            return 0
        n = self._mover.walk_route(self._sc, self._player, gx, gy,
                                   stop_short=keep, points=pts)
        self._walk_t = 0.0
        return n

    def _give_up(self, why: str) -> None:
        """這一隻先放著，換一隻。⛔ 不記黑名單：只是「有別隻時先挑別隻」，
        沒別隻就再問同一隻；並立刻重讀地形（門可能開了）。"""
        m = self._cur
        if m is not None:
            self._last_gave_up = m.eid
            self._notify(f"「{m.name}」{why} → 換一隻")
        self._drop_target()
        self._grid_t = 0.0
        self._pick_next()

    def _fight(self, me, dt: float) -> bool:
        """**打得到的**怪還有就打（掛機頁 tick() 打怪那段的複本）。
        回 True＝這一拍在打怪，腳本先不動；False＝打得到的一隻都不剩。"""
        self._me = me
        if self._cur is None:
            if not self._pick_next():
                # ★ 一隻都挑不到 → 定期要求全掃當保險（熱區可能整塊漏掉）。
                now = time.monotonic()
                if now - self._full_req_t >= FULL_HUNT_GAP:
                    self._full_req_t = now
                    self._scan.force_full(self._pid)
                skipped = len(self._live_monsters())
                if skipped:
                    self._say(f"周圍 {skipped} 隻怪都不打：{self._left_out_note()}"
                              f" → 當作這裡清光了")
                self._keys.set_on(False)
                self._keys.eid = None
                self._last_gave_up = None
                return False
        m = self._cur
        # ★ 正在打的這隻每拍當場重讀一次：物件還在嗎／動畫狀態／血量。
        alive, st, _lp, hp_ent = entity.read_live_hp(self._sc, m)
        # ★★ 血量歸零＝打死了，屍體還在也不管（使用者 2026-09-05：副本裡的
        #   柱子死掉屍體會留一段時間、狀態不變 'Dead'，不看血量會一直對屍體出手）。
        #   ⚠ 只認恰好 0：−1 是沒交戰／讀不到，照打。
        if alive and hp_ent == 0:
            self._say(f"「{m.name}」血量歸零＝打死了（屍體還在）→ 換下一隻")
            self._last_gave_up = None
            self._drop_target()
            return True
        # ★ 正在打的那隻不在掃描結果裡 → 用剛才那次讀取驗物件：還在而且不是屍體
        #   ＝掃描漏了（照打＋補一次全掃）；物件沒了才**連續兩拍**判沒了。
        if not any(x.eid == m.eid for x in self._live_monsters()):
            if alive and st != "Dead":
                self._gone = 0
                now = time.monotonic()
                if now - self._full_req_t >= FULL_HUNT_GAP:
                    self._full_req_t = now
                    self._scan.force_full(self._pid)
            else:
                self._gone += 1
                if self._gone >= GONE_SCANS:
                    self._drop_target()
                    return True
        else:
            self._gone = 0

        hp = self._atk.hp
        mp = entity.read_pos(self._sc, m.addr)
        dist = _d(mp, me) if (mp and me) else None
        if mp:
            self._keys.pos = (round(mp[0]), round(mp[1]))
            self._keys.pos_f = (mp[0], mp[1])       # 量距離用原始座標

        # ── 每 _path_gap 秒問一次地形圖「我跟這隻怪之間有沒有地形」 ──
        self._path_t += dt
        if (self._path_t >= self._path_gap and mp is not None and me
                and dist is not None):
            self._path_t = 0.0
            plan_t0 = time.perf_counter()
            grid = self._grid
            mtile = (int(me[0]), int(me[1]))
            ttile = (int(mp[0]), int(mp[1]))
            self._no_grid = "" if grid is not None else (
                getattr(self._maps, "why", "") or "讀不到地形圖")
            if grid is None:
                self._path_pts, self._way = 0, []
                self._line_clear = False
            elif grid.clear_line(mtile, ttile):
                self._path_pts, self._way, self._unreach = 1, [], 0
                self._line_clear = True
            else:
                self._line_clear = False
                wp = grid.waypoints(mtile, ttile)
                if wp:
                    self._path_pts = max(2, len(wp))
                    self._way = [(x + 0.5, y + 0.5) for x, y in wp]
                    self._unreach = 0
                else:
                    self._path_pts, self._way = 0, []
                    self._unreach += 1
            self._path_gap = min(max(PATH_GAP,
                                     (time.perf_counter() - plan_t0)
                                     * PATH_BUDGET),
                                 PATH_GAP_MAX)
        blocked = self._path_pts > 1
        rng = self._keys.min_range
        reach_walk = (ATTACK_PACKET_RANGE if rng is None
                      else min(ATTACK_PACKET_RANGE, float(rng) + 1.0))
        handoff = bool(self._keys.handoff and not blocked
                       and not self._handoff_fail)
        in_range = ((dist is not None and dist <= HANDOFF_RANGE) if handoff
                    else self._keys.in_range_of_any(dist))
        if handoff and dist is not None:
            if dist <= reach_walk or self._hurt:
                self._handoff_t = 0.0
            else:
                self._handoff_t += dt
                if self._handoff_t >= HANDOFF_WAIT:
                    self._handoff_fail = True
        reach_keep = HANDOFF_RANGE if handoff else reach_walk
        margin = 2.0 if reach_keep >= 6.0 else 0.6
        keep = (MELEE_RANGE if (blocked or self._push_in)
                else min(max(reach_keep - margin, move.MIN_GAP),
                         reach_keep - 0.5))
        # 地形圖連續算不出路 → 換一隻（打傷過的不算、貼身的不算）
        if (self._unreach >= UNREACH_HITS and not self._hurt
                and dist is not None and dist > NO_PATH_NEED):
            self._give_up(f"走不到（尋路連續 {UNREACH_HITS} 次算不出，{dist:.1f} 格）")
            return True
        gd = dist
        slack = min(WALK_SLACK, max(0.3, reach_keep - 0.5 - keep))
        need_walk = gd is not None and (
            gd > keep + slack or (not in_range and gd > keep))
        walk_gap = (WALK_GAP_FAR if (gd is not None and gd > FAR_ENOUGH)
                    else WALK_GAP)
        self._walk_t += dt
        if (me and mp and not self._busy_walking()
                and self._walk_t >= walk_gap and need_walk):
            self._walked_ok = self._walk_toward(mp[0], mp[1], me, keep) > 0

        self._atk.packets = bool(self._keys.mode == MODE_PACKET
                                 and self._keys.packets and self._keys.skill
                                 and self._keys.mover is not None)
        self._atk.engaged = self._keys.selected
        self._keys.player = self._player
        self._keys.reach = HANDOFF_RANGE if handoff else 0.0
        self._keys.client_walk = handoff
        self._keys.set_on(in_range)               # ★ 邊走邊打

        waiting_opener = getattr(self._keys, "open_wait", 0.0) > 0.0
        if in_range and dist is not None and dist <= keep:
            self._why = "出手中"
        elif in_range:
            # （跟掛機唯一不同的一句：隔地形時講出來，使用者查「盯著怪」才有線索）
            self._why = (f"⛰ 零傷害疑似擋線 → 繞過去貼身（停 {keep:.1f} 格）"
                         if self._push_in
                         else f"⛰ 隔著地形 → 邊打邊沿路徑貼身（停 {keep:.1f} 格）"
                         if blocked
                         else f"打得到，同時走近到 {keep:.1f} 格")
        elif dist is None:
            self._why = "⚠ 讀不到座標"
        elif not self._mover_ok():
            self._why = "⚠ 移動跳板沒裝上"
        elif self._no_grid:
            self._why = f"⚠ {self._no_grid} → 這一拍不走位"
        elif not self._walked_ok:
            self._why = "⛔ 走不過去"
        elif blocked:
            self._why = (f"⛰ 隔著地形 → 沿路徑走到 ({mp[0]:.0f},{mp[1]:.0f})"
                         if mp is not None and len(self._way) >= 2
                         else "⛰ 隔著地形，走近一點")
        else:
            self._why = "→ 走進攻擊範圍"
        if waiting_opener:
            self._why = f"⏳ 等首發技能冷卻好（{self._keys.open_wait:.0f} 秒）"

        # ── 進展判定（真訊號＝目標血量；位移看離錨點的淨位移）──
        if 0 < hp < self._last_hp:
            self._hurt = True
            self._push_in = False
        if me and (self._anchor is None
                   or math.hypot(me[0] - self._anchor[0],
                                 me[1] - self._anchor[1]) > STUCK_EPS):
            self._anchor = me
            self._stuck = 0.0
        elif (0 < hp < self._last_hp) or self._last_hp < 0:
            self._stuck = 0.0
        else:
            self._stuck += dt
        self._last_hp = hp
        if waiting_opener or self._no_grid:
            self._stuck = 0.0
        if self._switch_closer(m, dist):
            return True
        engaged = bool(in_range and self._keys.selected)
        if (engaged and not self._push_in and not handoff
                and self._stuck >= PUSH_IN_SECS
                and dist is not None and dist > NO_PATH_NEED):
            self._push_in = True
            self._notify(f"「{m.name}」打得到卻 {PUSH_IN_SECS:.0f} 秒零傷害"
                         f"（隔著障礙物？）→ 繞過去貼身打")
        limit = STUCK_ENGAGED if engaged else STUCK_SECS
        if self._stuck >= limit:
            self._give_up(f"{limit:.0f} 秒沒進展"
                          + ("（打不中？）" if engaged else "（走不過去？）"))
            return True
        self._say(f"打怪：{m.name}　"
                  + (f"{dist:.1f} 格" if dist is not None else "？格")
                  + f"　{self._why}　（走得到的還有 {len(self._targets())} 隻）")
        return True

    def _drop_target(self) -> None:
        self._cur = None
        self._stuck = 0.0
        self._anchor = None
        self._last_hp = -1
        self._push_in = False
        self._hurt = False
        if self._keys is not None:
            self._keys.set_on(False)
            self._keys.eid = None
        if self._atk is not None:
            self._atk.hold_off()
        self._nav.reset()
        # 死了／消失了 → 立刻重掃，別等下一個慢拍。
        self._scan_t = 0.0

    def _on_died(self, eid, _confirmed) -> None:
        if self._cur is not None and self._cur.eid == eid:
            self._last_gave_up = None
            self._drop_target()

    # -- 跑腳本 -------------------------------------------------------
    def _run_step(self, me, dt: float) -> None:
        step = self._script.steps[self._i]
        kind = step.get("do")
        # 上一拍沒卡在「沒有路」→ 等門開的計時歸零（門開了就重新算）
        if not self._blocked_last:
            self._unreach_t = 0.0
        self._blocked_last = False
        self._step_t += dt
        # ⛔ 這裡**沒有逾時**（使用者 2026-09-02：「不會有『幾秒沒到就壞掉』，
        #   那個拔掉」）—— 慢就慢（遺落之地一段路要走 533 格），
        #   卡住就一直試，出口是使用者自己取消勾選。
        if kind == dungeon.CLEAR:
            # 怪已經在 _fight 清掉了，走到這裡就代表沒怪。
            # ⚠ 但要沉澱一下：實體是跟著玩家串流進來的，一拍掃不到不算數。
            self._empty_since += dt
            self._say(f"清怪確認中…{self._empty_since:.1f}/{CLEAR_SETTLE:.0f} 秒")
            if self._empty_since >= CLEAR_SETTLE:
                self._next()
            return

        if kind == dungeon.WAIT:
            # ★ 使用者 2026-09-02：「休息要確認周圍沒有可以打到的怪物
            #   才能進入休息」——「休息」是拿來等機關／等對話準備好的，
            #   一邊被打一邊倒數等於白等（而且倒數完就去點機關，人還在挨打）。
            #   ⚠ 正常情況 `_fight` 已經擋在前面；這裡再明寫一次，
            #   而且中途被打斷就**從頭數**（見 `_fight` 挑到目標時歸零）。
            if self._targets():
                self._wait_left = 0.0
                self._say(f"第 {self._i + 1} 步　先別休息：周圍還有 "
                          f"{len(self._targets())} 隻走得到的怪")
                return
            if self._wait_left <= 0:
                self._wait_left = float(step.get("secs", 1))
            self._wait_left -= dt
            self._say(f"等待…剩 {max(self._wait_left, 0):.1f} 秒")
            if self._wait_left <= 0:
                self._next()
            return

        if kind == dungeon.WALK:
            gx, gy = step["to"]
            if _d((gx, gy), me) <= ARRIVE:
                # ★ 使用者 2026-09-02 定的規矩，這裡再明寫一次當保險：
                #   「並且周圍（不含走不到的）沒有怪物才算是有走到點位」。
                #   —— 正常情況 `_fight` 已經擋在前面了，但這一條是規格，
                #   寫在完成判定裡才不會被別的改動不小心繞過去。
                if self._targets():
                    self._say(f"第 {self._i + 1} 步　已經站上點位，"
                              f"但周圍還有 {len(self._targets())} 隻走得到的怪"
                              f" —— 先清光才算到")
                    return
                self._nav.reset()
                self._next()
                return
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._walk_onto(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到 ({gx}, {gy})", (gx, gy))
                    return
            self._say(f"第 {self._i + 1} 步　走到 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}"
                      f"　{self._mon_note()}")
            return

        if kind == dungeon.PORTAL:
            # ★ 完成條件**不是**「走到那一格」而是「人被搬走了」——踩上傳點
            #   的下一瞬間人就被移走，那一格永遠不會「到達」。
            #   換圖那種由 `_check_map_change` 接手；同一張圖裡的順移看這裡。
            gx, gy = step["to"]
            if self._jumped:
                frm = self._jumped                 # 跳之前站的位置
                land = step.get("land")
                near_land = bool(land) and _d(land, me) <= LAND_TOL
                from_portal = _d((gx, gy), frm) <= PORTAL_FROM
                if not near_land and not from_portal:
                    # ★ 人不在傳點上就跳了 ＝ 不是傳點搬的（伺服器拉回／被擊退）
                    #   → 不算完成、更不能當「傳到別的地方」停下
                    #   （2026-09-05 無限塔第 41／52 步的誤停就是這個）。
                    self._notify(f"第 {self._i + 1} 步　位置一拍跳了 "
                                 f"{_d(frm, me):.0f} 格，但跳之前離傳點 "
                                 f"{_d((gx, gy), frm):.0f} 格（不在傳點上）"
                                 f"→ 不算傳送，繼續走")
                elif land and not near_land:
                    self._stop(
                        f"⛔ 第 {self._i + 1} 步：傳點把人送到 "
                        f"({me[0]:.0f}, {me[1]:.0f})，"
                        f"腳本記的出口是 ({land[0]:g}, {land[1]:g}) —— "
                        f"差 {_d(land, me):.0f} 格，停下來")
                    return
                else:
                    self._drop_target()
                    self._scan.force_full(self._pid)   # 順移到新的一區＝新的怪
                    self._say(f"第 {self._i + 1} 步　傳點過了，落在 "
                              f"({me[0]:.0f}, {me[1]:.0f})")
                    self._next()
                    return
            if _d((gx, gy), me) <= PORTAL_NEAR:
                # ★ 已經站在傳點上卻沒被搬走 → 每 PORTAL_POKE 秒對它送一次
                #   互動（有些傳點要點一下才走）。⛔ 不是每一拍狂送：那是
                #   洪水，伺服器會擋（跟補給點 NPC 同一個道理）。
                self._nav.reset()
                self._poke_portal(step, dt)
                return
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._walk_onto(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到傳點 ({gx}, {gy})", (gx, gy))
                    return
            self._say(f"第 {self._i + 1} 步　走進傳點 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}")
            return

        if kind == dungeon.INTERACT:
            self._do_interact(step, me, dt)
            return

        self._stop(f"⛔ 第 {self._i + 1} 步是不認得的動作「{kind}」")

    def _send_portal(self, at, want, tag: str) -> str:
        """對那個傳點**主動送一次 0x0D**（＝踩上去那一包）。回一句說明。

        使用者 2026-09-02：「進副本不是站在傳送口等傳送，而是要一直打進
        傳送點封包」。用 `portal.enter` —— 它會**當場重讀重驗**那個物件
        （+0x1D0 每次載圖重配，用舊值送就是送到別的東西身上）。
        ⛔ 找不到對應的觸發物件就只回報，**絕不就近送一個**。
        """
        trigs = portal.nearby(self._sc, at, PROP_TOL)
        if trigs is None:
            return "物件清單讀不到，等下一次"
        hit = _pick(trigs)
        if not hit:
            return "附近找不到傳點物件"
        pf = move.pathfinder_this(self._sc)
        if not pf:
            return "讀不到自己的實體（載圖中？）"
        ok, msg = portal.enter(self._mover, self._sc, hit[0], pf)
        return f"{tag}：{msg}" if not ok else f"{tag}：已送 0x0D"

    def _poke_portal(self, step: dict, dt: float) -> None:
        """人已經站在傳點上 —— **每 PORTAL_POKE 秒主動送一次 0x0D**。

        使用者 2026-09-02：「進副本不是站在傳送口等傳送，而是要一直打進
        傳送點封包」。⛔ 沒有上限，過不去就一直打。
        ⚠ 遊戲自己那支有去重欄（站著不動永遠不會送第二次，見 portal.py
          檔頭）—— 主動送就沒有這個限制，所以不必退開再走回來。
        """
        self._poke_t -= dt
        mins = self._step_t / 60.0
        if self._poke_t > 0:
            self._say(f"第 {self._i + 1} 步　站在傳點上打封包…"
                      f"（已 {mins:.1f} 分鐘）")
            return
        self._poke_t = PORTAL_POKE
        note = self._send_portal(tuple(step["to"]), step.get("model"), "傳點")
        self._say(f"第 {self._i + 1} 步　{note}…已 {mins:.1f} 分鐘")

    def _wnd_open(self, dt: float) -> bool | None:
        """對話視窗現在開著沒（True／False／**None＝問不到**）。

        ⚠ 這支是叫進遊戲問的（`talkwnd.window_open`，會等遊戲做完），
          所以 `WND_TTL` 秒內直接用上一次的答案 —— ⛔ 不要每拍問。
          剛點下去要把 `_wnd_t` 歸零，強制重問。
        """
        self._wnd_t -= dt
        if self._wnd_t > 0:
            return self._wnd
        self._wnd_t = WND_TTL
        self._wnd = talkwnd.window_open(self._mover, self._sc)
        return self._wnd

    def _do_interact(self, step: dict, me, dt: float,
                     tag: str = "", finish=None) -> None:
        """點一個物件並把對話走完。

        `tag` ＝訊息前綴（不給就是「第 N 步」）；`finish` ＝走完之後做什麼
        （不給就是進到下一步）—— 入口那種「點下去選第 1 項才進得去」的門口
        也是走這一支（使用者 2026-09-02），只是走完不能前進步驟，
        要等場景真的變了才算進去。
        """
        tag = tag or f"第 {self._i + 1} 步"
        finish = finish or self._next
        ax, ay = step["at"]
        want_model = step.get("model")
        # ⚠ 先走到旁邊再點：太遠就發互動包＝人還沒到、對話先開，選項送出去
        #   會落空（買東西那條路踩過同一個坑）。
        # ★★ 有記站位就**走到那個站位**（使用者 2026-09-02：「跟 NPC 對話會
        #   記錄我在哪個位置對話的，會走到那個位置才對話」）——那是製作時
        #   真的講到話的位置，一定站得住、距離也一定夠。
        #   沒記的舊腳本才退回「靠近那個物件、留 TALK_KEEP 格」。
        stand = step.get("stand")
        if stand:
            sx, sy = stand
            if _d((sx, sy), me) > ARRIVE:
                if _d((sx, sy), me) <= NAV_DEAD:
                    note = self._walk_onto(sx, sy)
                else:
                    note = self._nav.step(self._sc, self._mover, self._player,
                                          sx, sy)
                    if self._nav.stuck and self._nav.stuck_reason == "grid":
                        self._blocked(dt, f"走不到對話站位 ({sx}, {sy})",
                                      (sx, sy))
                        return
                self._say(f"{tag}　走去對話站位 ({sx:g}, {sy:g})"
                          f"　剩 {_d((sx, sy), me):.1f} 格　{note}"
                          f"　{self._mon_note()}")
                return
        elif _d((ax, ay), me) > TALK_NEAR:
            if _d((ax, ay), me) <= NAV_DEAD:
                note = self._walk_beside(ax, ay, TALK_KEEP)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      ax, ay)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到對話點 ({ax}, {ay})", (ax, ay))
                    return
            self._say(f"{tag}　走去對話點 ({ax}, {ay})"
                      f"　剩 {_d((ax, ay), me):.1f} 格　{note}"
                      f"　{self._mon_note()}")
            return
        self._nav.reset()

        if not self._clicked:
            # ★ 站穩了才點：走路中送互動包，人還沒到、對話開不起來
            #   （補給那邊同一條規矩「先走到位才發互動包」）。
            #   ⚠ 但**最多等 STILL_WAIT 秒**：`is_walking` 有可能恆為 True
            #     （被推、人擠人），那樣就會永遠不點。等過頭就照點。
            if self._busy_walking():
                self._still_t += dt
                if self._still_t < STILL_WAIT:
                    self._say(f"{tag}　還在走，站穩再點…"
                              f"（{self._still_t:.1f}/{STILL_WAIT:.1f} 秒）")
                    return
                self._say(f"{tag}　一直在動（被推？）—— 不等了，直接點")
            self._still_t = 0.0
            # ★★ 還沒點就說「對話視窗開著」＝**上一段對話的殘留**（每一步收尾
            #   都會 destroy，所以正常情況這裡一定是沒有）。不先收掉的話下面
            #   整段會以為對話已經開了 → 一路按確定 → 永遠不點物件（使用者
            #   2026-09-03 實機卡死就是這樣）。
            if self._wnd_open(dt):
                talkwnd.close_window(self._mover, self._sc)
                supply.leave_npc(self._mover, self._sc)
                self._wnd, self._wnd_t = None, 0.0
                self._say(f"{tag}　先收掉上一段留下的對話框再點")
                return
            props = scenery.nearby(self._sc, (ax, ay), PROP_TOL)
            if props is None:
                # ⚠ 讀不到 ≠ 沒有。等下一拍再試，不要當成「這裡沒東西」。
                self._say("物件清單讀不到，重試中…")
                return
            hit = _pick(props)
            if not hit:
                self._stop(
                    f"⛔ {tag}：({ax}, {ay}) 附近 {PROP_TOL:.0f} "
                    f"格內找不到可互動的物件 —— 停下來")
                return
            # ★ 基準要在**點下去之前**讀：點完對話可能立刻就開了，
            #   那時再讀就跟第一頁一樣，永遠判不出「有沒有點到」。
            pg0 = talkwnd.page(self._sc)
            ok, msg = produce.click(self._mover, self._sc, hit[0])
            if not ok:
                self._say(f"點不下去（{msg}），重試中…")
                return
            self._clicked = True
            self._menu_i = 0
            # ⚠ 那些全域關著也會留舊值（見 talkwnd）→ 簽章一直沒變＝沒點到。
            self._talk_sig = self._talk_base = (pg0.sig if pg0 else None)
            self._talk_same, self._click_t, self._nudge = 0, 0.0, 0
            self._click_best = None
            self._talk_did = ""
            self._talk_seen, self._close_t, self._close_n = False, 0.0, 0
            self._gone_t = 0.0
            self._wnd, self._wnd_t = None, 0.0   # 強制重問一次
            # ★ 間隔照這一步自己存的（腳本製作那頁可以調）——太快送選項，
            #   伺服器那邊對話還沒準備好就會被拒絕（使用者 2026-09-02）。
            self._menu_t = MENU_GAP
            self._say(f"{tag}　已點外觀 {hit[0].model}")
            return

        # ★★★ 走對話（使用者 2026-09-02 定案）：
        #   「只要沒選項就幫我對話到結束或出現選項」
        #   → 腳本裡**只記要選第幾項**，沒有選項的那些頁自己按確定過掉。
        #   ⚠ 舊腳本裡記的 0（過場）直接忽略：現在是自動的，再送一次會多按。
        menu = [n for n in (step.get("menu") or []) if n]
        gap = MENU_GAP
        self._menu_t -= dt
        if self._menu_t > 0:
            return
        self._menu_t = gap
        # ★★★ 「現在到底有沒有對話視窗」＝**硬訊號**（使用者 2026-09-02：
        #   「請要明確知道有沒有視窗」）—— 問遊戲自己那支「依代號查視窗」的
        #   函式（`talkwnd.window_open`），⛔ 不再靠那幾個 Lua 全域猜，
        #   它們關掉之後還會留著舊值。
        #   ⚠ 回 None ＝**不知道**（叫不動／讀不到），不可以當成「沒有視窗」→
        #     那種情況才退回舊的「簽章有沒有變」判斷。
        wnd = self._wnd_open(dt)
        pg = talkwnd.page(self._sc)
        if wnd:
            self._talk_seen = True
        # ★ 點下去之後對話沒開起來 → 再點一次（不是點一次就不管）。
        if (wnd is False and not self._talk_seen) or (
                wnd is None and pg is not None
                and pg.sig == self._talk_base):
            # ⚠ 遊戲收到 0x05 會**自己走過去**才開對話 —— 還在靠近的期間
            #   不可以插手（重點一次會把那趟打斷）。所以只有「沒有更靠近」
            #   的時候才累加計時。
            d_now = _d((ax, ay), me)
            if self._click_best is None or d_now < self._click_best - CLICK_PROGRESS:
                self._click_best, self._click_t = d_now, 0.0
                self._say(f"{tag}　遊戲正在走過去…"
                          f"剩 {d_now:.1f} 格")
                return
            self._click_t += gap
            if self._click_t >= CLICK_RETRY:
                self._click_t = 0.0
                props = scenery.nearby(self._sc, (ax, ay), PROP_TOL) or []
                hit = _pick(props)
                if not hit:
                    self._say(f"{tag}　點了沒反應，"
                              f"而且那一格現在掃不到東西 —— 等下一輪")
                    return
                # ★ 先**調整站位往它靠上去**再點（站著硬點是沒用的）。
                #   物件位置用**現場重讀的**那一個，不是腳本裡的舊座標。
                tx, ty = hit[0].x, hit[0].y
                self._click_best = None      # 重新給遊戲一次自己走過去的機會
                keep = NUDGE_KEEP[min(self._nudge, len(NUDGE_KEEP) - 1)]
                self._nudge += 1
                how = (self._walk_onto(tx, ty) if keep <= 0
                       else self._walk_beside(tx, ty, keep))
                ok, msg = produce.click(self._mover, self._sc, hit[0])
                self._say(f"{tag}　點了沒反應 → 靠近一點"
                          f"（留 {keep:g} 格，{how}）再點"
                          f"（{'送出' if ok else msg}）")
                return
            self._say(f"{tag}　等對話出現…"
                      f"（{self._click_t:.0f}/{CLICK_RETRY:.0f} 秒）")
            return
        if wnd is False and self._talk_seen:
            # ★★★ 開過又不見了 ＝ 這段對話**真的**走完了（使用者 2026-09-02：
            #   「對話後關視窗太慢了，不知道在等啥」）—— 不必等它「穩定」、
            #   也不必再叫 DestroyMessageWnd（視窗本來就沒了）。
            # ⚠ 但**剛按完確定**那一小段不算：確定會 destroy 視窗，下一頁要等
            #   伺服器回來才重建（見 CLOSE_GRACE）。這段時間內只能「等下一頁」。
            if self._talk_did == "opt":
                # ★ 送了選項、對話就直接結束（視窗不見）＝伺服器收到那一項並
                #   拿它收尾了（「帶我去」這種最後一項就是這樣）。那一項算送到，
                #   ⛔ 不可以判成「選項沒送到」而停機（2026-09-03 回歸抓到）。
                self._menu_i += 1
                self._talk_did = ""
            if (self._talk_did == "close" and self._page_ended is False
                    and self._gone_t < CLOSE_GRACE):
                self._gone_t += gap
                self._wnd_t = 0.0                # 下一拍再問一次有沒有新視窗
                self._say(f"{tag}　按了確定，等下一頁…"
                          f"（{self._gone_t:.1f}/{CLOSE_GRACE:.1f} 秒）")
                return
            if self._menu_i < len(menu):
                self._stop(f"⛔ {tag}：對話已經關掉了，"
                           f"但腳本還有 {len(menu) - self._menu_i} 個選項沒送到"
                           f" —— 停下來（NPC 的對話跟腳本記的不一樣？）")
                return
            supply.leave_npc(self._mover, self._sc)
            finish()
            return
        if pg is None:
            # 讀不到 Lua 表 → 退化成「照腳本把選項送完就離開」（不自動翻頁）。
            if self._menu_i < len(menu):
                n = menu[self._menu_i]
                if sell.talk(self._mover, supply.talk_option(n)):
                    self._menu_i += 1
                self._say(f"{tag}　讀不到對話狀態 → 照腳本送第 {n} 項")
                return
            supply.leave_npc(self._mover, self._sc)
            finish()
            return
        # ⚠⚠ 這些全域**關掉對話還會留著**，所以一切以「簽章變了＝換頁了」為準：
        #   · 換頁了 → 上一個動作生效了（送出去的選項才算真的送到）
        #   · 沒換頁 → 還在等伺服器回，**不可以**當成「對話結束」
        #     （使用者 2026-09-02 回報的 bug 就是這樣誤判：明明有選項卻說
        #      「對話結束但腳本還有一個選項沒送到」）
        if pg.sig != self._talk_sig:
            if self._talk_did == "opt":
                self._menu_i += 1           # 有換頁 ＝ 剛剛那一項真的送到了
            self._talk_sig, self._talk_same, self._talk_did = pg.sig, 0, ""
            self._close_t, self._close_n = 0.0, 0   # 有換頁＝真的有反應
            self._gone_t = 0.0
        else:
            self._talk_same += 1
        if not self._talk_did:
            # 這一頁還沒動過 → 決定要做什麼
            if pg.has_options:
                if self._menu_i >= len(menu):
                    # ⛔ 跳出選項但腳本沒說要選哪一項 —— **絕不亂選**。
                    self._stop(f"⛔ {tag}：對話跳出 "
                               f"{len(pg.options)} 個選項，"
                               f"但腳本沒有記要選第幾項 —— 停下來")
                    return
                n = menu[self._menu_i]
                if n not in pg.options:
                    self._stop(f"⛔ {tag}：腳本要選第 {n} 項，"
                               f"但這一頁只有 {list(pg.options)} —— 停下來")
                    return
                if not sell.talk(self._mover, supply.talk_option(n)):
                    self._say(f"第 {n} 項送不出去（指令槽忙碌），重試中…")
                    return
                self._talk_did = "opt"
                self._wnd_t = 0.0            # 動過了 → 下一拍重問視窗
                self._say(f"{tag}　已送第 {n} 項"
                          f"（{self._menu_i + 1}/{len(menu)}）")
                return
            if wnd is None and self._talk_same < TALK_SETTLE:
                # 只有**問不到有沒有視窗**的時候才要先等它穩定 —— 剛點下去
                # 那幾拍讀到的可能還是上一次的殘留（那些全域不會被清掉）。
                # 視窗確定開著就直接按，不用等（使用者嫌慢）。
                self._say(f"{tag}　等對話出現…")
                return
            # ★ 按之前先問遊戲「這是不是最後一頁」（ismessageend 那個旗標）：
            #   最後一頁 → 按完視窗不見就立刻收工（使用者要快）；
            #   不是 → 視窗不見只是 destroy 了，要等伺服器把下一頁送來。
            self._page_ended = talkwnd.message_ended(self._sc)
            ok, why = talkwnd.close_page(self._mover, self._sc)
            self._talk_did, self._close_t, self._gone_t = "close", 0.0, 0.0
            self._wnd_t = 0.0                # 按了確定 → 下一拍重問視窗
            self._say(f"{tag}　沒有選項的那一頁 → 按確定"
                      f"（{'送出' if ok else why}"
                      f"{'，最後一頁' if self._page_ended else ('，還有下一頁' if self._page_ended is False else '')}）")
            return
        # 動過了但畫面沒換頁 —— 分兩種情況，⛔ 不可以都當成「對話結束」
        if self._talk_did == "opt":
            # ⛔ 不設上限（使用者定）：伺服器慢就慢，一直等。
            self._say(f"{tag}　等對話回應…"
                      f"（已 {self._talk_same * gap:.0f} 秒）")
            return
        if wnd:
            # 按了確定、視窗**還在** ＝ 還沒翻過去（或翻到長得一模一樣的一頁）。
            #   → 隔一下補送一次確定，⛔ 不要當成「對話結束」。
            self._close_t += gap
            if self._close_t >= CLOSE_RETRY:
                self._close_t = 0.0
                self._close_n += 1
                if self._close_n >= CLOSE_GIVEUP:
                    # ★ 按了這麼多次、對話頁一格都沒變 ＝ 那個「視窗還在」是
                    #   殘留（見 CLOSE_GIVEUP）。destroy 掉當這段對話結束，
                    #   ⛔ 不要再無限按下去（會整趟卡死）。
                    talkwnd.close_window(self._mover, self._sc)
                    supply.leave_npc(self._mover, self._sc)
                    self._wnd, self._wnd_t = None, 0.0
                    self._close_n = 0
                    if self._menu_i < len(menu):
                        self._stop(f"⛔ {tag}：確定按了 {CLOSE_GIVEUP} 次對話頁"
                                   f"都沒變，腳本還有 "
                                   f"{len(menu) - self._menu_i} 個選項沒送到"
                                   f" —— 停下來")
                        return
                    self._say(f"{tag}　確定按不動＝殘留的對話框，收掉當結束")
                    finish()
                    return
                ok, why = talkwnd.close_page(self._mover, self._sc)
                self._wnd_t = 0.0
                self._say(f"{tag}　確定沒反應 → 再按一次"
                          f"（{self._close_n}/{CLOSE_GIVEUP}，"
                          f"{'送出' if ok else why}）")
                return
            self._say(f"{tag}　等對話翻頁…")
            return
        if self._talk_same < TALK_SETTLE:
            self._say(f"{tag}　等對話翻頁…")
            return
        # 按了確定又沒有下一頁 ＝ 這段對話走完了
        if self._menu_i < len(menu):
            self._stop(f"⛔ {tag}：對話結束了，"
                       f"但腳本還有 {len(menu) - self._menu_i} 個選項沒送到"
                       f" —— 停下來（NPC 的對話跟腳本記的不一樣？）")
            return
        # ★ 收尾：**先把對話框從畫面上收掉**再送離開互動。
        #   使用者 2026-09-02：「現在都會帶著最後無異議對話離開到處跑，
        #   要把對話框點掉避免 BUG」——`messageclose` 只通知伺服器，
        #   畫面上那個框要叫 Lua 的 DestroyMessageWnd 才會收。
        talkwnd.close_window(self._mover, self._sc)
        supply.leave_npc(self._mover, self._sc)
        finish()

    def _next(self) -> None:
        self._i += 1
        self._step_t = 0.0
        self._unreach_t = 0.0
        self._blocked_last = False
        self._menu_i = 0
        self._clicked = False
        self._talk_seen = False
        self._close_t = 0.0
        self._wait_left = 0.0
        self._empty_since = 0.0
        self._poke_t = 0.0            # 下一步的傳點要馬上補送第一次
        self._nudge = 0               # 靠近重試的次數歸零
        self._still_t = 0.0
        self._nav.reset()
        self._refresh_steps()

    def _finish(self, dt: float) -> None:
        """腳本跑完了 —— 還要**周圍沒有打得到的怪**才算這一趟結束。

        ⚠ 用 `_targets()` 不是 `_live_monsters()`：隔壁區有一隻永遠打不到的
          怪就會讓這一趟永遠收不了工。
        """
        if self._targets():
            self._empty_since = 0.0
            return
        self._empty_since += dt
        self._say(f"腳本跑完，確認周圍沒怪…"
                  f"{self._empty_since:.1f}/{CLEAR_SETTLE:.0f} 秒")
        if self._empty_since >= CLEAR_SETTLE:
            self._done = True
            if not self._loop:
                self._stop("✔ 這一趟結束：腳本跑完，周圍也沒有怪了")
                return
            self._rounds += 1
            self._start_supply_trip()

    # -- 全自動循環：補給 → 飛回入口 → 組隊 --------------------------------
    def _cycle_tick(self, dt: float) -> bool:
        """外圈這一拍有事做就回 True（呼叫端整拍不跑內圈）。"""
        if self._cycle == "supply":
            self._supply_tick(dt)
            return True
        if self._cycle == "team":
            self._team_tick(dt)
            return True
        if self._cycle == "back":
            self._back_tick(dt)
            return True
        return False

    def _start_supply_trip(self) -> None:
        """這一趟刷完 → 跑一次**跟掛機同一套**的回程補給（存倉→修裝→買水），
        回程改跳到**離入口最近的傳送點**（back_to＝入口座標＋入口那張圖）。
        整趟是背景執行緒（阻塞式、幾十秒到幾分鐘），`_supply_tick` 每拍輪詢。"""
        ent = self._script.entrance or {}
        ex, ey = (ent.get("to") or [None, None])[:2]
        back = ((ex, ey, ent.get("scene"))
                if ent.get("scene") is not None and ex is not None else None)
        if self._keys is not None:
            self._keys.set_on(False)
            self._keys.eid = None
        if self._atk is not None and hasattr(self._atk, "hold_off"):
            self._atk.hold_off()
        self._drop_target()
        try:
            plan = robot.potion_buy_ids(self._mover, self._sc, self._pid)
        except Exception:                                # noqa: BLE001
            plan = None
        self._supply_gen += 1
        gen, mv, sc = self._supply_gen, self._mover, self._sc
        self._supply_result = None
        self._supply_progress = "出發"
        self._i = 0                       # 下一趟從頭跑
        self._done = False
        self._empty_since = 0.0

        def _worker():
            try:
                res = supply.run_full_supply(
                    mv, sc, say=lambda m: setattr(self, "_supply_progress", m),
                    back_to=back, potions=plan)
            except Exception as exc:                      # noqa: BLE001
                res = (False, f"補給出錯：{exc}")
            if gen == self._supply_gen:
                self._supply_result = res

        t = threading.Thread(target=_worker, daemon=True)
        self._supply_thread = t
        t.start()
        self._cycle = "supply"
        self._say(f"✔ 第 {self._rounds} 趟結束 → 回程補給…")

    def _supply_tick(self, dt: float) -> None:
        res = self._supply_result
        if res is None:
            self._say(f"第 {self._rounds} 趟結束 → 補給中：{self._supply_progress}")
            return
        ok, why = res
        self._notify(("補給完成" if ok else "⚠ 補給沒跑完") + f"：{why}")
        self._supply_result = None
        if not self._plan_route():
            return
        self._cycle = "back"

    def _plan_route(self) -> bool:
        """照現在人在哪決定下一段（跟開跑時同一套）：別張圖 → 飛；入口那張圖 →
        撞入口；副本裡 → 直接跑。回 False＝已經 _stop 了。"""
        here = scene.map_key(scene.current_id(self._sc))
        ent = self._script.entrance or {}
        self._map_key = here
        self._fly = None
        self._drop_target()
        self._nav.reset()
        if here is not None and self._script.scene is not None and here != self._script.scene:
            if not ent:
                self._stop("⛔ 這份腳本沒記入口傳送點，回不去副本")
                return False
            if here != ent.get("scene"):
                ex, ey = (ent.get("to") or [None, None])[:2]
                self._fly = jumpmap.nearest(ent["scene"], ex, ey)
                if self._fly is None:
                    self._stop(f"⛔ 趴趴GO沒有到「{scene.scene_name(ent.get('scene'))}」的傳送點")
                    return False
                self._phase = "fly"
                self._fly_t, self._fly_total = 0.0, 0.0
            else:
                self._phase = "enter"
                self._enter_t = 0.0
                self._poke_t = 0.0
        else:
            self._phase = "run"
            self._grid_t = 0.0
        return True

    def _back_tick(self, dt: float) -> None:
        """補給完 → 用趴趴GO飛回入口那張圖（落地才輪到組隊）。"""
        if self._phase == "fly":
            if self._check_map_change():
                if self._phase != "fly":           # 落地了（入口那張圖或副本裡）
                    self._team_begin()
                return
            self._go_fly(dt)
            return
        self._team_begin()

    def _team_begin(self) -> None:
        """進入組隊段：先退組（兩隻都退），再邀請／等遊戲配隊。"""
        if self._party == "none":
            self._cycle = "go"
            return
        self._cycle = "team"
        self._team_sub = "leave"
        self._team_t = 0.0
        self._join_t = 0.0
        self._team_note_t = 0.0

    def _team_tick(self, dt: float) -> None:
        mine = team.members(self._sc)
        self._team_t -= dt
        self._join_t -= dt
        if self._team_sub == "leave":
            his = team.members(self._psc) if self._psc is not None else []
            if mine is None or his is None:
                self._say("組隊：讀不到隊伍狀態，等下一拍…")
                return
            if not mine and not his:
                self._team_sub = "invite" if self._party == "bind" else "wait"
                self._team_t = 0.0
                return
            if self._team_t <= 0:
                self._team_t = LEAVE_GAP
                if mine:
                    team.leave(self._mover)
                if his and self._pmover is not None:
                    team.leave(self._pmover)
            self._say("組隊：先退組…（等隊伍名單清空）")
            return
        if self._team_sub == "wait":
            # 遊戲自動組隊（遊戲裡設定）：只等隊伍名單出現人
            if mine:
                self._notify(f"組到隊了（{len(mine)} 人）→ 開跑")
                self._cycle = "go"
                return
            self._say("退組了，等遊戲自動組隊…（隊伍名單有人就開跑）")
            return
        if self._team_sub == "invite":
            names = {m.name for m in (mine or [])}
            if self._partner_name in names:
                self._notify(f"已跟「{self._partner_name}」組隊（均分）→ 開跑")
                self._cycle = "go"
                return
            if self._team_t <= 0:
                self._team_t = INVITE_GAP
                ok, why = team.invite(self._mover, self._partner_name, team.SHARE_EVEN)
                if not ok:
                    self._say(f"組隊：邀請送不出去（{why}），重試中…")
                    return
            if self._join_t <= 0 and self._pmover is not None:
                self._join_t = JOIN_GAP
                team.join(self._pmover, self._psc)
            self._say(f"組隊：邀請「{self._partner_name}」入隊中（均分）…")
            return

    # ------------------------------------------------------------------
    def _refresh_steps(self) -> None:
        self.steps.clear()
        if self._script is None:
            return
        for i, s in enumerate(self._script.steps):
            mark = "▶" if i == self._i else ("✔" if i < self._i else "　")
            self.steps.addItem(f"{mark} {i + 1:>2}. {dungeon.describe(s)}")
        self.prog.setText(f"{min(self._i, len(self._script.steps))}"
                          f" / {len(self._script.steps)} 步")

    def _say(self, text: str) -> None:
        # ★ 重要提示要**看得到**：同一拍裡 `_refresh_grid` 講的「地形變了」
        #   會被緊接著的走路訊息蓋掉（2026-09-02 實跑時整趟一次都沒顯示到）。
        #   所以 `_notify()` 的內容在 NOTICE_SECS 秒內一直掛在前面。
        if self._notice and time.monotonic() < self._notice_t:
            text = f"{self._notice}　｜　{text}"
        else:
            self._notice = ""
        self.status.setText(text)

    def _check_death(self, dt: float) -> bool:
        """角色死了嗎 → 停機＋通知；回 True＝這一拍到此為止。

        訊號跟掛機頁一樣：`player.read().hp <= 0`。基準位址從掃描快照拿
        （`s.stats`），沒有就 `locate_fast`；讀不到（物件搬家）就丟掉重找、
        **不算死**。連續 DEATH_HITS 次 ≤ 0 才算 —— 單次可能是搬家瞬間的垃圾值。
        """
        self._death_poll += dt
        if self._death_poll < DEATH_POLL:
            return False
        self._death_poll = 0.0
        # ⚠ 讀失敗（分身關了、物件搬家）一律**不算死**，下一輪再試。
        try:
            if not self._stats:
                self._stats = (getattr(self._last, "stats", None)
                               or player.locate_fast(self._sc))
                if not self._stats:
                    return False
            st = player.read(self._sc, self._stats)
        except Exception:                                # noqa: BLE001
            self._stats = None
            self._dead_hits = 0
            return False
        if st is None:
            self._stats = None
            self._dead_hits = 0
            return False
        if st.hp > 0:
            self._dead_hits = 0
            return False
        self._dead_hits += 1
        if self._dead_hits < DEATH_HITS:
            return False
        self._stop("☠ 角色死亡 —— 自動刷副本已停止（復活後要自己重新開跑）")
        return True

    # -- 通知（跟自動掛機那一頁同一套、同一份設定）-----------------------
    def notify(self, msg: str) -> None:
        """送警報通知（受「啟用通知」總開關管；關掉只是不送，該停還是停）。"""
        if self._notifier is None or not self.notify_cb.isChecked():
            return
        who = self.who.currentText() or "自動刷副本"
        note = self._notifier.fire(who, msg)
        self.status.setText(self.status.text() + f"　[{note}]")

    def _nkey(self, field: str) -> str:
        """通知那一列的設定鍵 —— 跟自動掛機共用（不含帳號，所有分身共用）。"""
        return f"{self._prefix}.{field}"

    def _load_notify(self) -> None:
        """把共用的通知設定套到畫面上（別的分頁改了也會被叫到）。"""
        on = config.get(self._nkey("notify_on"), True)
        method = config.get(self._nkey("notify"), "sound")
        room = config.get(self._nkey("tg_id"), "")
        keep, self._loading = self._loading, True
        try:
            self.notify_cb.setChecked(bool(on))
            if str(method) == "telegram":
                self.rb_tg.setChecked(True)
            else:
                self.rb_sound.setChecked(True)
            self.tg_id.setText(str(room or ""))
        finally:
            self._loading = keep

    def _save_notify(self) -> None:
        """通知那一列改了 → 存進共用鍵，掛機頁與其他分頁同步跟著變。"""
        if self._loading:
            return
        config.set(self._nkey("notify_on"), self.notify_cb.isChecked())
        config.set(self._nkey("notify"),
                   "telegram" if self.rb_tg.isChecked() else "sound")
        config.set(self._nkey("tg_id"), self.tg_id.text().strip())
        config.save()
        for page in list(_NOTIFY_PAGES):
            try:
                if page is not self and page._prefix == self._prefix:
                    page._load_notify()
            except RuntimeError:                 # 那一頁的 C++ 物件已被刪掉
                pass

    def _notify(self, text: str) -> None:
        """停留幾秒的提示（地形變了、可達區怪怪的…）。"""
        self._notice, self._notice_t = text, time.monotonic() + NOTICE_SECS
        self.status.setText(text)

    def _mon_note(self) -> str:
        """狀態列上的怪物盤點：看得到幾隻、其中幾隻走得到。

        ★ 使用者 2026-09-02 說「走過去才打怪」時，第一件要分清楚的事就是
          「當下到底看不看得到怪」——沒有這一行只能用猜的。
        """
        live = len(self._live_monsters())
        if not live:
            return "（周圍 0 隻怪）"
        n = len(self._targets())
        why = self._left_out_note()
        return (f"（周圍 {live} 隻怪，走得到 {n} 隻"
                + (f"；{why}" if why and n < live else "") + "）")

    def _busy_walking(self) -> bool:
        """正在走路嗎（正在走就別再送，重下指令會把上一段打斷）。"""
        try:
            return bool(entity.is_walking(self._sc, self._player))
        except Exception:                                # noqa: BLE001
            return False

    def _walk_onto(self, gx: float, gy: float) -> str:
        """**走到那一格本身**（點位、傳點用）。不留距離、不尋路。

        ⚠ 只在最後那 3 格用：`navigate.ARRIVE` 是 3.0，尋路器在那之內
          就當「到了」什麼都不做（見 `NAV_DEAD`）。
        ⛔ 這一支跟 `_walk_beside` **刻意分開**（使用者 2026-09-02 要求）：
          「走到點位」跟「走到某個東西旁邊」是兩件事，用同一支加參數很容易
          在某一邊誤留距離／誤踩上去。
        """
        if self._busy_walking():
            return "走最後一段…"
        try:
            self._mover.walk_exact(self._sc, self._player, gx, gy)
        except Exception:                                # noqa: BLE001
            return "走最後一段（送不出去）"
        return "直接走到那一格（尋路器 3 格內不動作）"

    def _walk_beside(self, gx: float, gy: float, keep: float) -> str:
        """走到**離目標 keep 格的旁邊**（沒有記錄站位的舊腳本才用）。

        機關／NPC 常常站在不可走的格上，走到它身上永遠走不到。
        """
        if self._busy_walking():
            return "走最後一段…"
        try:
            self._mover.walk_near(self._sc, self._player, gx, gy, keep)
        except Exception:                                # noqa: BLE001
            return "走最後一段（送不出去）"
        return f"走到旁邊（留 {keep:g} 格）"

    def _blocked(self, dt: float, what: str, goal=None) -> None:
        """尋路說「現在沒有路」——重讀地形繼續試。**永遠不會因此停機。**

        ⛔ 副本的牆是解謎才打開的，而且使用者 2026-09-02 定案「不要幾秒沒到
          就壞掉」——所以這裡只重讀地形＋回報，不設上限。
        """
        self._unreach_t += dt
        self._blocked_last = True
        self._grid_t = 0.0                    # 下一拍就重讀地形（門可能剛開）
        self._say(f"第 {self._i + 1} 步：{what} —— 現在沒有路，"
                  f"重讀地形繼續試（已 {self._unreach_t / 60.0:.1f} 分鐘）"
                  f"{self._why_unreachable(goal)}")

    def _why_unreachable(self, goal) -> str:
        """停下來時**把證據講出來**：那一格到底是牆，還是在別的區。

        ⚠ 只在要停的那一拍算（多一次泛洪 6ms）—— 使用者看到「走不到」時
          最想知道的就是「是門沒開，還是我腳本點錯地方」。
        """
        if goal is None or self._grid is None:
            return "門沒開？點位在別的區？"
        gx, gy = int(goal[0]), int(goal[1])
        if not self._grid.walkable(gx, gy):
            return (f"（({gx}, {gy}) 那一格在地形圖上**是牆** —— "
                    f"腳本的點位放在不能站的地方？）")
        comp = self._grid.reachable(gx, gy)
        mine = self._reach_n if self._reach is not None else "?"
        return (f"（那一格可以站，但屬於另一區：它那區 "
                f"{len(comp) if comp else '?'} 格、我這區 {mine} 格 —— "
                f"中間的門沒開，或是要先走傳點）")

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        """應用程式關閉前的收尾。

        ⚠⚠ **一定要是 `on_close()` 不是 `closeEvent()`**：分頁是塞在
          QTabWidget 裡的子視窗，Qt 根本不會對它發 close 事件 —— 主視窗
          關閉時走的是 `MainWindow.closeEvent` → 逐個 `tab.on_close()`。
          寫成 closeEvent 等於沒收尾，掃描執行緒還在跑就被解構，
          Qt 丟「QThread: Destroyed while thread '' is still running」，
          嚴重時直接 0xC0000409 當掉（跟自我監察那條同一個坑）。
        """
        self._timer.stop()
        self._stop(quiet=True)
        self._scan.stop()
        self._scan.wait(800)
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
