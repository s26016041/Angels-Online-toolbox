"""自寫補給 —— 商人購買（取代官方補給的購買那塊）。

流程（都是背景封包，不動滑鼠）：
    找最近的商人 NPC → 送「選定」包開交易 → 照補給頁購買清單把不足的買到目標數量。
「關交易（買）」是純客戶端（`OnNPCSaleClose` 只 window.destroy，不送封包），不用送；
但**「關維修」不一樣**：要送「離開 NPC」包 0x5D29C1(0x22,0)，不送角色會卡住不能走
（repairclose 0x5906CB，見 REPAIR_CLOSE_FN）。

各封包／位址的出處與實機驗證見 memory 的 self-supply-buy（2026-08-14 嵐狐三次購買、
走遠再回來確認選定包能從關閉狀態開交易、修裝＋關窗）。

★ 新位址都已登進 locate.py 的 AOB（supply.INTERACT_FN / REPAIR_ALL_FN / REPAIR_CLOSE_FN），
  warm() 開機定位、改版自動跟上；沿用的送包/建包走 jumpmap.*、talkaction 走 sell.TALK_FN。
"""
from __future__ import annotations

import math
import struct
import threading
import time

from app.game import (attack, bag, gather, quickbar, robot, itemname, scene,
                      entity, move, jumpmap, recall, inventory,
                      navigate, sell, lua, talkwnd)

# 「這個參數沒給」——跟「給了但讀不到（None）」要分得開（見 `_wait_dialog`）。
_UNSET_PAGE = object()

# --- 位址：能沿用的都沿用已登記 AOB 的（改版自動跟上，呼叫時才讀值） -----
#   建包 / 送出 / 連線 → jumpmap.BUILD_FN / SEND_FN / CONN_PTR（0x50E0AE / 0x711380 / 0x9B6660）
#   talkaction        → sell.TALK_FN（0x5D9066，實測與買東西共用同一支）
#   只有「點 NPC 互動」是新的 → supply.INTERACT_FN，已登進 locate.py 由 warm() 定位。

# ★★★ 中途叫停（2026-09-09 使用者要求：「觸發回程補給，我把開始掛機關閉就要停止」）
#   整趟補給是呼叫端開的**背景執行緒**在跑（阻塞式，幾十秒到幾分鐘）。以前只能等它
#   跑完 —— 掛機都關掉了，角色還在城裡繼續修裝、買水、再飛回練功點。
#   現在 `run_full_supply(should_stop=…)`：所有等待一律走 `_nap()`，它每次醒來都問
#   一次呼叫端「還要不要跑」，要停就丟 `Aborted` 把整趟中斷，外層攔下來收尾
#   （關商店、離開 NPC 互動 —— ⛔ 不送 0x22 會卡在互動狀態）再回 (False, 已中止)。
#   ⚠ 回呼存在 **thread-local**：同一個工具箱可能好幾台分身各跑各的一趟，
#   模組級單一變數會互相踩。
class Aborted(Exception):
    """呼叫端要求中止這一趟補給（例如掛機被關掉）。"""


_ABORT = threading.local()
# ★ 這一趟有沒有「20 秒都講不到話」（使用者 2026-09-20：要通知＋停機）。
#   ⚠ 跟 `_ABORT` 一樣用 threading.local：一台分身一條補給執行緒，不會互相蓋掉。
_TALK = threading.local()


def _abort_check() -> None:
    """呼叫端說要停就丟 Aborted（沒設回呼＝永遠不停，舊行為）。"""
    fn = getattr(_ABORT, "fn", None)
    if fn is not None and fn():
        raise Aborted()


def _talk_failed(why: str) -> None:
    """記一筆「這一趟有 NPC 講不到話」——`run_full_supply` 收尾時讀走（見 TALK_FAIL）。

    ⚠ 只記原因，不記是哪個 NPC：哪一步失敗由呼叫端的回傳訊息講
    （「開交易失敗」「倉庫開不起來」…），兩邊拼起來才完整。
    """
    _TALK.why = why


def _talk_clear() -> None:
    _TALK.why = ""


def talk_failure() -> str:
    """這一趟（同一條執行緒）最後一次「講不到話」的說明；沒有就是空字串。"""
    return getattr(_TALK, "why", "")


def _nap(secs: float) -> None:
    """等一下下，順便看呼叫端有沒有叫停。

    ⛔ 補給流程裡**一律用它**，不要直接 `time.sleep` —— 補給幾乎所有時間都花在
    這些等待上（等換圖、等走到、等對話、等視窗），只在段落之間問「要不要停」
    的話，最久要等好幾十秒才停得下來。
    """
    time.sleep(secs)
    _abort_check()


BUY_OPCODE = 0x27          # 跟 NPC 買

# ★★★ 開交易的完整序列（2026-08-14 嵐狐實測跨圖冷交易 39→44 確認）：
#   ① 點 NPC → 開對話框（WND_MESSAGE）。
#      ⚠ 手動用 WALK_FN 走過去**不會**觸發它；opcode 0x07 只是面向不是點擊。
#      ⚠⚠ 2026-09-03 起這一步改叫 **TRY_ACT_FN（官方滑鼠點真正動手那支）**，
#          舊的自動走路狀態機 0x54A520 退成備援 —— 原因見 TRY_ACT_FN 的檔頭。
#   ② talkaction(10)「我要買東西」→ 開販售頁（WND_NPCSALE）、伺服器端交易開啟。
#   ③ 買（0x27）。
#   ⚠ talkaction(10) 碼是對的，但**一定要先用 0x54A520 開真對話**才有效（少了它 talkaction 無效）。
INTERACT_FN = 0x0054A520   # ★ AOB 定位（locate.py supply.INTERACT_FN）：thiscall ecx=玩家物件−8
# ★ 出處同上段實測序列①：0x54A520 的第一參數，1＝走到 NPC 並互動（開對話）。
INTERACT_MODE = 1

# ★★★ 2026-09-03 使用者「貼在 NPC 臉上都說不到話，滑鼠點卻很正常」→ 反組譯出
#   **官方滑鼠點 NPC 是兩步，我們以前只做了第二步**：
#     ① TryAct(eid, kind)＝真正動手那支：當拍判距離(0x508DF6)＋視線(0x5B87E4)，
#        過了就直接開對話（0x5065E7 → 事件 "R011" → 送 0x07 面向 ＋ 0x05 點選）；
#        不在範圍就**用官方尋路自己走一步**，下一拍再叫就繼續走。
#     ② 只有 ① 沒做到才設自動走路狀態機 INTERACT_FN(mode=1)。
#   而 ② 會先把倒數 [pf+0x41B0] 設成 **20 拍**、歸零才第一次嘗試 —— 貼臉也要空等；
#   [pf+0x41A0]（動作鎖）不是 0 時更是整包寫進延後槽 [pf+0x41B8] **不執行**，
#   對我們看起來就是「點了沒反應」，然後上層開始橋位置 → 就是那個「超卡」。
#   ★ 實機（2026-09-03 黑狐 永夜城 銀行小姐艾寶）：6.4 格叫一次 → 官方尋路走到
#     1.4 格；再叫一次 → **0.13 秒對話框就開了**。
#   ⚠ 回傳值**不能**當「成功了沒」：kind 3 走完 0x5065E7 是 `mov al,1` 收尾
#     （實測貼近成功那次也回 1）→ 成功與否一律看對話框代號有沒有變（_wait_dialog）。
TRY_ACT_FN = 0x00506784    # ★ AOB 定位（locate.py supply.TRY_ACT_FN）：thiscall ecx=pathfinder_this
# ★ 出處：自動走路狀態機 mode 1 的每拍動作就是 TryAct(eid, 3)（0x5493D9），
#   而點擊處理式對 NPC 也是先叫 TryAct(eid, 3) 再視情況設 mode 1（0x5AC8E0）。
KIND_TALK = 3
# ★★★ 跟 **NPC** 講話的 kind 是 **2**，不是 3（2026-09-06 黑狐 棕櫚基地 實測＋封包鏈）：
#   使用者滑鼠點銀行員的封包鏈是 0x5AC95C 點擊處理式 → 狀態機 mode 4 → tick 0x5494A5
#   `push 2; push eid; call TryAct` → 0x5069BC 送包。我們一直用 kind 3（那是雕像／公佈欄／
#   副本物件這類場景物件的 kind，produce.click 用它沒錯；⚠ **製作檯不行**：2026-09-07 廚狐
#   實測 kind 3 走到旁邊也不開面板，開面板走 produce.click_bench 自送 0x05），對 NPC 的範圍小很多：
#     kind 3：站 (186,137) Δ(3,-2) 30 發全開不了；kind 2：同一格 0.53 秒開。
#   kind 2 實測範圍（tile 差，9 個點）：|Δx|≤5 且 |Δy|≤3 全開，Δx=6/7、Δy=4 不開，
#   隔著櫃檯也開（沒有視線檢查）。見 TALK_BOX_X/Y。
KIND_NPC = 2
# ★★★ 打怪（普攻）的 kind ＝ **1**（2026-09-20 反組譯＋使用者封包實錄定案）：
#   滑鼠命中判定 `0x5AAE74` 對**敵對目標**往 out 參數寫 1（`0x5AB5D2`），
#   再由 `0x5AC8D5` 帶著它叫 TryAct。用法見 `attack.basic()`（掛機的「空格＝普攻」）。
KIND_ATTACK = 1
# 等對話框的期間每隔多久補叫一次 TryAct（＝官方狀態機那個 20 拍重試，我們做得比它勤）。
# 它同時也是「還沒走到就繼續往前走」的動力來源，所以不能只叫一次就乾等。
CLICK_REPEAT = 0.35
# ★ 出處同上段實測序列②：TALK_OPTION1＝對話第一個選項（商人＝我要買東西）。
TALK_BUY = 10
# ★ 對話選單的「第 N 項」碼：TALK_OPTION1=10 … TALK_OPTION10=19（擷取實測，
#   跟 TALK_BUY / TALK_BANK_USE 同一組機制）。**要送第幾項一律叫這支**，
#   不要再在別的模組寫一次 10（多寫一份就會有一份跟不上）。
TALK_OPTION1 = 10


def talk_option(n: int) -> int:
    """對話選單第 `n` 項（1 起算）的 talkaction 碼。"""
    return TALK_OPTION1 + (n - 1)

# ★ 出處：擷取的呼叫鏈跟「買東西」完全一樣＝對話第一個選項
#   （商人是買、維修商是修），所以也是 10。
TALK_REPAIR = 10
# 「全修」＝ repairall UI 指令（0x5906BD）呼叫的本體：thiscall，讀 WND_REPAIR 視窗的待修
#   清單、逐件送修裝包 0x3B。開維修視窗後直接叫它就全修（repairone 是 0x5D6320）。
#   ★ AOB 定位（locate.py supply.REPAIR_ALL_FN，錨在 push 2/push 0x3C 分家 repairone）。
#     送的是「修裝全部」包 opcode 0x3C（repairone 是逐件 0x3B）。反組譯 reports/repair_disasm.txt。
REPAIR_ALL_FN = 0x005D62C1
# 「關維修畫面」＝ repairclose UI 指令本體（0x5906CB）：找 WND_REPAIR、送「離開 NPC」包
#   0x5D29C1(0x22,0)。⚠ **修完不叫它角色會卡住不能走**（伺服器端還在維修互動狀態，
#   實測 2026-08-14 嵐狐）。純叫這支即可，它會自己判斷有沒有窗、送離開包、關窗。
#   ★ AOB 定位（locate.py supply.REPAIR_CLOSE_FN，錨在 push 0/push 0x22 的「離開」代碼）。
REPAIR_CLOSE_FN = 0x005906CB
# ★ 遊戲主物件的全域指標（talkaction 的 this）＝ gather.WORLD_PTR（0x9B669C）。
#   ⚠ 不在這裡再寫一份位址 —— 那個全域已登記 AOB（locate gather.WORLD_PTR，
#   交叉驗證兩處），用時直接讀 gather.WORLD_PTR 拿 warm() 之後的值。
# ★ 「離開 NPC 互動」包 ＝ 泛用送包(0x22, 0)。⚠ 送包函式**不寫死** ——
#   0x5D29C1 就是 attack.SELECT_FN（泛用送包 0x5D3D97）8/11 改版後的新位址
#   （reports/patch_doctor.txt 實證），AOB 特徵早就有，直接用 attack.SELECT_FN。
LEAVE_NPC_CODE = 0x22
# ★ 出處：實測序列①的結果——0x54A520 互動成功時客戶端開的對話框視窗（Lua 全域名）。
#   ⚠ 只能做「值變了」的邊沿偵測，不能當「非 0＝開著」（見 _dialog_token）；
#   讀不到時走安全退化（等停下＋固定等待照送）。
DIALOG_WND = "WND_MESSAGE"
DIALOG_TIMEOUT = 12.0      # 點 NPC 後等對話框的上限（0x54A520 會自己走過去，邊走邊等）
# ★★ **貼著 NPC 點下去的那種，等 3 秒就夠**（2026-08-27 使用者回報：
#   「點不到的時候會等很久才橋位置」）。人已經在 CLICK_RANGE 內時，對話框就是
#   一趟伺服器來回的事；DIALOG_TIMEOUT 那 12 秒是留給「從遠處點、客戶端自己
#   走過去」的。⚠ 差別在**人擠人的時候**：被別的玩家推著滑動 → `is_walking`
#   一直是 True → 底下那個「停住 0.8 秒就放棄」的快路徑永遠不觸發，舊寫法就
#   卡滿 12 秒才肯換站位（天使學園廣場實測）。所以要有這個硬上限。
# ★★ 2026-09-03 再壓到 1.2 秒：實測貼身叫 TryAct **0.13 秒**對話框就開，
#   而且 WND_MESSAGE 的代號**是那個視窗物件的位址**（同一個視窗重開會拿到
#   同一個值）→ 除了「這個 session 第一次開」以外根本看不到邊沿。等久了也等
#   不到，只是白等；等不到就照送選項（下面「驗不了」那條），成沒成看目標視窗。
DIALOG_NEAR_TIMEOUT = 1.2
DIALOG_STILL_GRACE = 0.8   # 角色停住這麼久還沒開對話框＝這次點沒成功，馬上去調位置
                           # （判太早無害：基準只記一次，晚到的對話下一輪立刻接上）
# ★ 走近 NPC 時「連續這麼久沒更靠近」＝被卡住了（人牆／伺服器退回移動）
#   → 不要磨到逾時，直接回去讓呼叫端發互動包（客戶端自己會再走一段）。
APPROACH_STALL = 3.0
# ★★★★ 2026-09-20 使用者定：「先自己走到 NPC 旁邊，然後再用官方；不用一定要走到
#   那個點位（可能被其他玩家佔住），靠近後不動了就可以換官方，防止卡住。」
#   → 看得到他但還講不到話時，每次自己走**一段**（這麼多秒）就回來補一發官方
#     TryAct；走不動（APPROACH_STALL 3 秒沒位移）本來就會提早回來。
APPROACH_STEP = 10.0
TALK_GAP = 1.2            # 對話選項之間**最多**等多久（8/14 盲等值；現在只是上限，
                           #   看到下一頁就送 —— 見 `_wait_page`）
TALK_STEP_FLOOR = 0.25     # 兩個對話動作之間的最小間隔（同一拍連送伺服器不吃）
WND_TIMEOUT = 3.0          # 最後一個選項送出後，輪詢目標視窗（販售/維修/倉庫）的上限
# ★★★★ 2026-09-20 使用者定的新規則（他第三次回報「補給的跟藥水商人又在來回踱步」）。
#   原話：「就直接走到藥水商人最近，不用管能不能走到，反正角色不動了就發送官方的，
#   然後讓官方自己走然後對話；如果 2 秒沒偵測到對話就再發送一次官方的，
#   直到 20 秒都不行再停下來。」→ 見 `_engage_npc`。
#   ⛔ 一起刪掉的舊東西（**不要再寫回來**）：地形圖挑「離 NPC 最近的可走格」
#     （`_walk_to_npc`／`_near_spots`／`_box_status`）、點不開就踩上 NPC／穿到
#     另一側換站位（`_nudge_toward`、NUDGE_STEPS）。那一整套就是他連續三次
#     回報的「一直上下前後走」「走出去又回去」「來回踱步」。
TALK_CLICK_GAP = 2.0       # 沒看到對話 → 每這麼久補送一次官方 TryAct
TALK_GIVE_UP = 20.0        # 看得到 NPC 之後這麼久還講不到話＝放棄（呼叫端通知＋停機）
WALK_TIMEOUT = 90.0        # 自己走到 NPC 的上限（秒）——銀行常在城另一頭（永夜城實測
                           #   離落點 176 格）；走到就提早返回，卡住更早（APPROACH_STALL）
# ★ 「20 秒都講不到話」的標記字樣：**訊息裡有這幾個字 ＝ 呼叫端要通知＋停機**
#   （使用者 2026-09-20 定）。⚠ 只有這一份，呼叫端不要自己另寫一組字串。
TALK_FAIL = "講不到話"
# ★★ 使用者 8/19 晚實機回報的 bug：**太遠就發互動包 → 人還沒走到對話框先開**，
#   這時送「我要買東西」伺服器不理（人不在旁邊）→ 販售頁開著卻買不了。
#   → 規則：**先自己走進 CLICK_RANGE 內才發互動包**；對話開了還要過 TALK_RANGE
#   的「人真的到了」閘門才送選項。
CLICK_RANGE = 2.5          # 發互動包前，先自己走到離 NPC 這麼近
TALK_RANGE = 4.0           # 送對話選項前的直線門檻 —— ⚠ 只剩 tile 讀不到時的備援，
                           #   正常路徑看 TALK_BOX_X/Y（_npc_in_box）
# ★★★ 真正的「講得到話」判定＝**tile 方框**（2026-09-06 黑狐 棕櫚基地 銀行實測，TryAct kind 2）：
#   tile＝世界座標/32 取整，|Δx|≤TALK_BOX_X 且 |Δy|≤TALK_BOX_Y 就開（0.1 秒），隔著櫃檯也開。
#   實測點（我的 tile − NPC tile）：開 (3,-2)(4,0)(5,0)(4,-2)(3,-3)(5,1)(5,3)；不開 (6,0)(7,0)(3,-4)。
#   ⚠ 反組譯 0x508DF6 是 AABB 相交（我框邊長＝[+0x1A4]+2·range），但 range 那張表沒讀出來，
#     兩軸為什麼不一樣也沒追；先用實測數字。舊的 kind 3 是 Chebyshev ≤2（另一組數字）。
TALK_BOX_X = 5
TALK_BOX_Y = 3
OFF_ACT_SIZE = 0x1A4       # 實體互動框邊長（TryAct 距離檢查用；NPC 實測 1）

# ★★★ 銀行存款（2026-08-14 擷取＋反組譯，見 memory self-supply-buy）
#   開倉庫序列（跟買/修同一套點 NPC → talkaction，只差選項碼）：
#     ① 點 NPC（0x54A520，同買/修）→ 開對話。
#     ② talkaction(11)「我要用倉庫」＝TALK_OPTION2（銀行選單第 2 項）。
#     ③ talkaction(10)「自己的倉庫」＝TALK_OPTION1（子選單第 1 項）→ WND_BANK 開。
#   ⚠ 兩個碼是擷取到的實際值（TALK_OPTION1=10…TALK_OPTION10=19 是選單位置碼）；
#     跟買東西「我要買東西=10」同一組機制。
TALK_BANK_USE = 11         # 我要用倉庫
# ★ 出處同上段擷取：TALK_OPTION1＝子選單第 1 項「自己的倉庫」（非公會）。
TALK_BANK_SELF = 10
BANK_WND = "WND_BANK"      # 倉庫視窗名（確認真的開了）
WND_SALE = "WND_NPCSALE"   # 買東西的販售視窗（確認交易真的開了）
WND_REPAIR = "WND_REPAIR"  # 修裝視窗（確認真的開了）
# 存款封包（反組譯 0x5D2988：push 0xB/push 0x2F/建包 0x50E0AE → 送 0x711380）：
#   代號 0x2F、內文 11 bytes = u16代號 + u8動作(0x11) + u32格號 + u32(0)
#   ★ 建/送/連線完全沿用 jumpmap.BUILD_FN/SEND_FN/CONN_PTR（跟買東西同一組，已 AOB）。
DEPOSIT_OPCODE = 0x2F
# ★ 出處同上反組譯 0x5D2988：內文 u8 動作碼，0x11＝存入。
DEPOSIT_ACTION = 0x11
# ★ 出處同上反組譯：push 0xB＝內文 11 bytes。
DEPOSIT_BODY = 11
# ★ 出處：補給頁「這些物品要怎麼處理」的平行清單（robot var 樹 DATAID，
#   dump_lua 全域常數對照）：AS_STRLIST_TODISCARD(1516)=物品**名字**、
#   AS_INTLIST_TODISCARD(1518)=**處理方式**。
#   方式：0 移除 / 1 存銀行 / 2 賣掉 / 3 丟棄（HANDLETYPE_*，遊戲 Lua 常數）。
#   我們只認「1=存銀行」的，背包有同名的就存。（不寫死物品，讀使用者設的那張。）
AS_HANDLE_NAMES = 1516
# ★ 出處同上：處理方式那張 int 清單（AS_INTLIST_TODISCARD）。
AS_HANDLE_TYPES = 1518
# ★ 出處同上：遊戲 Lua 常數 HANDLETYPE_*，1＝存銀行。
HANDLETYPE_DEPOSIT = 1
MAX_DEPOSIT = 120          # 一趟最多存幾件（防呆上限；正常一批物品遠少於此）
DEPOSIT_WAIT = 0.25        # 每存一件後等背包更新（會 poll 到真的少一件）
# ⚠ 自家的輪詢上限（不是遊戲常數）：存完最多 poll 幾次確認物品離開（×DEPOSIT_WAIT ≈ 1.5s）。
DEPOSIT_POLL = 6

# ★ 買入封包版面（擷取＋實測，見 memory self-supply-buy）：
#   u16 代號 + u32 件數 + 每件 { u32 全域種類id, u32 數量 }
_BODY_HEAD = 6
# ★ 出處同上：每件 8 bytes（種類id + 數量）。
_BODY_ENTRY = 8
SCRATCH_OFF = 0x180        # 相對 mover.scratch()；避開 jumpmap/lua/sell 用的區段
CALL_TIMEOUT = 1.0

# 商人物件欄位（結構偏移，改版才會壞；出處 self-supply-buy）
OFF_SELECT_ID = 0x1D0      # NPC 交給伺服器的選定 id（會變，每次現讀）
# ★ 出處同上（self-supply-buy 實測）：NPC 編號，對到 npc.xml / str_npc——用它精準認商人。
OFF_NPC_NUM = 0x1D8
# ★ 出處同上：NPC 名字（內嵌 UTF-8）；只拿來 debug/顯示。
OFF_NPC_NAME = 0x1DC

# ★ 各城的補給/維修商 **編號＋位置**（由 tools/build_supply_merchants.py 從 GAMEDATA/map 的
#   .MPC 自動抽，不手打）。認人**用編號**（+0x1D8，唯一確定）不用名字——
#   補給商各城名字統一但編號各異、維修商名字還不統一，一律靠這張表的確切編號比對。
#   結構：{ 場景: {"buy":(編號,x,y), "repair":(編號,x,y)} }；載不到就空 dict。
def _load_npc_table() -> dict[int, dict[str, tuple[int, int, int]]]:
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "assets" / "supply_merchants.json"
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return {int(k): {kind: (int(v[0]), int(v[1]), int(v[2]))
                         for kind, v in ent.items()}
                for k, ent in raw.items()}
    except Exception:                                      # noqa: BLE001
        return {}


NPC_TABLE = _load_npc_table()


# ★ 補給店販售表（tools/build_supply_shop.py 從 GAMEDATA 的 shop.xml＋item*.xml
#   自動抽，不手打）：{種類id: (單顆重量, 單顆價格)}。用途：
#   ① 掛機藥水見底時分辨「回城買不買得到」（買不到 → 通知＋停機，見 farm_tab）；
#   ② run_potion_fill 拿重量算「買到負重 95%」該買幾顆、拿價格做金幣封頂。
#   ⚠ NPC 販售清單不在可被動讀取的記憶體（開店時伺服器現送、只存 UI 視窗），
#     Item 範本的重量欄位偏移也還沒有反組譯出處 —— 照 CLAUDE.md 第 3 級規矩
#     走 build 工具；讀取端每輪拿實際負重／背包對帳，表過期只會買太少＋大聲說。
def _load_shop_table() -> dict[int, tuple[int, int]]:
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parents[2] / "assets" / "supply_shop.json"
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return {int(k): (int(v["w"]), int(v["p"]))
                for k, v in raw["items"].items()}
    except Exception:                                      # noqa: BLE001
        return {}


SHOP_TABLE = _load_shop_table()


def shop_sells(tid: int) -> bool:
    """補給店（藥水雜貨商人）賣不賣這種東西。

    表載不到（assets 缺檔）一律 False＝當「買不到」：安全退化 —— 寧可見底時
    通知＋停機，也不跑一趟注定買不到的補給（farm_tab 的訊息會標明表載不到）。
    """
    return int(tid) in SHOP_TABLE


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def _npc_name(scanner, addr: int) -> str:
    raw = scanner._read_bytes(addr + OFF_NPC_NAME, 24)
    if not raw:
        return ""
    try:
        return bytes(raw).split(b"\x00")[0].decode("utf-8")
    except UnicodeDecodeError:
        return ""


# ---------------------------------------------------------------------------
# 購買清單（讀補給頁那張，不寫死）
# ---------------------------------------------------------------------------
def read_buy_list(scanner) -> list[tuple[int, int]] | None:
    """回 [(種類id, 目標數量), …]；讀不到／兩張表不同步回 None。

    ★ 直接讀補給頁的 `AS_INTLIST_TOBUYID` / `AS_INTLIST_TOBUYNUM` 平行陣列
      （見 [[buy-list-intlist]]）—— 不寫死、使用者在補給頁設什麼就買什麼。
    """
    rec_i = robot._find_var(scanner, robot.AS_TOBUY_ID)
    rec_n = robot._find_var(scanner, robot.AS_TOBUY_NUM)
    if not isinstance(rec_i, int) or not isinstance(rec_n, int):
        return None                        # 樹讀不到／清單還沒建
    ids = robot._read_list(scanner, rec_i)
    nums = robot._read_list(scanner, rec_n)
    if ids is None or nums is None or len(ids) != len(nums):
        return None                        # 不同步 → 不敢用
    return [(int(i), int(n)) for i, n in zip(ids, nums) if i > 0 and n > 0]


# ---------------------------------------------------------------------------
# 背包現有數量
# ---------------------------------------------------------------------------
def bag_counts(scanner) -> dict[int, int] | None:
    """回 {種類id: 現有數量}；**整袋沒讀完就回 None**（不要當成「都沒有」）。

    ⚠ 半袋有東西也算讀不到：拿半袋去算「還缺幾個」會把缺口灌水、多買一堆
      （bag-false-empty-guards 那類誤報的變形）。
    """
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    out: dict[int, int] = {}
    for it in items:
        out[it.type_id] = out.get(it.type_id, 0) + it.count
    return out


# ---------------------------------------------------------------------------
# 找商人
# ---------------------------------------------------------------------------
def _scene_entities(scanner) -> list[int]:
    mgr = _u32(scanner, quickbar.MGR_PTR)
    scene = _u32(scanner, mgr + bag.OFF_SCENE_MGR) if mgr else 0
    table = _u32(scanner, scene + bag.OFF_ENT_TABLE) if scene else 0
    cap = _u32(scanner, scene + bag.OFF_ENT_CAP) if scene else 0
    if not table or not 0 < cap <= 0x10000:
        return []
    raw = scanner._read_bytes(table, cap * 4)
    if not raw:
        return []
    return [p for p in struct.unpack(f"<{cap}I", bytes(raw))
            if 0x10000 < p < 0x7FFF0000]


def find_npc(scanner, npc_id: int):
    """回離自己最近、**NPC 編號 (+0x1D8) == npc_id** 的那隻 (實體位址, 選定id)；找不到回 None。

    ★ 認人**用編號**（唯一確定；npc_id 是那城那隻商人的確切編號，來自 NPC_TABLE）。
      玩家的 +0x1D8 是大指標值，不會撞到 NPC 的小編號。
    ⚠ 場景實體表**只有附近的**（串流）——呼叫前要先走到 NPC_TABLE 那一格附近，
      NPC 才會出現在表裡讓這支認到。
    ⚠ 選定 id 會變（走遠再回來就換一個），呼叫端每次用前現讀，回傳只在這一拍有效。
    """
    me = bag.player_entity(scanner)
    if not me:
        return None

    def world_xy(addr):
        raw = scanner._read_bytes(addr + 0xC6, 8)   # +0xC6 X、+0xCA Y（int16，世界=格×32）
        if not raw:
            return None
        x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
        return x, y

    mp = world_xy(me)
    if mp is None:
        return None
    mx, my = mp
    best = None
    for e in _scene_entities(scanner):
        if e == me:
            continue
        if _u32(scanner, e + OFF_NPC_NUM) != npc_id:
            continue                            # 認編號（確切那隻）
        sel = _u32(scanner, e + OFF_SELECT_ID)
        if not 0x10000 < sel < 0x7FFF0000:
            continue
        p = world_xy(e)
        if p is None:
            continue
        d = abs(p[0] - mx) + abs(p[1] - my)
        if best is None or d < best[0]:
            best = (d, e, sel)
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------------------
# 開交易 + 買
# ---------------------------------------------------------------------------
def click_object(mover, scanner, ent_addr: int, kind: int = KIND_TALK) -> bool:
    """**官方點法**：對場上任何一個東西送一發 `TryAct(eid, kind)`。

    ★ **NPC 跟場景物件都走這一支**（雕像／公佈欄／副本的機器人／製作檯）——
      它們在同一張物件表裡，`+0xBC` 都查得到。實機 A/B 見 `produce.click`。
    ⚠ 中間我曾經因為「副本點不到」把物件那條退回自送 0x05 —— **那次的真兇是
      跳板被拆掉（IAT 指回原生 API，見 `move.Mover.installed`），不是這支**。
      事後乾淨重測：退到 5.5 格外只用這支，3 發之內走過去並開了對話。
      ⚠ 也順手排除了「它會把角色一直往目標拉」的疑慮：互動完
      `[pf+0x41A4]` 自己歸 0（實測連續取樣都是 0）。
    kind：3＝互動／講話。
    回 True 只代表**這一發送進去了**（TryAct 對 kind 3 的回傳恆為 1，不能當
    「成功了」用）；到底有沒有點到，看對話框／視窗有沒有變。
    """
    pf = move.pathfinder_this(scanner)
    if not (pf and TRY_ACT_FN and mover and mover.active):
        return False
    eid = _u32(scanner, ent_addr + 0xBC)
    if not eid:
        return False
    with mover.lock:
        return mover.call_sync(TRY_ACT_FN, eid, kind, ecx=pf,
                               timeout=CALL_TIMEOUT) is not None


def _click_npc(mover, scanner, npc_ent: int) -> bool:
    """點 NPC ＝ **跟滑鼠點同一件事**：叫官方的 `TryAct(eid, KIND_TALK)`。

    在互動範圍內 → 當場開對話；不在範圍 → 它用官方尋路幫我們走一步
    （呼叫端每 `CLICK_REPEAT` 秒再叫一次就會一路走到、然後開對話）。
    ⚠ 目標用 **eid**（實體 +0xBC），不是選定 id；this = pathfinder_this()。
    ⚠ 回 True 只代表**這一發送進去了**，不代表對話開了（見 TRY_ACT_FN 檔頭：
      kind 3 一律 `mov al,1` 收尾）。開沒開一律由 `_wait_dialog` 的邊沿偵測判。
    ⚠ TryAct 定位失敗才退回舊路（自動走路狀態機 INTERACT_FN）—— 那條會慢
      20 拍、動作鎖著時還會整包被延後，但總比整個功能停掉好。
    """
    if TRY_ACT_FN:
        return click_object(mover, scanner, npc_ent, kind=KIND_NPC)
    pf = move.pathfinder_this(scanner)
    if not pf:
        return False
    eid = _u32(scanner, npc_ent + 0xBC)
    if not eid:
        return False
    if not INTERACT_FN:               # 兩支都定位失敗（改版？）→ 大聲停用，不亂叫
        return False
    with mover.lock:
        return mover.call_sync(INTERACT_FN, INTERACT_MODE, eid, 0, ecx=pf,
                               timeout=CALL_TIMEOUT) is not None


def _talkaction(mover, scanner, code: int) -> bool:
    """送對話動作（talkaction，opcode 0x0B）。用 sell.TALK_FN（同一支，已登 AOB）。
    ⚠ 該函式進去就把 ecx 換成自己的區域緩衝，所以外面給的 ecx 不影響。"""
    if not sell.TALK_FN:
        return False
    with mover.lock:
        return mover.call_sync(sell.TALK_FN, code,
                               timeout=CALL_TIMEOUT) is not None


def _wnd_open(mover, scanner, name: str) -> bool:
    """某個視窗真的開了嗎（讀它的執行期代號，非 0 = 開著）。讀不到當「沒開」。

    ★ 2026-08-16 改**純讀**（`lua.globals_of` 走全域表雜湊節點，跟
      `produce.panel_open` 同一招）：舊寫法走 `lua.get_global` 要動 Lua
      堆疊，而補給一趟裡這支被叫十幾次、精靈主開關還開著 —— 正是崩潰
      dump 抓到的 Lua 競態場景。mover 參數留著不用（呼叫點多，不動簽名）。
    ⚠ 這裡「讀不到當沒開」是刻意的：呼叫端把「沒開」當成要重點 NPC 再試，
      多試一次無害；當成「開著」才會跳過必要步驟。
    """
    _ = mover
    try:
        g = lua.globals_of(scanner, (name,))
    except Exception:                                      # noqa: BLE001
        return False
    wnd = (g or {}).get(name)
    if not wnd:
        return False
    # ★★★ 2026-09-12：代號非 0 **不代表視窗開著** —— `WND_NPCSALE` 關掉不歸零，
    #   那正是 `close_sale` 檔頭寫的「假成功 → 下一趟直接跳過點 NPC、買不到卻
    #   一路回報成功」那個坑。現在有硬訊號了（`talkwnd.id_visible`＝視窗物件
    #   +0xB4，純讀、不佔指令槽），就真的問一句「畫面上有沒有」。
    #   ⚠ 回 None ＝**不知道**（偏移抄不到／改版改寫了 isvisible）→ 沿用舊判斷，
    #     ⛔ 不可以當成「沒開」（[[bag-false-empty-guards]]）。
    try:
        vis = talkwnd.id_visible(scanner, int(wnd) & 0xFFFFFFFF)
    except Exception:                                      # noqa: BLE001
        vis = None
    return True if vis is None else bool(vis)


def _ent_tile_f(scanner, ent: int):
    """場景實體的 tile 座標（浮點，+0xC6/+0xCA world=tile×32）；讀不到回 None。"""
    raw = scanner._read_bytes(ent + 0xC6, 8)
    if not raw:
        return None
    x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
    return (x / 32.0, y / 32.0)


def _npc_gap(scanner, npc_id: int):
    """自己到某 NPC 的直線距離（格）；讀不到回 None。"""
    pf = move.pathfinder_this(scanner)
    here = entity.read_pos(scanner, pf + 8) if pf else None
    found = find_npc(scanner, npc_id)
    nt = _ent_tile_f(scanner, found[0]) if found else None
    if not here or not nt:
        return None
    return math.hypot(here[0] - nt[0], here[1] - nt[1])


def _push_toward(mover, scanner, player_obj, tx: float, ty: float, nav=None):
    """往 (tx, ty) 走 —— **先用我們自己算的路徑**，回傳這趟用的 `Navigator`。

    ★★★★ 2026-09-20 晚使用者定（原話）：「**先用我們自己算路徑走到最近可以走到的
      地方，然後切官方的**」。所以順序是：
    ① **地形圖 A***（`navigate.Navigator`，純讀記憶體、不呼叫遊戲的尋路）。
       目標給 NPC 本人／`.MPC` 表座標就好 —— `terrain.route` 會把落在櫃檯裡的
       終點放寬到**離他最近的可走格**（`GOAL_RELAX`），那就是「最近可以走到的
       地方」。走到了（或走不動了）由呼叫端 `_approach_npc` 收工、`_engage_npc`
       切官方 `TryAct`。
       ⚠ 早上 e682764 把 `_walk_to_npc` 整支刪掉時連這一半也砍了，只剩官方尋路，
         而官方尋路**算不出長路就回 0**（實測 236 格直接回 0）→ 銀行／跨城的
         NPC 一步都走不出去（使用者：「他就是不動」）。
    ② 地形圖說走不到（`nav.stuck`）→ 才問官方 `walk_route`。
    ③ 官方也算不出來 → `walk_near` 直走一步當最後退路。
    ⛔ 這裡**沒有**「反覆重挑站位／點不開就換站位」—— 那才是他連三次回報的
      「來回踱步」，不准寫回來。目標一趟只有一個（NPC 本人）。
    """
    if nav is None:
        nav = navigate.Navigator()
    nav.step(scanner, mover, player_obj, tx, ty, arrive=CLICK_RANGE)
    if nav.stuck:
        if mover.walk_route(scanner, player_obj, tx, ty, stop_short=1.5) <= 0:
            mover.walk_near(scanner, player_obj, tx, ty, move.MIN_GAP)
    return nav


def _is_walking(scanner) -> bool:
    """角色現在正在走路嗎（讀不到當「沒在走」）。

    ★ 兩個地方要問：補點間隔（走路時踩油門 0.35 秒）、以及「要不要自己再走
      一段」—— 官方正在帶人走的時候 ⛔ 不要插手重下走路指令。
    """
    pf = move.pathfinder_this(scanner)
    return bool(pf and entity.is_walking(scanner, pf + 8))


def _act_size(scanner, ent: int) -> int:
    """實體的互動框邊長（[+0x1A4]）；讀不到／不合理當 1（NPC 與玩家實測都是 1）。"""
    v = _u32(scanner, ent + OFF_ACT_SIZE) if ent else 0
    return v if 0 < v < 16 else 1


def _in_talk_box(me_tile, me_size: int, npc_tile, npc_size: int) -> bool:
    """站在 me_tile 講不講得到 npc_tile（見 TALK_BOX_X/Y 的實測）。

    size 參數保留（NPC 與玩家實測都是 1，大一點的 NPC 沒遇過）；量到不一樣再接進來。
    """
    return (abs(me_tile[0] - npc_tile[0]) <= TALK_BOX_X
            and abs(me_tile[1] - npc_tile[1]) <= TALK_BOX_Y)


def _dist_to_npc(scanner, npc_id: int):
    """角色現在離某 NPC 幾格（給失敗訊息用，方便對距離）；算不出回 '?'。"""
    gap = _npc_gap(scanner, npc_id)
    return "?" if gap is None else round(gap, 1)


def _dialog_token(scanner):
    """讀 DIALOG_WND 全域**現值**（純讀）。讀不到表／名字不存在回 None。

    ⚠ 這個值**不能**當「開著沒有」用：8/19 掃 5 台分身，3 台掛機中沒在跟 NPC
      互動 WND_MESSAGE 也非 0（關窗殘值不歸零，跟 WND_NPCSALE 那批不同）。
      只能拿來當 `_wait_dialog` 的基準值做「變化」偵測。
    """
    try:
        g = lua.globals_of(scanner, (DIALOG_WND,))
    except Exception:                                      # noqa: BLE001
        return None
    if not g or DIALOG_WND not in g:
        return None
    # ★★★ 一律回**無符號**（2026-09-12 實機抓到的假邊沿）：這裡以前回 Lua 讀到的
    #   原值（有符號 -1712356784），而 `talkwnd.Page.wnd` 是 `& 0xFFFFFFFF` 的
    #   無符號（2582610512）—— 同一個代號、兩種表示法 → `_wait_dialog` 裡
    #   「代號變了」恆成立 → 每次點 NPC 都假宣告「對話開了」。
    try:
        return int(g[DIALOG_WND]) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return g[DIALOG_WND]


def _wait_dialog(scanner, baseline, timeout: float = DIALOG_TIMEOUT,
                 again=None, give_up_still: float | None = None,
                 page_base=_UNSET_PAGE, gap: float = CLICK_REPEAT) -> bool:
    """點 NPC 後等對話框**真的開了**：DIALOG_WND 變成「非 0 且 ≠ baseline」才算。

    ★ 用「值變了」不用「非 0」（見 _dialog_token 的坑）；視窗代號是遞增配號
      （同一視窗各台代號都不同），重開必拿新值，所以「和點之前不同」＝真開了新框。
    0x54A520 會自己把角色走到 NPC 旁才開對話 → 還在走就耐心等；
    停下來 DIALOG_STILL_GRACE 秒還沒開＝這次點沒成功（站著點的 flaky 案例），
    早點回 False 讓呼叫端調位置重點，不用傻等滿 timeout。

    ⚠⚠ 「還在走就耐心等」在**人擠人**的地方會失效：角色被別的玩家推著滑動，
      `is_walking` 一直是 True，快路徑永遠不觸發 → 傻等滿 timeout 才換站位
      （使用者 2026-08-27 回報）。所以貼身點的時候呼叫端要傳
      `DIALOG_NEAR_TIMEOUT`（3 秒）當硬上限，別靠這裡的走路判斷。

    page_base（2026-09-10 加）：**點下去之前**的對話頁簽章（`talkwnd.page().sig`）。
      ★★★ 為什麼要第二個訊號：`WND_MESSAGE` 的值是那個視窗**物件的位址**，同一個
        視窗重開會拿到同一個值 → 除了「這個 session 第一次開」以外根本看不到邊沿
        → 這支幾乎每次都回 False → 呼叫端把「驗不了」當成「沒開」→ 跑去換站位
        （踩上 NPC／穿到另一側重點）—— 使用者 2026-09-10 看到的「一直上下、前後走，
        雖然最後都會買到」就是這樣來的（那套換站位 2026-09-20 已整組刪掉）。
      ★ 頁面簽章（`MESSAGE_MSG_ID`／選項／圖像／代號）**新的一頁來了就會變**，
        是比位址邊沿硬得多的「對話真的開了」訊號。⛔ 但不能單獨當「現在開著」用
        （關掉之後那些值照樣留著，見 `talkwnd.page`）—— 這裡用的是**點之前 vs 點之後
        變了沒**，所以安全。沒傳就完全是舊行為。
    again（2026-09-03 加，配合 TryAct）：每 `CLICK_REPEAT` 秒補叫一次「點」。
      ★ 官方就是這樣做的（狀態機每 20 拍重叫一次 TryAct），而且**這也是走路的
        動力**：還沒到位的那幾發 TryAct 每一發都會往 NPC 走一步。
      ⚠ 有 again 的時候就**不再用「停住 0.8 秒就放棄」的快路徑** —— 那條是給
        「只點一次、乾等」用的；現在我們一直在重點，早退只會白白換站位。
    """
    if baseline is None:                 # 讀不到基準（改版把全域名換了？）→ 交給呼叫端安全退化
        return False
    pf = move.pathfinder_this(scanner)
    t0 = time.time()
    last_walk = t0
    last_click = t0                      # 進來之前呼叫端剛點過一次
    still_at, still_t = None, t0
    while time.time() - t0 < timeout:
        # ★★★★ 2026-09-12：**最硬的訊號先問** —— 對話視窗物件 +0xB4 那個
        #   「顯示中」旗標（`talkwnd.window_visible`，純讀、不佔指令槽）。
        #   以前沒有這個訊號，只能等「代號變了」或「頁面簽章變了」；而代號
        #   就是視窗物件的位址（同一個窗重開值一樣）→ 幾乎每次都判不出來 →
        #   貼身點明明 0.13 秒就開好了，卻要把 DIALOG_NEAR_TIMEOUT 磨滿才走
        #   「驗不了照送」那條（使用者 2026-09-12：「對話框不是馬上就會出現嗎，
        #   為何要等」）。⚠ 回 None ＝不知道 → 照舊看下面兩個訊號。
        vis = talkwnd.window_visible(scanner)
        if vis:
            return True
        # ★★★★ 2026-09-12 實機（雪狐 → 暴走穗海農場，兩趟都重現）：**旗標說 False
        #   就是畫面上真的沒有對話框 → 舊訊號一律讓位**。以前這裡是「旗標沒說開，
        #   那再看看代號／簽章」，結果是：
        #       4.01s  window_visible = False      ← 硬訊號說沒開
        #       4.01s  _wait_dialog = True（1ms）   ← ⛔ 代號邊沿卻說開了
        #   → 對著沒開的對話送掉三個選項（全落空）→ `wait_done` 磨滿 ENTER_WAIT
        #     20 秒 → 第二次點才真的開對話。33 秒裡有 20 秒是這樣白等的。
        #   真兇是代號的**表示法**：`_dialog_token` 回有符號（-1712356784）、
        #   `talkwnd.Page.wnd` 是無符號（2582610512）→ `now != baseline` 恆成立
        #   ＝每次點下去都宣告「開了」（已一起改成無符號，見 `_dialog_token`）。
        #   ⚠ 只有旗標回 **None（問不到）** 才回頭看代號／簽章那兩個軟訊號。
        if vis is None:
            # ★ 一次讀完（`talkwnd.page` 跟 `_dialog_token` 走同一條純讀 Lua 全域的路，
            #   順便把 WND_MESSAGE 的現值一起拿回來）。讀不到才退回只讀代號。
            pg = talkwnd.page(scanner) if page_base is not _UNSET_PAGE else None
            now = pg.wnd if pg is not None else _dialog_token(scanner)
            if now is not None and now != baseline and now != 0:
                return True
            # ★★ 第二個訊號：**新的一頁來了**（見 page_base 的說明）。
            if (pg is not None and page_base is not _UNSET_PAGE
                    and page_base is not None and pg.sig != page_base):
                return True
        if again is not None:
            # gap：多久補送一次「點」。可以給**函式**（每一拍現算）——
            # ★★★★ 2026-09-20 實機回報「等超久才走去」的真因就在這裡：TryAct
            #   那一發**同時是讓官方把人走過去的動力**（見上面 again 的說明）。
            #   人還沒到位時要用 CLICK_REPEAT(0.35s) 一直踩油門；站定了才換成
            #   TALK_CLICK_GAP(2s)（使用者：「角色不動了就發送官方的，2 秒沒偵測到
            #   對話就再發送一次」）。⛔ 不准一路用 2 秒 —— 那等於把油門收掉 6 倍。
            now_gap = gap() if callable(gap) else gap
            if time.time() - last_click >= now_gap:
                last_click = time.time()
                again()
            # ★ give_up_still（2026-09-06 棕櫚基地銀行實錄）：人在互動方框外時 TryAct 唯一的
            #   作用是「用官方尋路走一步」——位置一直沒動就是官方尋路也走不過去（人牆／櫃檯），
            #   再點 12 秒也只是白等（實測三輪 12 秒 30 發全空）。位置這麼久沒變就回去換站位。
            if give_up_still and pf:
                p = entity.read_pos(scanner, pf + 8)
                if p is not None and (still_at is None or math.hypot(
                        p[0] - still_at[0], p[1] - still_at[1]) > 0.3):
                    still_at, still_t = p, time.time()
                elif p is not None and time.time() - still_t > give_up_still:
                    return False
        elif pf and entity.is_walking(scanner, pf + 8):
            last_walk = time.time()
        elif time.time() - last_walk > DIALOG_STILL_GRACE:
            return False
        _nap(0.1)
    return False


def _wait_page(scanner, base_sig, timeout: float,
               floor: float = TALK_STEP_FLOOR) -> bool:
    """送完一個對話動作之後，等「**下一頁真的來了**」（頁面簽章變了）。

    回 True＝換頁了（可以送下一項）；False＝等到 `timeout` 還沒換頁（照舊送，
    跟以前睡滿 `TALK_GAP` 的行為一樣）。
    ★ `floor` 是最小間隔：同一拍連送兩個對話動作伺服器不吃（8/14 實測的
      「太快送會沒作用」），所以先睡這麼久再開始看。
    ⚠ 簽章讀不到（`page` 回 None）或沒有基準 → 退化成睡滿 `timeout`，
      ⛔ 不可以當成「換頁了」。
    """
    _nap(floor)
    if base_sig is None:
        _nap(max(0.0, timeout - floor))
        return False
    t0 = time.time()
    while time.time() - t0 < max(0.0, timeout - floor):
        pg = talkwnd.page(scanner)
        if pg is not None and pg.sig != base_sig:
            return True
        _nap(0.05)
    return False


def leave_npc(mover, scanner=None) -> None:
    """送「離開 NPC 互動」包 ＝ 泛用送包(0x22, 0)。

    ⚠⚠ **對話開著／交易開著時角色被伺服器鎖住不能走**（實測 walk 位移=0，
      見 `_bank_close`、`REPAIR_CLOSE_FN` 的說明）。所以任何「開了 NPC 對話但
      沒走完流程」的路徑收尾都要叫這支，不然人就卡在原地。
    ★ 送包走 `attack.SELECT_FN`（泛用送包，AOB 已定位）——**不准在別處寫死第二份**。
    ⚠⚠ **給了 scanner 就順手把對話框 destroy 掉**（2026-09-03 使用者實機卡死）：
      0x22 只是跟伺服器說「不講了」，畫面上那個對話框**不會消失**，`WND_MESSAGE`
      也一直指著那個視窗物件（id 還對得上、遊戲自己的 `ismessageend` 也回
      「還沒結束」）。留著的話**後面每一個用「有沒有對話視窗」判斷的功能都會被
      它騙**——刷副本就是這樣整趟卡死：以為對話已經開著 → 從頭到尾不去點物件 →
      一路按確定。收尾一定要 destroy（跟 `_bank_close` 叫 `DestroyBankWnd` 同理）。
    ⚠ 失敗不吵：這是收尾動作，叫不動最多是視窗還在，不該把整趟判成失敗。
    """
    try:
        if attack.SELECT_FN and mover and mover.active:
            with mover.lock:
                mover.call_sync(attack.SELECT_FN, LEAVE_NPC_CODE, 0,
                                timeout=CALL_TIMEOUT)
    except Exception:                                      # noqa: BLE001
        pass
    if scanner is None:
        return
    try:
        from app.game import talkwnd
        talkwnd.close_window(mover, scanner)
    except Exception:                                      # noqa: BLE001
        pass


def close_sale(mover, scanner) -> None:
    """跟商人買完的收尾：送離開包 **＋ 真的把畫面上那個「商店」框關掉**。

    ⚠⚠ 2026-09-03 實測到的**假成功**：只送 0x22（`leave_npc`）不會關掉販售視窗，
      `WND_NPCSALE` 就一直非 0 → **下一趟**補給進 `_engage_npc` 的第一句
      `if done(): return True` 直接命中 → **跳過點 NPC**（實測 0.04 秒回 True、
      人一步都沒動），接著 `buy()` 把購買包送進一個早就結束的交易 —— 什麼都
      沒買到卻一路回報成功。（黑狐 永夜城：開商店 → 送離開包 → 走開 → 再叫一次
      就重現。）這是 CLAUDE.md 說的「安靜地做錯事」，一律當 bug 修。
    ★ 關法照銀行那套（`_bank_close`）：送離開包 → 叫遊戲自己的視窗關閉處理式。
      `OnNPCSaleClose` 就是 X 鈕那支，無參數；叫完 WND_NPCSALE → 0（實測）。
    ⚠ 失敗不吵：這是收尾動作，叫不動最多是框還在，不該把整趟判成失敗。
    """
    leave_npc(mover, scanner)
    try:
        lua.call(mover, scanner, "OnNPCSaleClose")
    except Exception:                                      # noqa: BLE001
        pass


def _wait_for(ok, timeout: float) -> bool:
    """輪詢等 `ok()` 成立；成立馬上回 True（取代「睡滿固定秒數再看一眼」）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ok():
            return True
        _nap(0.1)
    return False


def _wait_wnd(mover, scanner, name: str, timeout: float) -> bool:
    """輪詢等某視窗開。"""
    return _wait_for(lambda: _wnd_open(mover, scanner, name), timeout)


def _wait_move_done(scanner, start_grace: float = 0.8, timeout: float = 4.0) -> None:
    """等「一段走位」真的結束。

    ⚠ 先等它**開始**走：下指令後 ~0.46s 狀態才變 Run（move.is_walking 檔頭實測），
      太早看是「還沒動」不是「走完了」——直接 _wait_still 會誤判已停、把同一步
      再走一次（看起來抖一下）。start_grace 內都沒開始走就直接返回，
      讓呼叫端用位移判斷這步到底有沒有生效。
    """
    pf = move.pathfinder_this(scanner)
    if not pf:
        return
    t0 = time.time()
    while time.time() - t0 < start_grace:            # 等開始走
        if entity.is_walking(scanner, pf + 8):
            break
        _nap(0.05)
    while time.time() - t0 < timeout:                # 等走完
        if not entity.is_walking(scanner, pf + 8):
            return
        _nap(0.1)


def _engage_npc(mover, scanner, npc_id: int, fallback, talk_codes, wnd_name: str,
                tries: int = 4, confirm=None,
                confirm_timeout: float | None = None) -> bool:
    """點 NPC 開對話、送對話選項、**確認對應視窗開了**。買／修／銀行共用這一支。

    ★★★★ 2026-09-20 使用者定（原話見 TALK_GIVE_UP 上面那段）——**先自己走到他
      旁邊，再用官方**：
        ① 自己往 NPC 本人走（`_approach_npc` ＝ 官方尋路）。⛔ 不挑「離他最近
           的可走格」、⛔ 不管走不走得到：「不用一定要走到那個點位，因為可能被
           其他玩家佔住，所以靠近後不動了就可以換官方，防止卡住」。
        ② 到了講話方框內（或走不動了）→ 送官方的 `TryAct`（＝滑鼠點他）：
           在互動範圍內當場開對話，不在範圍它自己再把最後一段走完。
        ③ 沒看到對話就補送（站定在框內 2 秒一發；人還在走 0.35 秒一發＝油門）。
        ④ **不再靠近他之後**滿 `TALK_GIVE_UP`（20 秒）還講不到話 → 記一筆
           `TALK_FAIL`、回 False，由呼叫端（掛機頁）**通知＋停機**。
        ⑤ 還是走不到／講不到 → `WALK_TIMEOUT`（90 秒）硬收工（同樣算失敗）。
    ⚠⚠ 碼錶 ⛔ 不准從「看到他」起算（2026-09-20 實機誤報：人還在半路走，
      20 秒一到就發通知＋停機）——**還在靠近他就重新算**，方框外站著不動、
      或已經站進方框才是真的「講不到話」。
    ⚠ 「還沒看到 NPC」也不算講不到話（跨城走一半而已）：那段給 `WALK_TIMEOUT`，
      繼續往 .MPC 表座標走；真的走完還看不到才算失敗。
    ⚠ 安全退化：對話框那個全域（`DIALOG_WND`）讀不到（改版換名）→ 退回
      「等人停穩＋固定等待」照送選項，最後一樣驗目標視窗 —— 名字失效只會
      變慢，不會整條斷掉。
    ⚠ talkaction 之間留間隔（太快送伺服器不吃，8/14 實測）；但不睡滿
      `TALK_GAP` —— 看到下一頁來了就送下一項（`_wait_page`）。
    ⚠⚠ 跨地圖回城後交易是「冷」的：**一定要先開真對話**，talkaction 才有效。

    confirm / confirm_timeout：給「成功不是開視窗、而是人被傳走」的 NPC 用
      （活動地圖入口）。不傳就跟買／修／銀行三條路一模一樣。
    tries：現在只當「對話開了、選項也送了，卻沒換到目標視窗」的重試上限。
    talk_codes: 依序送的對話碼（買=[10]、修=[10]、銀行=[11,10]）。
    """
    if not (mover and mover.active):
        return False
    # 成功判定：預設「那個視窗開了」；confirm 有給就用它（人被傳走那種）。
    done = confirm if confirm is not None else (
        lambda: _wnd_open(mover, scanner, wnd_name))
    wait_done = (lambda: _wait_for(done, confirm_timeout or WND_TIMEOUT))
    if done():                                       # 已經成功了就別重點
        return True
    # ① 自己走過去（只走這一段；走不到就算了，最後一段交給 TryAct）
    _approach_npc(mover, scanner, npc_id, fallback)
    # ★ 邊沿基準只在進場記**一次**（不是每輪重記）：判失敗判得早也無害——
    #   對話框晚一拍才到，下一輪 `_wait_dialog` 看到「值已經變了」立刻接上。
    base = _dialog_token(scanner)
    walk_t0 = time.time()      # 整段（走過去＋講話）的硬上限，見 WALK_TIMEOUT
    talk_t0 = None             # 「人已經不再靠近他了」才開始算 20 秒（見下）
    best = None                # 看過離他最近的距離（有縮短＝還在往他走）
    handed = False             # 已經「走不動了 → 交給官方」（⛔ 不再自己走）
    fails = 0                  # 對話開了、選項送了，卻沒換到視窗的次數
    why = f"{TALK_GIVE_UP:.0f} 秒都沒開對話"      # 失敗原因（break 時會改成實際那條）
    while True:
        if done():
            return True
        found = find_npc(scanner, npc_id)
        if not found:
            # 還沒串流進來（跨城走一半）→ 再往表座標走一段。⚠ 這不算講不到話。
            if time.time() - walk_t0 > WALK_TIMEOUT:
                _talk_failed("走到了還是看不到他")
                return done()
            _approach_npc(mover, scanner, npc_id, fallback)
            _nap(0.3)          # ⚠ 它可能瞬間就返回（讀不到座標）→ 這裡不睡會空轉燒 CPU
            continue
        # ★★★★ 2026-09-20 實機：使用者收到「講不到話」的**誤報**通知（＋停機）。
        #   真因兩個，都在這裡：
        #   ① 看得到他 **≠** 到得了他 —— NPC 一串流進來（20~30 格外）就開始跑
        #      20 秒碼錶，而那 20 秒全花在走過去的路上。
        #   ② 看得到他之後我們**完全不自己走了**，只靠 TryAct 讓官方帶人走。
        #   使用者定的規格（原話）：「先自己走到 NPC 旁邊，然後再用官方；不用一定要
        #   走到那個點位，因為可能被其他玩家佔住，所以靠近後不動了就可以換官方。」
        #   → 講話方框外：人沒在走就自己走一段（`_approach_npc`，不動 3 秒它自己
        #     會回來換官方）；只要還在**靠近**他，碼錶就重新算。
        #   ⚠ 座標讀不到（box is None）＝不知道 → 安全退化成舊行為（照樣點、照算）。
        if time.time() - walk_t0 > WALK_TIMEOUT:
            why = (f"走了 {WALK_TIMEOUT:.0f} 秒還是講不到話"
                   f"（離他 {_dist_to_npc(scanner, npc_id)} 格）")
            break
        if _npc_in_box(scanner, npc_id) is False:
            gap_now = _npc_gap(scanner, npc_id)
            if gap_now is not None and (best is None or gap_now < best - 0.5):
                # 還在靠近他 → 兩個碼錶都重新算（⚠ 跨城走一趟本來就要幾十秒，
                # 人在往他那邊移動就不是「走不到」也不是「講不到話」）
                best, talk_t0, handed = gap_now, None, False
                walk_t0 = time.time()
            if not handed and not _is_walking(scanner):  # ⛔ 官方正帶著走別插手
                _approach_npc(mover, scanner, npc_id, fallback,
                              timeout=APPROACH_STEP)
                after = _npc_gap(scanner, npc_id)
                # ★ 走完這一段還是沒更靠近＝走不動了（人牆／櫃檯）→ **換官方**，
                #   別在這裡磨（使用者：「靠近後不動了就可以換官方，防止卡住」）。
                if (gap_now is None or after is None
                        or after >= gap_now - 0.5):
                    handed = True
                _nap(0.1)      # ⚠ 它可能瞬間返回（讀不到座標）→ 不睡會空轉燒 CPU
                continue
        if talk_t0 is None:
            talk_t0 = time.time()
        left = TALK_GIVE_UP - (time.time() - talk_t0)
        if left <= 0:
            why = f"{TALK_GIVE_UP:.0f} 秒都沒開對話"
            break
        if fails >= tries:
            # 對話開得了、選項也送了，卻一直沒換到目標視窗 —— 再重試也一樣，
            # 一併走「通知＋停機」那條（訊息要講**實話**，不是「沒開對話」）。
            why = f"對話開得了，但 {tries} 次都沒換到{wnd_name or '目標視窗'}"
            break
        npc_ent, _ = found
        # ★ 點**之前**的對話頁簽章 —— 點完變了＝對話真的開了（見 `_wait_dialog`
        #   的 page_base）。⚠ 一定要在點之前讀，點完才讀就沒有「變了」可比。
        pg0 = talkwnd.page(scanner)
        page_base = pg0.sig if pg0 is not None else None
        if not _click_npc(mover, scanner, npc_ent):  # TryAct：到位就開對話，沒到就走一步
            _nap(0.3)
            continue

        # ★ 等對話框的期間補送 TryAct。⚠ 每一發都**重新找那隻 NPC**：實體會被
        #   回收／換一格，上一拍的位址不能信（CLAUDE.md「送出前當場重驗」）。
        def _again(_m=mover, _s=scanner, _id=npc_id):
            f = find_npc(_s, _id)
            return bool(f) and _click_npc(_m, _s, f[0])

        # ★★★★ 補點間隔要**看狀況**（2026-09-20 實機「等超久才走去」）：
        #   還在走路／還沒進講話方框 → CLICK_REPEAT(0.35s)，因為每一發 TryAct
        #   就是官方把人往前帶一步的動力；站定且在框內 → TALK_CLICK_GAP(2s)。
        def _gap(_s=scanner, _id=npc_id):
            if _is_walking(_s):
                return CLICK_REPEAT
            box = _npc_in_box(_s, _id)
            return TALK_CLICK_GAP if box else CLICK_REPEAT

        if base is None:                             # 安全退化：全域讀不到 → 盲等照送
            _wait_still(scanner, timeout=min(12.0, left))
            _nap(TALK_GAP)
        elif not _wait_dialog(scanner, base, left, again=_again,
                              page_base=page_base, gap=_gap):
            # ★★ 沒看到對話 **≠** 一定沒開：對話框**本來就開著**時代號一動也不動
            #   （2026-09-03 黑狐銀行實測）。「讀不到≠沒有」是本專案復發八次的坑
            #   （[[bag-false-empty-guards]]）→ 旗標沒有明說 False、代號又非 0、
            #   人也到位的話，照樣把選項送出去，成沒成一律交給最後那道視窗檢查。
            #   ⚠ 旗標明說 False（畫面上確定沒有對話框）就不送，那是 2026-09-12
            #     加的第一道閘：硬送只會落空然後磨滿確認逾時。
            if not (talkwnd.window_visible(scanner) is not False
                    and _dialog_token(scanner)
                    and _wait_arrival(scanner, npc_id, timeout=2.0)):
                continue                             # 時間到了 → 上面 left<=0 收工
        elif not _wait_arrival(scanner, npc_id, timeout=min(4.0, left)):
            fails += 1                               # 對話開了但人沒到位（被擋/太遠）
            continue                                 # → 絕不對著遠方的商人送選項
        for code in talk_codes[:-1]:
            # ⚠ 不睡滿 TALK_GAP：看到「下一頁真的來了」就送下一項（`_wait_page`），
            #   換頁本身就是「伺服器準備好了」的證據；等不到就照舊磨到 TALK_GAP。
            pg = talkwnd.page(scanner)
            _talkaction(mover, scanner, code)
            _wait_page(scanner, pg.sig if pg is not None else None, TALK_GAP)
        _talkaction(mover, scanner, talk_codes[-1])
        if wait_done():
            return True
        # 對話有開但最終視窗沒開（假邊沿／選項沒吃到）→ 刷新基準，下一輪要求
        # 新變化，免得同一個舊值一直當「已開」空轉送選項。
        fails += 1
        base = _dialog_token(scanner)
    if not done():
        _talk_failed(why)
        return False
    return True


def talk_to_npc(mover, scanner, npc_id: int, fallback, talk_codes,
                confirm, confirm_timeout: float, tries: int = 4) -> bool:
    """公開入口：點 NPC 開對話、依序送選項、用 `confirm()` 判成功。

    給「成功不是開一個視窗、而是人被傳走」的 NPC 用（活動地圖入口，見
    `app/game/eventmap.py`）。買/修/銀行三條路走的是同一支 `_engage_npc`，
    這裡只是把它的視窗判定換成呼叫端給的條件 —— 走近、點、沒開就每 2 秒補一發，
    **全部跟藥水雜貨商人同一套**（使用者 2026-08-27 要求）。

    ⛔ **呼叫端不要自己先用地形圖走過去**（NPC 櫃檯區會誤判走不到，天使學園
      廣場人擠人時更明顯）：走近交給裡面的 `_approach_npc`（官方尋路），
      最後一段交給 TryAct 自己收尾。
    """
    return _engage_npc(mover, scanner, npc_id, fallback, talk_codes, "",
                       tries=tries, confirm=confirm,
                       confirm_timeout=confirm_timeout)


def _approach_npc(mover, scanner, npc_id: int, fallback=None,
                  timeout: float = WALK_TIMEOUT) -> None:
    """自己走到 NPC 旁（`CLICK_RANGE` 內）—— ⛔ **不管走不走得到**。

    ★★★★ 2026-09-20 使用者定：「直接走到藥水商人最近，不用管能不能走到，
      反正角色不動了就發送官方的」。所以這裡只有一條路：**官方尋路**
      （`walk_route`，stop_short 留 1.5 免得撞進 NPC 本格）直接對 NPC 本人；
      他還沒串流進來就對 .MPC 表座標 `fallback` 走（只為把人帶進串流範圍）。
      ★ 走路手段（2026-09-20 晚使用者定）：**先用我們自己算的路徑**（地形圖
      A*，`navigate.Navigator`）走到離他最近可以走到的地方；地形圖說走不到
      才問官方尋路，最後才 `walk_near` 直走（見 `_push_toward`）。
      ⛔ 不挑「離他最近的可走格」、⛔ 不用地形圖判斷「走不走得到」再決定去不去、
      ⛔ 不換站位 —— 那一整套（`_walk_to_npc` 的挑格那半／`_nudge_toward`）
      就是使用者連三次回報的「來回踱步」，2026-09-20 刪了、不准寫回來。

    ★★★★ 2026-09-20 晚回歸修正（使用者：「他就是不動……應該是走路問題」）：
      早上 e682764 把 `_walk_to_npc` **整支**刪掉時，連「跨城長距離用地形圖
      A* 自己走」那一半也一起砍了，只剩官方尋路 —— 而官方尋路**算不出長路
      就回 0**（舊註解實測：236 格 `walk_route` 直接回 0）→ 銀行／跨城的 NPC
      一步都走不出去，站到逾時。藥水商人在附近所以看起來正常。
      → 這裡把 A* **只當走路手段**接回來：目標還是 NPC 本人／`.MPC` 表座標
        （`terrain.route` 自己會把落在櫃檯裡的終點放寬到最近可走格，
        `GOAL_RELAX`），⛔ 沒有任何「挑站位／換站位」的東西。
    ⚠ **走不動就別磨滿 timeout**（2026-08-27 使用者回報「點不到的時候會等很久
      才橋位置」）：連續 `APPROACH_STALL` 秒沒更靠近就回去，讓 `TryAct` 自己
      走最後一段（NPC 旁邊圍滿人時距離根本縮不了）。
    ⚠ 不回傳「到了沒」：夠不夠近不由這裡判 —— 呼叫端一律照樣送 TryAct。
    """
    if not (mover and mover.active):
        return
    t0 = time.time()
    was = None                       # 上次看到的位置
    moved_t = t0                     # 上次「人真的有動」是什麼時候
    nav = None                       # 官方尋路算不出路時才建（地形圖 A*）
    while time.time() - t0 < timeout:
        _abort_check()
        pf, here = _player_tile(scanner)
        if pf is None or here is None:
            return
        found = find_npc(scanner, npc_id)
        nt = _ent_tile_f(scanner, found[0]) if found else None
        if nt is None and fallback:
            nt = (float(fallback[0]), float(fallback[1]))
        if nt is None:
            return                   # 看不到他、也沒有表座標 → 沒得走
        gap = math.hypot(here[0] - nt[0], here[1] - nt[1])
        if found and gap <= CLICK_RANGE:
            return                   # 到了（⚠ 看得到他才算數）
        # ★★★★ 2026-09-20 實機「一直卡住」的真因：舊寫法是「**沒更靠近 0.5 格**
        #   就算卡住」，而這個迴圈一圈要等 `_wait_move_done`（最多 8 秒）——
        #   正常走一段只縮短 0.4 格也會被判成卡住、當場不走了。
        #   → 改成看**人有沒有在動**（位置變了就續命），跟使用者的說法一致：
        #     「反正角色不動了就發送官方的」。
        if was is None or math.hypot(here[0] - was[0], here[1] - was[1]) > 0.5:
            was, moved_t = here, time.time()
        elif time.time() - moved_t > APPROACH_STALL:   # 真的不動了 → 回去點
            return
        # 先走我們自己算的路徑（地形圖 A*）→ 走不到才官方尋路 → 最後直走（見 `_push_toward`）
        nav = _push_toward(mover, scanner, pf + 8, nt[0], nt[1], nav)
        _wait_move_done(scanner, timeout=8.0)


def _wait_arrival(scanner, npc_id: int, timeout: float = 8.0) -> bool:
    """對話框開了之後、送選項之前的**到位閘門**：等「人停下且離 NPC ≤ TALK_RANGE」。

    ★ 使用者 8/19 晚實機回報的 bug 的第二道保險：對話框比人先到時，這裡把
      「我要買東西」擋住等人走完；停了卻還離很遠（被擋住沒走到）＝這輪失敗，
      讓呼叫端調位置重來，**絕不對著遠方的商人送購買選項**。
    讀不到距離時放行（安全退化＝照舊送，跟舊版行為一致）。
    """
    pf = move.pathfinder_this(scanner)
    t0 = time.time()
    still_since = None
    while time.time() - t0 < timeout:
        if pf and entity.is_walking(scanner, pf + 8):
            still_since = None
        else:
            still_since = still_since or time.time()
            if time.time() - still_since > 0.3:          # 停穩了（不是走路中的頓拍；9/6 0.5→0.3）
                # ★ 到位＝在講話方框內（TALK_BOX_X/Y；伺服器實測從框內任何一格都吃選項，
                #   最遠 (5,3)＝5.81 格）。tile 讀不到才退回舊的直線 TALK_RANGE。
                box = _npc_in_box(scanner, npc_id)
                if box is not None:
                    return box
                gap = _npc_gap(scanner, npc_id)
                return gap is None or gap <= TALK_RANGE
        _nap(0.1)
    return False


def _npc_in_box(scanner, npc_id: int):
    """我現在站的 tile 講不講得到這隻 NPC（TALK_BOX_X/Y）；座標讀不到回 None。"""
    pf, here = _player_tile(scanner)
    nt = _npc_tile(scanner, npc_id)
    if pf is None or here is None or nt is None:
        return None
    return _in_talk_box((int(here[0]), int(here[1])), 1, nt, 1)


def _repair_all(mover, scanner) -> bool:
    """按「全修」（repairall 本體 0x5D62C1）：讀 WND_REPAIR 待修清單、逐件送修裝包。

    ⚠ this＝[gather.WORLD_PTR]（warm() 之後的值），呼叫前驗指標範圍 ——
      拿 0 或垃圾當 this 呼叫就是把遊戲弄當（memory-re-pitfalls 第一條）。
    """
    if not REPAIR_ALL_FN:
        return False
    main = _u32(scanner, gather.WORLD_PTR)
    if not 0x10000 < main < 0x7FFF0000:
        return False
    with mover.lock:
        return mover.call_sync(REPAIR_ALL_FN, ecx=main,
                               timeout=CALL_TIMEOUT) is not None


def _repair_close(mover, scanner) -> bool:
    """關維修畫面（repairclose 0x5906CB）：送「離開 NPC」包 0x5D29C1(0x22,0) 並關窗。
    ⚠ 不叫這支，修完角色會卡住不能走（伺服器端還在維修互動）。它自己讀全域、
      無參數（ecx 不影響）；沒開窗時它會自己 no-op，多叫無害。"""
    ok = False
    if REPAIR_CLOSE_FN:
        with mover.lock:
            ok = mover.call_sync(REPAIR_CLOSE_FN,
                                 timeout=CALL_TIMEOUT) is not None
    # ⚠⚠ repairclose **只送離開包，畫面上那個「修理」框不會關**（2026-09-03 截圖
    #   實證：叫兩次還在、WND_REPAIR 一直非 0）。留著的話下一趟 `_engage_npc`
    #   會被它騙成「維修視窗已經開著」→ 跳過點 NPC → 全修送進空的互動（同
    #   `close_sale` 檔頭那個假成功）。→ 補叫遊戲自己的 X 鈕處理式把它 destroy。
    #   ⚠ `OnRepairClose` **要帶視窗代號**（不帶會噴 repair.lua:7 bad argument）。
    try:
        w = (lua.globals_of(scanner, (WND_REPAIR,)) or {}).get(WND_REPAIR)
        if w:
            lua.call(mover, scanner, "OnRepairClose", w)
    except Exception:                                      # noqa: BLE001
        pass
    return ok


def run_repair(mover, scanner, npc_id: int, fallback) -> tuple[bool, str]:
    """在維修商人處修裝：走到 NPC → 我要修裝 → 全修。假設角色**已走到維修商附近**。

    npc_id: 這城維修商的確切編號（NPC_TABLE 給）；fallback: .MPC 表座標（重試靠近用）。
    ★ **無腦修**（使用者指定）：不看裝備狀態，一律送全修。全修一包就把全身補滿，
      沒壞的也不會出事，反正人都到維修商了，順手全部補滿。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not _engage_npc(mover, scanner, npc_id, fallback,
                       [TALK_REPAIR], WND_REPAIR):
        return False, "開維修視窗失敗（靠不夠近或對話碼不對）"
    _nap(0.3)
    if not _repair_all(mover, scanner):
        _repair_close(mover, scanner)          # 開了窗就算失敗也把它關掉，別卡住
        return False, "全修送不出去（指令槽忙）"
    _nap(0.8)
    _repair_close(mover, scanner)              # ⚠ 一定要關維修畫面，不然角色卡住不能走
    _nap(0.3)
    return True, "已送出全修"


# ---------------------------------------------------------------------------
# 銀行存款：把補給頁「處理列表」標成「儲存」的物品，存進自己的倉庫
# ---------------------------------------------------------------------------
def _read_strlist(scanner, rec: int) -> list[str] | None:
    """讀一個**字串清單**記錄的全部元素（UTF-8）。型別/筆數/指標對不上回 None。"""
    head = scanner._read_bytes(rec, robot._L_ELEMS)
    if not head or head[robot._V_TYPE] != robot.VAR_T_STRLIST:
        return None
    n = struct.unpack_from("<i", bytes(head), robot._L_COUNT)[0]
    if not 0 <= n <= robot._L_CAP:
        return None
    if n == 0:
        return []
    raw = scanner._read_bytes(rec + robot._L_ELEMS, n * 4)
    if not raw:
        return None
    out: list[str] = []
    for p in struct.unpack(f"<{n}I", bytes(raw)):
        if not 0x10000 < p < 0x7FFF0000:
            return None
        s = scanner._read_bytes(p, 128)
        if not s:
            return None
        try:
            out.append(bytes(s).split(b"\x00")[0].decode("utf-8"))
        except UnicodeDecodeError:
            return None
    return out


def deposit_targets(scanner) -> set[str] | None:
    """回「要存銀行」的物品名字集合（處理列表裡方式==1 存銀行的）。

    讀不到回 None（**不要當成沒有**）、清單還沒建或沒有標儲存的回空集合。
    ★ 直接讀使用者在補給頁設的那張處理列表（名字 1516 ∥ 方式 1518），不寫死。
    """
    rec_n = robot._find_var(scanner, AS_HANDLE_NAMES)
    rec_t = robot._find_var(scanner, AS_HANDLE_TYPES)
    if rec_n is robot._MISSING or rec_t is robot._MISSING:
        return set()                       # 清單還沒建 = 沒有要存的
    if not isinstance(rec_n, int) or not isinstance(rec_t, int):
        return None                        # 樹讀不到／只缺一張（不同步）
    names = _read_strlist(scanner, rec_n)
    types = robot._read_list(scanner, rec_t)
    if names is None or types is None or len(names) != len(types):
        return None                        # 兩張表對不上 → 不敢用
    return {nm for nm, ty in zip(names, types)
            if ty == HANDLETYPE_DEPOSIT and nm}


def pending_deposits(scanner, targets: set[str] | None):
    """背包裡名字在 targets 的物品清單。背包**沒讀完整袋**回 None、沒有回 []。

    ⚠ 半袋不算數：漏讀的那半袋裡可能正好是要存的東西，「讀了半袋＝沒有」
      會安靜地跳過存款（bag-false-empty-guards）。
    """
    if not targets:
        return []
    items, complete = bag.scan(scanner)
    if not complete:
        return None                        # 讀不到（別當成「沒有」）
    return [it for it in items if it.name in targets]


def _bank_close(mover, scanner) -> None:
    """關倉庫視窗＋**解鎖移動**。

    ⚠⚠ 開倉庫時角色被伺服器**鎖住不能走**（使用者：「關倉庫就可以移動了」，實測 walk 位移=0）——
      所以銀行做完一定要關窗解鎖，不然接下來走不到維修/補給商。做三件（照 OnCloseBank ＋離開包）：
      ① 送「離開 NPC 互動」包 ＝ 泛用送包(0x22, 0)（同修裝 repairclose 內部做的事）
      ② `game.closebank`（伺服器端關倉庫）③ `DestroyBankWnd`（客戶端關窗，WND_BANK→0）。
    ★ 送包走 `attack.SELECT_FN`（泛用送包，AOB 已定位）——擷取時的 0x5D29C1 就是它
      8/11 改版後的位址，**不准在這裡寫死第二份**（改版位移後呼叫舊位址＝跳進亂碼）。
    """
    leave_npc(mover, scanner)
    try:
        lua.call(mover, scanner, "game.closebank")
        lua.call(mover, scanner, "DestroyBankWnd")
    except Exception:                                      # noqa: BLE001
        pass


def deposit_slot(mover, scanner, slot: int) -> tuple[bool, str]:
    """送一包「存入倉庫」。slot = 物品格號（bag.Item.slot，＝物品自記 +0x25）。

    封包＝代號 0x2F、內文 11：u8 動作(0x11) + u32 格號 + u32(0)。建/送同買東西。
    """
    if not (jumpmap.BUILD_FN and jumpmap.SEND_FN):
        return False, "送包位址還沒定位（改版？先跑 patch_doctor）"
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(jumpmap.BUILD_FN, DEPOSIT_OPCODE, DEPOSIT_BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌）"
        data = _u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # data+0 代號已由建構函式寫；我們填 +2 動作、+3 格號、+7 保留 0。
        payload = struct.pack("<BII", DEPOSIT_ACTION, slot & 0xFFFFFFFF, 0)
        if not mover.write(data + 2, payload):
            return False, "寫封包內容失敗"
        conn = _u32(scanner, jumpmap.CONN_PTR)
        pkt = _u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(jumpmap.SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌）"
    return True, ""


def _item_gone(scanner, serial: int) -> bool:
    """這個序號的物品**確定**離開背包了嗎（存進去了序號就不在了）。

    ⚠ 背包**讀不到時回 False**（當「還在」），不要把讀取失敗誤當成「存好了」——
      否則暫時性讀空會謊報成功、還可能被當成「沒滿」跳過真正的滿倉判斷。
    ⚠ 半袋也算讀不到：漏讀的那半袋可能正好是它待的容器，「半袋裡沒看到」
      ≠「不見了」。
    """
    items, complete = bag.scan(scanner)
    if not complete:
        return False                       # 沒讀完整袋 → 不敢說它不見了
    return not any(it.serial == serial for it in items)


def run_bank(mover, scanner, npc_id: int, fallback) -> tuple[bool, str]:
    """在銀行存款：走到 NPC → 我要用倉庫 → 自己的倉庫 → 把背包裡「標記儲存」的物品都存進去。

    假設角色**已走到銀行 NPC 附近**。npc_id = 這城銀行的確切編號（NPC_TABLE 給）；
    fallback = .MPC 表座標（重試靠近用）。
    ★ 倉庫**可能滿**：送了存入封包但物品沒離開背包 = 滿了 → 關窗離開（使用者要求）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    targets = deposit_targets(scanner)
    if targets is None:
        return True, "處理清單讀不到，跳過銀行"
    if not targets:
        return True, "處理清單沒有標記儲存的物品"

    if not _engage_npc(mover, scanner, npc_id, fallback,
                       [TALK_BANK_USE, TALK_BANK_SELF], BANK_WND):
        _bank_close(mover, scanner)
        return False, (f"倉庫開不起來（停在離銀行約 {_dist_to_npc(scanner, npc_id)} 格；"
                       "靠不夠近或對話碼不對）")

    deposited = 0
    capped = False
    bag_lost = False
    for _ in range(MAX_DEPOSIT):
        pend = pending_deposits(scanner, targets)
        if pend is None:                       # 背包突然讀不到 → 停手（要說出來，別像做完了）
            bag_lost = True
            break
        if not pend:                           # 都存完了
            break
        it = pend[0]
        ok, msg = deposit_slot(mover, scanner, it.slot)
        if not ok:
            _bank_close(mover, scanner)
            return (deposited > 0), f"存款送不出去（{msg}）；已存 {deposited} 件"
        # poll 等它真的離開背包（避免網路延遲被誤判成「滿了」）
        left = False
        for _ in range(DEPOSIT_POLL):
            _nap(DEPOSIT_WAIT)
            if _item_gone(scanner, it.serial):
                left = True
                break
        if not left:
            # 送了封包東西卻沒存進去 ＝ 倉庫滿了 → 關窗離開
            _bank_close(mover, scanner)
            return (deposited > 0), (f"倉庫滿了（{it.name} 存不進去），"
                                     f"已存 {deposited} 件，關窗離開")
        deposited += 1
    else:
        # 迴圈跑滿 MAX_DEPOSIT 還沒 break = 可能還有沒存到（不靜默截斷）
        capped = bool(pending_deposits(scanner, targets))

    _bank_close(mover, scanner)
    tail = "（達單趟上限，還有沒存完的）" if capped else ""
    if bag_lost:
        tail = "（⚠ 背包讀不到，提前停手，可能還有沒存完的）"
    return True, f"存了 {deposited} 件到倉庫{tail}"


def run_bank_here(mover, scanner, say=None) -> tuple[bool, str]:
    """**就地**測銀行：讀目前地圖的銀行 NPC → 走過去 → 存「標記儲存」的物品。

    不回城、不修不買，單獨驗銀行這段（給測試鈕用）。假設角色**已在有銀行的城裡**。
    """
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"
    here = scene.current_id(scanner)
    if here is None:
        return False, "讀不到目前地圖"
    entry = NPC_TABLE.get(here)
    bank_npc = entry.get("bank") if entry else None
    if not bank_npc:
        return False, f"{scene.scene_name(here)} 沒有銀行 NPC（不在名單內，或這張圖沒銀行）"

    targets = deposit_targets(scanner)
    if targets is None:
        return False, "處理清單讀不到"
    if not targets:
        return True, "處理清單沒有標記儲存的物品（在補給頁把某物設成「儲存」再測）"
    pend = pending_deposits(scanner, targets)
    if pend is None:                     # ⚠ 讀不到≠沒有（bag-false-empty-guards）
        return False, "背包讀不到，先不動（避免把「讀不到」當成「沒有」）"
    if not pend:
        return True, f"背包沒有要存的東西（要存的：{'、'.join(sorted(targets))}）"

    bkid, bkx, bky = bank_npc
    note(f"背包有 {len(pend)} 件要存，走去銀行 ({bkx},{bky}) 開倉庫…")
    # ⚠ 不在這裡先自己走：走去銀行那段由 run_bank 內的 `_engage_npc` 負責
    #   （官方尋路走過去，最後一段交給 TryAct 自己走完）。
    return run_bank(mover, scanner, bkid, (bkx, bky))


def _wait_still(scanner, timeout: float = 3.0) -> None:
    """等角色停下（走完）；讀不到玩家物件就直接返回。"""
    pf = move.pathfinder_this(scanner)
    if not pf:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not entity.is_walking(scanner, pf + 8):
            return
        _nap(0.15)


def buy(mover, scanner, entries: list[tuple[int, int]]) -> tuple[bool, str]:
    """送一包買入。entries = [(種類id, 數量), …]，數量 > 0。"""
    rows = [(int(i), int(q)) for i, q in entries if int(q) > 0]
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not rows:
        return False, "沒有要買的東西"

    if not (jumpmap.BUILD_FN and jumpmap.SEND_FN):
        return False, "送包位址還沒定位（改版？先跑 patch_doctor）"
    body = _BODY_HEAD + _BODY_ENTRY * len(rows)
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(jumpmap.BUILD_FN, BUY_OPCODE, body, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌）"
        data = _u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # 代號由建構函式寫在 data+0；我們從 +2 接著寫件數與各件。
        payload = struct.pack("<I", len(rows))
        for tid, qty in rows:
            payload += struct.pack("<II", tid & 0xFFFFFFFF, qty & 0xFFFFFFFF)
        if not mover.write(data + 2, payload):
            return False, "寫封包內容失敗"
        conn = _u32(scanner, jumpmap.CONN_PTR)
        pkt = _u32(scanner, buf + 0xC)
        if not conn:                     # 重連/載圖時 [CONN_PTR]=0，SEND_FN 拿它去算會爆
            return False, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(jumpmap.SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌）"
    return True, ""


# ---------------------------------------------------------------------------
# 一次補給（商人購買）
# ---------------------------------------------------------------------------
MAX_ROUNDS = 6             # 買完對帳、還缺就再買幾輪（伺服器可能限一次買的量）
SETTLE = 0.8              # 送出後等背包更新再對帳


def run_buy(mover, scanner, npc_id: int, fallback, ledger=None) -> tuple[bool, str]:
    """跟商人買 —— **收尾一定把販售視窗關掉**（`close_sale` 檔頭的假成功）。"""
    try:
        return _run_buy(mover, scanner, npc_id, fallback, ledger)
    finally:
        if mover is not None and getattr(mover, "active", False):
            close_sale(mover, scanner)


def _run_buy(mover, scanner, npc_id: int, fallback, ledger=None) -> tuple[bool, str]:
    """跑一次「商人購買」：讀清單 → 開交易 → 把不足的買到目標數量。

    npc_id: 這城補給商（藥水雜貨商人）的確切編號（NPC_TABLE 給）；fallback: .MPC 表座標。
    回傳 (有沒有全部補齊, 給人看的說明)。假設角色**已經在補給商附近**（走到了）。

    ledger(種類id, 實收數量) 可選：購買記帳（掛機頁「購買紀錄」，2026-08-20）。
    ⚠ 回報的是**下一輪對帳時背包的實測差額**，不是送出的數量 ——
      送 50 只進 30 就記 30（金幣不夠／限量時兩者會不同）。
      中途「背包讀不到」提早返回的那一輪沒得對帳＝不記（寧可少記不亂記）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"

    want = read_buy_list(scanner)
    if want is None:
        return False, "讀不到補給頁的購買清單（清單還沒建？補給頁先設一項）"
    if not want:
        return True, "購買清單是空的，沒東西要買"

    lines = []
    last_need = last_have = None   # 上一輪送出的單、送出前的背包（記帳對帳用）

    def _settle(have_now: dict) -> None:
        """上一輪買的到帳了多少 → 記帳。跟「還缺多少」的對帳同一份資料。"""
        nonlocal last_need, last_have
        if ledger is not None and last_need:
            for tid, _q in last_need:
                got = have_now.get(tid, 0) - (last_have or {}).get(tid, 0)
                if got > 0:
                    ledger(tid, got)
        last_need = last_have = None

    for _ in range(MAX_ROUNDS):
        have = bag_counts(scanner)
        if have is None:
            return False, "背包讀不到，先不動（避免把「讀不到」當成「沒有」）"
        _settle(have)
        need = [(tid, keep - have.get(tid, 0))
                for tid, keep in want if keep - have.get(tid, 0) > 0]
        if not need:
            done = "、".join(f"{itemname.label(t)}={k}" for t, k in want)
            return True, f"都夠了：{done}"

        if not _engage_npc(mover, scanner, npc_id, fallback,
                           [TALK_BUY], WND_SALE):
            return False, "開交易失敗（靠不夠近或對話碼不對）"
        _nap(0.2)

        ok, msg = buy(mover, scanner, need)
        if not ok:
            return False, msg + "（" + "；".join(lines) + "）" if lines else msg
        lines.append("買 " + "、".join(
            f"{itemname.label(t)}×{q}" for t, q in need))
        last_need, last_have = need, have
        _nap(SETTLE)

    # 幾輪之後還沒補齊：回報現況（可能商人沒賣、或每次限量）
    have = bag_counts(scanner) or {}
    _settle(have)                  # 最後一輪買的也要記帳
    short = [f"{itemname.label(t)} 還缺 {keep - have.get(t, 0)}"
             for t, keep in want if keep - have.get(t, 0) > 0]
    return (not short), ("；".join(lines) + "；" + "、".join(short)
                         if short else "、".join(lines))


# ---------------------------------------------------------------------------
# 藥水買到負重 95%（2026-08-19 使用者要求）
# ---------------------------------------------------------------------------
# 掛機的回程補給不再看「購買清單」買藥水，改成：照精靈頁**放的那幾種藥水**，
# 在補給商這裡買到**負重 95%**，而且 HP／MP 補完的總數對齊同一個目標（差不多數量）。
FILL_PCT = 0.95            # 買到負重的這個比例（使用者指定 95%）
# 買→對帳 最多幾輪。⚠ 這只是「防空轉」的保險 —— 真正的出口是「買到目標」
#   或「這一輪零進貨」（買了背包沒動就大聲停手）。要買到 95% 負重常是幾百顆，
#   伺服器若限單筆數量就得靠多輪慢慢補，輪數不能小氣（離線模擬踩過：
#   單筆限 50 顆時 8 輪只補到 76%）。
FILL_ROUNDS = 40
PROBE_N = 10               # 第一輪先小買這麼多顆，實測「一顆多重」再放量 ——
                           #   表的重量若過期，一輪大買可能把人買到超載（超載走不動）
FILL_HARD_CAP = 5000       # ⚠ 目標數量防呆上限（不是遊戲常數）：重量/金幣都封不住
                           #   時（單顆重量 0 又讀不到金幣）別無限上綱


def _fill_target(budget: int, gold, groups: list[tuple[int, int, int]]) -> int:
    """算「補完之後每組各有幾顆」的目標 T（HP/MP 用同一個 T＝差不多數量）。

    groups = [(現有數量, 單顆重量, 單顆價格), …]（1~2 組）。找最大的整數 T 使
    Σ max(0, T−現有)×重量 ≤ budget 且 Σ max(0, T−現有)×價格 ≤ gold。
    gold=None＝金幣讀不到就不封頂 —— 買不起伺服器自然少賣，對帳看得到。
    """
    def fits(t: int) -> bool:
        if sum(max(0, t - h) * w for h, w, _ in groups) > budget:
            return False
        if gold is not None and \
                sum(max(0, t - h) * p for h, _, p in groups) > gold:
            return False
        return True

    top = max(h for h, _, _ in groups)
    ws = [w for _, w, _ in groups if w > 0]
    hi = min(top + (budget // min(ws) + 1 if ws else FILL_HARD_CAP),
             top + FILL_HARD_CAP)
    lo = 0
    while lo < hi:                         # fits 對 T 單調遞減 → 二分找最大可行
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _fill_frac(fill_pct) -> float:
    """把「掛機設定」給的百分比整數換成比例；None／壞值 → 模組預設 FILL_PCT（95%）。

    2026-09-06 使用者要求：買到多少負重改成掛機頁右上角「掛機設定」可調、全部分身
    共用（app/game/farmsettings.py）。這裡再夾一次是讀取端的合理性驗證 —— 傳錯
    型別或範圍不拿垃圾值去算購買量。
    """
    try:
        v = int(fill_pct)
    except (TypeError, ValueError):
        return FILL_PCT
    if v < 10 or v > 100:
        return FILL_PCT
    return v / 100.0


def run_potion_fill(mover, scanner, npc_id: int, fallback,
                    plan: dict, say=None, ledger=None,
                    fill_pct=None) -> tuple[bool, str]:
    """補藥水 —— **收尾一定把販售視窗關掉**（同 run_buy，見 `close_sale`）。

    fill_pct 可選：買到負重的百分比整數（掛機設定；None＝預設 95%）。
    """
    try:
        return _run_potion_fill(mover, scanner, npc_id, fallback, plan,
                                say, ledger, fill_pct)
    finally:
        if mover is not None and getattr(mover, "active", False):
            close_sale(mover, scanner)


def _run_potion_fill(mover, scanner, npc_id: int, fallback,
                     plan: dict, say=None, ledger=None,
                     fill_pct=None) -> tuple[bool, str]:
    """把精靈頁放的藥水**買到負重 95%**。假設角色已在補給商附近（跟 run_buy 同一站）。

    plan = {"HP": [種類id…], "MP": […]}（robot.potion_buy_ids 給的，只含「真藥水」）。
    每組挑**第一個補給店有賣**的種類來買；數量用 SHOP_TABLE 的重量算，但
    **第一輪只小買 PROBE_N 顆、實測 Δ負重÷Δ數量**之後才放量（表過期不會把人
    買到超載）；每輪重讀負重／背包／金幣對帳，買了沒進背包就大聲停手。

    ledger(種類id, 實收數量) 可選：購買記帳（同 run_buy —— 記的是本來就在算的
    `got`＝背包實測差額，不是送出量）。
    """
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"

    groups = []            # (標籤, 買哪種, 這組算「現有幾顆」要看的全部種類)
    skipped = []
    for what in ("HP", "MP"):
        ids = [int(t) for t in (plan or {}).get(what, [])]
        if not ids:
            continue
        buy_id = next((t for t in ids
                       if t in SHOP_TABLE and SHOP_TABLE[t][0] > 0), None)
        if buy_id is None:
            skipped.append(f"{what}藥水店裡沒賣（"
                           + "、".join(itemname.label(t) for t in ids) + "）")
            continue
        groups.append((what, buy_id, ids))
    if not groups:
        return (not skipped), ("；".join(skipped) if skipped else
                               "精靈頁沒放藥水，跳過")

    pf = move.pathfinder_this(scanner)
    if not pf:
        return False, "找不到玩家物件（負重讀不到），先不買"

    ids_of = {what: ids for what, _, ids in groups}
    frac = _fill_frac(fill_pct)      # 買到負重的比例（掛機設定；預設 95%）
    w_est: dict[int, float] = {}     # 買哪種 → 實測單顆重量（Δ負重÷Δ數量）
    bought = {what: 0 for what, _, _ in groups}
    left_note = ""
    for _ in range(FILL_ROUNDS):
        wgt = entity.weight(scanner, pf + 8)
        if wgt is None:
            return False, "負重讀不到，先不買（避免超載）"
        cur, cap = wgt
        budget = int(cap * frac) - cur
        have = bag_counts(scanner)
        if have is None:
            return False, "背包讀不到，先不買"
        gold = bag.gold(scanner)
        counts = {what: sum(have.get(t, 0) for t in ids)
                  for what, _, ids in groups}
        # 重量用實測值優先（第一輪過後就有）；還沒實測才用表
        trip = [(counts[what],
                 int(round(w_est.get(bid, SHOP_TABLE[bid][0]))),
                 SHOP_TABLE[bid][1]) for what, bid, _ in groups]
        target = _fill_target(max(0, budget), gold, trip)
        need = []
        for what, bid, _ in groups:
            n = target - counts[what]
            if n > 0:
                # 這種還沒實測過重量 → 先小買一批當探針
                need.append((what, bid, n if bid in w_est else min(n, PROBE_N)))
        if budget <= 0 or not need:
            left_note = ""
            break
        left_note = "、".join(f"{what}還缺 {n}" for what, _, n in need)

        if not _engage_npc(mover, scanner, npc_id, fallback,
                           [TALK_BUY], WND_SALE):
            return False, "開交易失敗（靠不夠近或對話碼不對）"
        _nap(0.2)
        moved = False
        for what, bid, qty in need:
            before_n = counts[what]
            ok, msg = buy(mover, scanner, [(bid, qty)])
            if not ok:
                return False, "藥水購買送不出去：" + msg
            _nap(SETTLE)
            after = bag_counts(scanner)
            wgt2 = entity.weight(scanner, pf + 8)
            if after is None:
                return False, "買完背包讀不到，先停手"
            got = sum(after.get(t, 0) for t in ids_of[what]) - before_n
            if got > 0:
                moved = True
                bought[what] += got
                if ledger is not None:
                    ledger(bid, got)   # 記帳：實收差額（跟 bought 同一個數）
                # 實測單顆重量（要買同一包前後的負重都讀得到才算）
                if wgt2 is not None and wgt2[0] > cur:
                    w_est[bid] = (wgt2[0] - cur) / got
            if wgt2 is not None:
                cur = wgt2[0]            # 下一種接著算（同一輪連買兩種）
        if not moved:
            # 送了但一顆都沒進來：金幣不夠／背包滿／其實沒賣 —— 別空轉燒輪次
            return False, ("藥水買了但背包數量沒動（金幣不夠？背包滿？）"
                           "——先停手" + ("；" + "；".join(skipped)
                                        if skipped else ""))
        note("補藥水中…" + "、".join(
            f"{what}+{bought[what]}" for what, _, _ in groups))
    else:
        # 輪次用完（沒 break）→ 最後一輪買完的帳還沒對，重算一次再回報缺額
        wgt = entity.weight(scanner, pf + 8)
        have = bag_counts(scanner)
        left_note = ""
        if wgt is not None and have is not None:
            budget = int(wgt[1] * frac) - wgt[0]
            counts = {what: sum(have.get(t, 0) for t in ids)
                      for what, _, ids in groups}
            trip = [(counts[what],
                     int(round(w_est.get(bid, SHOP_TABLE[bid][0]))),
                     SHOP_TABLE[bid][1]) for what, bid, _ in groups]
            target = _fill_target(max(0, budget), bag.gold(scanner), trip)
            left_note = "、".join(
                f"{what}還缺 {target - counts[what]}" for what, _, _ in groups
                if target - counts[what] > 0)

    wgt = entity.weight(scanner, pf + 8)
    pct = f"，負重 {wgt[0] * 100 // wgt[1]}%" if wgt else ""
    msg = "、".join(f"{what}藥水+{bought[what]}" for what, _, _ in groups) + pct
    if left_note:
        msg += f"（{FILL_ROUNDS} 輪後{left_note}）"
    if skipped:
        msg += "；" + "；".join(skipped)
    # 有組別買不到、或幾輪都沒補齊 → 整段算失敗（呼叫端要看得到）
    return (not skipped and not left_note), msg


# ---------------------------------------------------------------------------
# 完整流程：記錄練功點 → 天使之翼回城 → 查表走到商人 → 買 → 趴趴GO 跳回
# ---------------------------------------------------------------------------
# 買/修 商人的編號＋位置 = NPC_TABLE（模組頂端從 assets/supply_merchants.json 載入，
#   由 tools/build_supply_merchants.py 從 GAMEDATA/map 的 .MPC 自動抽）。

WING_WAIT = 12.0       # 回城後等地圖變的上限（秒）
# ★ 2026-09-05 雪狐實錄：兩趟補給都回「回城後地圖沒變」停機，人卻已經在主城、翼 50→48
#   —— 翼確實生效、只是換圖晚於 12 秒（慢載圖）。所以 12 秒沒變先看翼有沒有**少一張**：
#   少了＝傳送在路上，再多等這麼久；沒少＝那一下真的沒生效，照舊算失敗（不盲等）。
WING_WAIT_LATE = 20.0
JUMP_TRIES = 3         # 回程趴趴GO 最多重送幾次（★ 送出去≠到得了，見 run_full_supply）
# run_full_supply(back_to=STAY)：補完**不回程、留在城裡**（副本頁清單全部刷完、沒勾循環
# 那條：使用者 2026-09-07「補給完就好」）。⚠ 不是 None：None＝跳回出發當下站的地方。
STAY = "stay"
JUMP_WAIT = 10.0       # 每次送出後等落地的上限（秒）
def _player_tile(scanner):
    pf = move.pathfinder_this(scanner)
    if not pf:
        return None, None
    return pf, entity.read_pos(scanner, pf + 8)


def _wing_slot(scanner) -> tuple[int | None, bool]:
    """背包裡天使之翼（回程道具）的格號（物品自記 +0x25）。回 (格號, 這個結論可信嗎)：

        (格號, True)   找到了
        (None, True)   整條陣列**真的走完了**、一個都沒有
        (None, False)  這一拍背包讀不到／沒走完 —— ⛔ 不准當成「沒有」

    ⚠⚠ [[bag-false-empty-guards]] 第八次（2026-09-17 黑狐實錄）：無限塔第 63 步
      剛跑完那一拍問這裡，背包容器讀不到 → 舊版回 None → 呼叫端當成「背包沒有
      天使之翼」整趟補給放棄（背包其實有 50 個，純讀探針對過）→ 人沒回城又開下
      一場 → 3 秒後伺服器把人送出副本 →「地圖變了」停機六小時。
    ★ 「沒有」這個結論一定要 `count_by_types()` 並且看第二個值（那支自己的註解
      就是這樣寫的）—— `find_by_type()` 走的 `_walk()` 對截斷不吭聲。
    """
    h = bag.head(scanner)
    if not h:
        return None, False
    got = inventory.find_by_type(scanner, h[0], recall.RECALL_ITEM)
    if got:
        return got[0], True
    try:
        _cnt, complete = inventory.count_by_types(scanner, h[0],
                                                  [recall.RECALL_ITEM])
    except Exception:                                      # noqa: BLE001
        return None, False
    return None, bool(complete)


def _wing_count(scanner):
    """背包裡天使之翼**總數**（可疊物品散在幾格都加起來）；讀不到回 None。
    給「翼用掉了沒」當證據用（見 WING_WAIT_LATE）。"""
    h = bag.head(scanner)
    if not h:
        return None
    try:
        return int(inventory.count_by_type(scanner, h[0], recall.RECALL_ITEM))
    except Exception:                                      # noqa: BLE001
        return None


LAND_READY_WAIT = 15.0     # 落地後等「玩家物件＋整袋背包」讀得到的上限（秒）


HEAD_START_GAP = 1.5       # 落地後「先開始走」的補送間隔（沒在走就再送一發）


def _wait_ready(scanner, timeout: float = LAND_READY_WAIT,
                mover=None, head_to=None, say=None) -> bool:
    """換圖落地後等到「玩家物件讀得到座標、背包整袋讀得完」才回 True；逾時回 False。

    ⚠ 場景編號換了 ≠ 人站穩了：實測（黑狐 2026-09-06）翼用掉 0.5 秒場景就變、
      但玩家物件 NULL／背包半袋還要再一陣子。任何「落地就讀背包做決定」的地方
      都要先過這裡，不然就是 bag-false-empty-guards 復發（讀不到被當成沒有／整趟放棄）。

    ★★★★ head_to / mover（2026-09-20 使用者回報「回程後原地發呆超級久」）：
      這個閘擋的是**判斷**，不是腳 —— 走去商人根本不用背包。所以人的座標一讀
      得到就先往 head_to（第一站 NPC 的 .MPC 表座標）走，**背包邊走邊等**。
      沒在走就每 `HEAD_START_GAP` 秒補送一發（落地那一瞬間送的常常會被吃掉）。
      ⛔ 不傳 head_to 就完全是舊行為（只等，不走）。
    say(訊息) 可選：等的期間每隔一下回報一次，狀態列才不會看起來像當掉。
    """
    t0 = time.time()
    sent = 0.0
    told = 0.0
    nav = None                 # 官方尋路算不出路時才建（地形圖 A*）
    while time.time() - t0 < timeout:
        pf, here = _player_tile(scanner)
        if pf and here is not None:
            # ★ 先走起來（人讀得到就夠了，不等背包）
            if (head_to and mover is not None
                    and getattr(mover, "active", False)
                    and time.time() - sent >= HEAD_START_GAP
                    and not _is_walking(scanner)):
                sent = time.time()
                nav = _push_toward(mover, scanner, pf + 8,
                                   float(head_to[0]), float(head_to[1]), nav)
            _items, complete = bag.scan(scanner)
            if complete:
                return True
        if say and time.time() - told >= 2.0:
            told = time.time()
            say(f"等背包同步（{time.time() - t0:.0f} 秒）"
                + ("…人已經先走去第一站了" if head_to else "…"))
        _nap(0.3)
    return False


def _wait_map_change(scanner, from_map: int, timeout: float):
    """等地圖變成別的（回城）。回新地圖 id；逾時回 None。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = scene.current_id(scanner)
        if cur is not None and cur != from_map:
            return cur
        _nap(0.3)
    return None


def _npc_tile(scanner, npc_id: int):
    """NPC 現在的**真實 tile**（實體 +0xC6/+0xCA，world=tile×32）；看不到回 None。"""
    found = find_npc(scanner, npc_id)
    if not found:
        return None
    raw = scanner._read_bytes(found[0] + 0xC6, 8)
    if not raw:
        return None
    x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
    return (x // 32, y // 32)


def run_full_supply(mover, scanner, say=None,
                    back_to=None, potions=None,
                    potion_only: bool = False,
                    ledger=None, guild_items=None,
                    fill_pct=None, should_stop=None) -> tuple[bool, str]:
    """完整補給一趟（外殼：只管**中途叫停**，內容全在 `_full_supply`）。

    should_stop() 可選（2026-09-09 使用者要求）：回 True 就**當場中止**這一趟。
    掛機分頁傳的是「這趟還算不算數」——關掉「開始掛機」它就變 True，於是人不會
    再繼續走完整趟補給。中止時會先收尾（關商店視窗、送 0x22 離開 NPC 互動，
    ⛔ 不送會卡在互動狀態）再回 `(False, 中止原因)`；人停在中止的那個地方
    （通常是城裡），呼叫端自己決定要不要做什麼。
    ⚠ 沒傳 should_stop ＝ 完全是舊行為（生產／副本／練技那三條都還沒接）。
    """
    _ABORT.fn = should_stop
    _talk_clear()
    try:
        ok, msg = _full_supply(mover, scanner, say=say, back_to=back_to,
                               potions=potions, potion_only=potion_only,
                               ledger=ledger, guild_items=guild_items,
                               fill_pct=fill_pct)
        # ★★★ 有 NPC「20 秒都講不到話」→ 把 TALK_FAIL 放進訊息裡，呼叫端據此
        #   **通知＋停機**（使用者 2026-09-20 定）。⚠ 只有整趟失敗才報：
        #   途中某一步講不到話但後面補救成功了，那趟還是成功的，別去吵他。
        why = talk_failure()
        if not ok and why:
            return False, f"{TALK_FAIL}（{why}）：{msg}"
        return ok, msg
    except Aborted:
        _ABORT.fn = None            # ⛔ 收尾自己不能再被中斷
        try:
            close_sale(mover, scanner)
            leave_npc(mover, scanner)
        except Exception:                              # noqa: BLE001
            pass                    # 對話早就關了／分身沒了：收尾失敗無所謂
        if say:
            say("補給已中止")
        return False, "補給中止（呼叫端叫停）"
    finally:
        _ABORT.fn = None


def _full_supply(mover, scanner, say=None,
                 back_to=None, potions=None,
                 potion_only: bool = False,
                 ledger=None, guild_items=None,
                 fill_pct=None) -> tuple[bool, str]:
    """完整補給一趟。say(訊息) 可選，用來即時回報進度。

    fill_pct 可選：藥水買到負重的百分比整數（掛機頁「掛機設定」，全部分身共用；
    None＝預設 95%）。呼叫端在主執行緒用 farmsettings.fill_pct() 讀了帶進來。

    記錄地圖與座標 → 天使之翼回城 → 查表 →（有要存的才去）銀行存 → 修裝全修 →
    照清單買 → 趴趴GO 跳回原練功點。順序＝**銀行 → 修裝 → 買**（使用者要求）。
    回 (整趟有沒有成功, 說明)。

    back_to=(x, y, 場景編號) 可選：回程改跳回這個指定點，而不是出發當下站的
    地方 —— 掛機分頁的「記錄點」（＝巡邏點，2026-08-18 使用者要求）用這個；
    None ＝ 原行為（生產分頁照舊）；`STAY` ＝ 補完留在城裡不回程（副本頁）。

    potions={"HP": [種類id…], "MP": […]} 可選（robot.potion_buy_ids 給的）：
    買完購買清單後，照精靈頁放的藥水**買到負重 95%**（run_potion_fill，
    2026-08-19 使用者要求）。None＝不買藥水 —— 生產分頁**一定要留 None**，
    採集的負重就是產能，塞滿藥水等於廢了它。

    potion_only=True：**只跑補給商那一站**（清單購買＋藥水），不去銀行、
    不去維修商（2026-08-19「自動練技」使用者指定：水用完那趟只有藥水商人
    的部分）。清單購買留著是因為同一個 NPC 順手把天使之翼補回 50 張 ——
    每趟回城都燒一張翼，不補的話練技幾趟後就回不了城。

    ledger(商人標籤, 種類id, 實收數量) 可選：購買記帳（掛機頁「購買紀錄」，
    2026-08-20 使用者要求）。標籤這裡組好（「某某城補給商」），數量由
    run_buy／run_potion_fill 的背包對帳回報 —— 記的是真的進來幾個。

    guild_items（2026-09-06 使用者要求）：要存**公會倉庫**的物品種類 ID 集合
    （`guildbank.wanted()`，全部分身共用一張清單）。背包有清單上的東西才多講一次話
    開社團倉庫存進去（跟個人倉庫同一個銀行 NPC）；None／空＝不存。
    potion_only 那趟一樣不去銀行。
    """
    def note(m):
        # ★ 每個進度點都順便問一次「還要不要跑」（等待那邊是 _nap 在問）——
        #   兩個加起來，中止最慢就是一次 _nap 的間隔。
        _abort_check()
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"

    # 1. 記錄練功點（地圖 + 座標，回程要跳回這裡）
    # ⚠ here 是「現在人在哪」，只給回城的地圖變化偵測用；回程目標是
    #   start_map/start_pos —— back_to 指定時兩者可以不同張圖，不能混用
    #   （混用的話：人不在 start_map 上，_wait_map_change 第一拍就以為到城了）。
    here = scene.current_id(scanner)
    if here is None:
        return False, "讀不到目前地圖"
    start_map = here
    _, start_pos = _player_tile(scanner)
    stay = back_to is STAY or back_to == STAY
    if stay:
        back_to = None
    elif back_to is not None:
        start_map = int(back_to[2])
        start_pos = (float(back_to[0]), float(back_to[1]))
    # ★ 活動地圖（暴走穗海農場那種）不在趴趴GO 傳送表裡 —— 回程要走活動 NPC
    #   的對話選單（app/game/eventmap.py）。活動結束把那邊的 ROUTES 清掉，
    #   這裡就自動退回原本的趴趴GO 行為。
    from app.game import eventmap            # 避免模組載入期循環相依
    ev = None if stay else eventmap.for_scene(start_map)
    # ★ 傳 scanner：這一拍人還站在練功點那張圖上，所以挑傳送點時可以用
    #   記憶體那張地形算路徑（最準）；回程的落點常有好幾個，挑**走過去最短**
    #   的那個（2026-09-16，見 jumpmap.nearest）。
    back = None if (ev or stay) else jumpmap.nearest(
        start_map,
        start_pos[0] if start_pos else None,
        start_pos[1] if start_pos else None,
        scanner)
    note(f"記錄練功點：{scene.scene_name(start_map)}"
         + ("（補完留在城裡，不回程）" if stay else
            f"（回程：找{ev.npc_name}）" if ev else
            f"（回程點：{back.name}）" if back else "（⚠ 沒有回程傳送點）"))

    # 2. 天使之翼回城
    # ★ 已經站在補給城裡（死亡「回標記點」復活後、或人本來就在城裡）→ 不燒翼、不等換圖。
    #   2026-09-05 自動刷副本「死亡當成一場 → 復活回城 → 補給」要走這條；以前會卡在
    #   「回城後地圖沒變」整趟失敗。只認 NPC_TABLE 有的城 —— 不在表裡就照舊用翼。
    slot, sure = None, True
    if not NPC_TABLE.get(here):
        slot, sure = _wing_slot(scanner)
        if slot is None and not sure:
            # ★★ 讀不到 ≠ 沒有（[[bag-false-empty-guards]]，見 _wing_slot 的說明）：
            #   副本最後一步剛跑完那一拍問背包常常整袋讀不到，而且伺服器過幾秒
            #   就會把人送出副本 —— 等到「人站穩、整袋讀得完」再問一次，順便把
            #   地圖重讀（等的期間被送回城的話就不用燒翼了）。
            note("背包這一拍讀不到（剛換圖？）→ 等人站穩再問一次…")
            _wait_ready(scanner)
            now = scene.current_id(scanner)
            if now is not None and now != here:
                note(f"等的期間被送到「{scene.scene_name(now)}」了")
                here = now
            slot, sure = _wing_slot(scanner)
    if NPC_TABLE.get(here):
        home = here
        note(f"已在 {scene.scene_name(home)}，不用回城")
    else:
        if slot is None:
            return False, (f"背包沒有{itemname.label(recall.RECALL_ITEM)}（回程道具）"
                           if sure else
                           f"背包讀不到，問不出有沒有"
                           f"{itemname.label(recall.RECALL_ITEM)}（回程道具）")
        before = _wing_count(scanner)
        if not recall.use_item(mover, slot):
            return False, "回程道具送不出去"
        note("用天使之翼回城中…")
        home = _wait_map_change(scanner, here, WING_WAIT)
        if home is None:
            # ★ 12 秒沒換圖：翼少了一張＝傳送在路上（慢載圖），再等；沒少＝沒生效
            after = _wing_count(scanner)
            if before is not None and after is not None and after >= before:
                return False, "回城後地圖沒變、翼也沒少（回程道具沒生效）"
            note(f"翼用掉了但還沒換圖，再等 {WING_WAIT_LATE:.0f} 秒…")
            home = _wait_map_change(scanner, here, WING_WAIT_LATE)
            if home is None:
                return False, (f"翼用掉了但等了 {WING_WAIT + WING_WAIT_LATE:.0f} 秒"
                               "地圖還沒變（回程可能失敗）")
        note(f"回到 {scene.scene_name(home)}")
        # ★★ 落地要等「人跟背包都讀得到」才准動（2026-09-06 黑狐實跑探針）：
        #   翼用掉 0.5 秒場景編號就換了，但那一瞬間玩家物件還是 NULL、背包半袋
        #   → 舊寫法固定睡 1 秒就往下走 → 「背包讀不到，跳過銀行」＋「背包讀不到，
        #   先不動」→ 整趟什麼都沒做、翼白燒一張、趴趴GO 第一發也送在人還沒站穩
        #   的時候（10 秒沒落地才重送第二發）。這是 bag-false-empty-guards 那一族：
        #   讀取端擋住了沒做錯事，但呼叫端要等到讀得到再問，不是問一次就放棄整趟。
        # ★★★★ 2026-09-20 使用者回報「回程後原地發呆超級久」：落地後這一段原本
        #   是**乾等**「整袋背包讀得完」（最多 LAND_READY_WAIT 秒）才准往下，
        #   而走去商人根本不需要背包 —— 那個閘擋的是判斷（[[bag-false-empty-guards]]）
        #   不是腳。→ 人的座標一讀得到就**先往第一站走**，背包邊走邊等。
        #   第一站挑「這趟一定會去的那個」：練技那趟只有補給商，其餘先走維修商
        #   （無腦全修一定跑）—— 銀行是「有東西要存才去」，不拿它當起跑方向。
        _ent = NPC_TABLE.get(home) or {}
        _first = _ent.get("buy") if potion_only else (
            _ent.get("repair") or _ent.get("buy") or _ent.get("bank"))
        if not _wait_ready(scanner, mover=mover,
                           head_to=_first[1:] if _first else None, say=note):
            note(f"⚠ 落地 {LAND_READY_WAIT:.0f} 秒人／背包還讀不到，照舊往下（各步驟自己會擋）")

    # ── 回程收尾（★★ 不准射後不理）────────────────────────────
    # 「送出去≠到得了」是這個專案抓過的真根因（memory jump-back-channel-fix，
    # 0c56fb4 修的就是這型）。呼叫端一拿到結果就會重開採集／掛機，人還在城裡
    # 就重開＝把採集中心寫成「城的地圖＋練功區座標」，精靈直接迷路。
    # 所以送了要**等落地、驗地圖、沒到就重送**，確定回到練功圖才把結果交回去。
    # ⚠ 這裡是背景執行緒，重送設有限次（不回報比重試更糟）；還是沒到就大聲說，
    #   呼叫端看得到「人不在採集圖」自己接手。
    def _jump_back() -> tuple[bool, str]:
        if stay:                                   # 呼叫端要的就是留在城裡
            return True, "補完留在城裡（不回程）"
        # ★ 活動地圖：改走活動 NPC 的對話選單，而且回**原來那條分流**
        #   （start_map 帶著支流序號，eventmap.go_back 自己拆）。
        if ev is not None:
            return eventmap.go_back(mover, scanner, start_map, say=note)
        if back is None:
            return False, "⚠ 沒有回練功點的傳送點，留在城裡"
        for _ in range(JUMP_TRIES):
            jok, _jm = jumpmap.teleport(mover, scanner, back.jump_id)
            note(f"跳回 {scene.scene_name(start_map)}…")
            landed = _wait_map_change(scanner, home, JUMP_WAIT) if jok else None
            if landed is not None and scene.same_map(landed, start_map):
                return True, "已回到練功點"
            if not jok:
                _nap(1.0)          # 送不出去（槽忙）→ 喘口氣再重送
        return False, (f"⚠ 趴趴GO 送了 {JUMP_TRIES} 次都沒回到練功點"
                       "（人可能還在城裡）")

    # 3. 查表（GAMEDATA/map 抽出來的：這城的買/修/銀行 NPC 編號＋位置）
    entry = NPC_TABLE.get(home)
    if not entry:
        # ⚠ 翼已經用掉了 —— 就算辦不了事也要把人送回練功點，不能丟在城裡
        #   （丟著的話呼叫端一看裝備還壞就再觸發，每趟再燒一張翼）。
        _jok, _jmsg = _jump_back()
        return False, (f"{scene.scene_name(home)} 沒有補給/維修/銀行 NPC"
                       f"（不在名單內）；{_jmsg}")

    results = []

    # 4. 先存銀行（★排在修裝前面——使用者要求）：有銀行 + 背包有「處理列表標記儲存」
    #    的物品 → 走去存。**沒有要存的就不去銀行**（使用者要求）。
    # ⚠ 各步驟**不在這裡先自己走過去**：走近＋點 NPC 全部在 run_bank/run_repair/
    #   run_buy 內的 `_engage_npc`（官方尋路走到他身邊，最後一段交給 TryAct）。
    bank_npc = entry.get("bank")
    if potion_only:
        bank_npc = None          # 自動練技那趟：不去銀行（使用者指定）
    if bank_npc:
        _bank_targets = deposit_targets(scanner)
        _bank_pend = (pending_deposits(scanner, _bank_targets)
                      if _bank_targets else [])
        if _bank_targets is None:
            results.append("⚠ 處理清單讀不到，跳過銀行")
        elif _bank_pend is None:
            results.append("⚠ 背包讀不到，跳過銀行")
        elif _bank_pend:
            bkid, bkx, bky = bank_npc
            note(f"背包有 {len(_bank_pend)} 件要存，走去銀行 ({bkx},{bky}) 開倉庫…")
            _, kmsg = run_bank(mover, scanner, bkid, (bkx, bky))
            note("銀行：" + kmsg)
            results.append("銀行:" + kmsg)
        # ★ 公會（社團）倉庫（2026-09-06 使用者要求）：清單全部分身共用，背包有清單上的
        #   東西才走這段；跟個人倉庫同一個 NPC，再講一次話開社團倉庫（見 guildbank 檔頭）。
        #   滿了安靜關窗（使用者定：不通知），結果只寫進訊息。
        if guild_items:
            from app.game import guildbank       # 避免模組載入期循環相依
            _gpend = guildbank.pending(scanner, guild_items)
            if _gpend is None:
                results.append("⚠ 背包讀不到，跳過公會倉庫")
            elif _gpend:
                bkid, bkx, bky = bank_npc
                note(f"背包有 {len(_gpend)} 件要存公會倉庫，走去銀行 ({bkx},{bky})…")
                _, gmsg = guildbank.run(mover, scanner, bkid, (bkx, bky), guild_items)
                note("公會倉庫：" + gmsg)
                results.append("公會倉庫:" + gmsg)

    # 5. 修裝：有維修商就走去全修（★無腦修，不看裝備——使用者指定）
    # ★ 修裝的成敗要進整趟的結果：壞裝觸發的那趟若修裝失敗，回去馬上又會
    #   觸發下一趟（又燒一張翼）——呼叫端要看得到 False 才能踩煞車。
    rep = entry.get("repair")
    repair_ok = True
    if potion_only:
        pass                     # 自動練技那趟：不修裝（使用者指定），也不用警告沒維修商
    elif rep:
        rid, rx, ry = rep
        note(f"走去維修商 ({rx},{ry}) 全修…")
        repair_ok, rmsg = run_repair(mover, scanner, rid, (rx, ry))
        note("修裝：" + rmsg)
        results.append("修裝:" + rmsg)
    else:
        # ⚠ 大聲說：這城修不了裝。壞裝觸發的補給會一直回來，原因要看得到。
        results.append("⚠ 這城沒維修商（裝備修不了）")

    # 6. 再買：有補給商 → 走去照清單買
    buy_npc = entry.get("buy")
    bought_ok = True
    if buy_npc:
        bid, bx, by = buy_npc
        # 記帳的商人標籤在這裡組好（哪座城的補給商）；數量由買的兩步對帳回報。
        lg = None
        if ledger is not None:
            _shop = f"{scene.scene_name(home)}補給商"
            lg = (lambda tid, qty: ledger(_shop, tid, qty))
        note(f"走去補給商 ({bx},{by}) 開店購買…")
        bought_ok, bmsg = run_buy(mover, scanner, bid, (bx, by), ledger=lg)
        note(bmsg)
        results.append("購買:" + bmsg)
        # ★ 藥水買到負重 95%（2026-08-19 使用者要求）：排在清單購買**之後**，
        #   翼那 50 張先佔掉的重量會自動算進去（每輪都重讀實際負重）。
        if potions and any(potions.get(w) for w in ("HP", "MP")):
            note(f"補藥水到負重 {int(round(_fill_frac(fill_pct) * 100))}%…")
            pok, pmsg = run_potion_fill(mover, scanner, bid, (bx, by),
                                        potions, say=note, ledger=lg,
                                        fill_pct=fill_pct)
            note("藥水：" + pmsg)
            results.append("藥水:" + pmsg)
            bought_ok = bought_ok and pok
    else:
        results.append("這城沒補給商（不賣天使之翼）")

    # 7. 趴趴GO 跳回練功點（等落地＋重送，見 _jump_back）
    summary = "；".join(results)
    jok, jmsg = _jump_back()
    return (bought_ok and repair_ok and jok), f"{summary}；{jmsg}"
