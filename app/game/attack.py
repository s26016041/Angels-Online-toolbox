"""直接送出「完整攻擊」三連包，不必按鍵。

攻擊的實際節奏（使用者攔下遊戲自己打怪的封包，照序號排出來的）
--------------------------------------------------------------
    ① 0x5D3EB5(0x0C, 目標實體ID)         選定目標　　**換目標時才送一次**
    ② 0x5DA9F4(玩家物件−8, 動作碼)       動作／位置同步 ┐ 之後就重複這兩包，
    ③ 0x559FF8(技能ID, 目標實體ID,0,0,0) 施放技能　　　┘ 直到怪死掉

證據：使用者攔到的分組計數 —— 選定那包在緩衝裡只有 **1 包**，
動作與施放各 **2 包**（封包 #13 → #16 → #17）。

⚠⚠ ①的第一個參數是 **玩家物件 −8**（= move.pathfinder_this()），
    **不是** entity.py 用 VT_PLAYER 掃到的那個位址。
    這是本專案踩過兩次的老坑：同一個物件有兩個 vtable、相隔 8 bytes，
    傳錯會當場讓遊戲崩潰。實測攔到的值就等於 pathfinder_this()。

為什麼要自己送「選定」
----------------------
掛機時我們是**直接寫記憶體**選怪（entity.set_target_id），遊戲因此不會送
「選定」那一包 —— 實測攔到的只有動作與施放。自己補送一次就跟遊戲一致了。

技能 ID 哪來
------------
`player.read_last_skill()`：按幾下那個 F 鍵之後讀「最近使用的技能 ID」欄位
（角色屬性基準 −0x50）。實測黑狐送 F2 後讀到 0x101，與這裡攔到的 ③ 第一個參數
完全一致 —— 所以不必叫使用者自己攔封包。
★ 學這個**不需要有怪**：雪狐全程沒有目標，F3 → 0x2E1、F5 → 0x279，
  與有目標時量到的一致。所以按下「開始掛機」就能先學好再開打。

⚠ 這裡只負責「送封包」。要不要送、打誰、在不在射程內，由呼叫端決定。
  遊戲的攻擊有冷卻，送太快沒有意義（實測純封包與送鍵的擊殺數 9:9）。
"""
from __future__ import annotations

ACTION_FN = 0x005DA9F4      # ①動作。f(玩家物件−8, 動作碼)
SELECT_FN = 0x005D3EB5      # ②選定。f(0x0C, 目標實體ID)
CAST_FN = 0x00559FF8        # ③施放。f(技能ID, 目標實體ID, 0, 0, 0)

SELECT_CODE = 0x0C          # ②的第一個參數（遊戲自己的程式碼就是推 0xC）
ACTION_CODE = 0             # ①的動作碼，實測攔到 0（另看過 5/7）

CALL_TIMEOUT = 0.12         # 每一包等它被主執行緒執行的上限（一幀約 16ms）


def _yield_now(mover) -> bool:
    """尋路／移動正在等指令槽 → 這一拍不打，把槽讓出去。

    ⚠⚠ 沒有這個讓路，攻擊會把指令槽佔到 82%（實測），
      掛機那邊就問不到「跟這隻怪之間有沒有障礙物」，
      於是站在原地打不到的位置一直空打，直到卡住偵測才換怪。
      少打一拍（約 50ms）換到正確的判斷，非常划算。
    """
    return bool(mover) and mover.slot_wanted


def _send(mover, calls) -> bool:
    """照順序送出一串呼叫；有任何一個排不進去就回 False。

    ⚠ 指令槽只有一個，一定要一個一個等它做完才排下一個，
      否則後面那個會被 call() 擋掉（回 False），變成只送出前面幾包。
    ⚠ 整串再抓一次 mover 的鎖：移動指令是別條執行緒下的，不能插進中間。
    """
    with mover.lock:
        for fn, args in calls:
            if mover.call_sync(fn, *args, timeout=CALL_TIMEOUT) is None:
                return False
    return True


def select(mover, target_id: int) -> bool:
    """選定目標。**換目標時送一次就好**，不必每次攻擊都送。

    mover: 已 start() 的 move.Mover（我們借它的 PeekMessageA 跳板呼叫遊戲函式）
    """
    if not (mover and mover.active and target_id) or _yield_now(mover):
        return False
    return _send(mover, ((SELECT_FN, (SELECT_CODE, target_id)),))


def strike(mover, pf_this: int, skill_id: int, target_id: int,
           tile_x: float | None = None, tile_y: float | None = None) -> bool:
    """打一下：動作 + 施放。選定之後就一直重複，直到怪死掉。

    pf_this: move.pathfinder_this() 的結果 —— **玩家物件 −8**
    tile_x/tile_y: 目標的格子座標。給了就**額外**送一發「對地施放」，
        涵蓋那種吃座標而不吃目標 ID 的技能。

    ⚠⚠ **兩種參數不能塞在同一發裡。** 施放封包的第 3、4 個參數是對地施放
      用的（順移就是這樣送，見 [[teleport-skill]]）；同一發裡既給目標 ID
      又給座標，攻擊會**完全失效**。決定性對照（黑狐，同一隻怪、同一個
      時間點、距離 1.9 格）：
          目標ID + 座標同一發 → 怪的血量甚至從 47 回升到 49（完全沒打到）
          只給目標 ID ×4      → 血 49 → 0，當場打死
      所以要涵蓋兩種技能就得**分成兩發**。

    ★ 順序：對地那發先送、**目標 ID 那發最後送**。
      萬一兩發會互相取消，後送的會贏 —— 而目標 ID 那發是實測有效的，
      要贏也是它贏。
    """
    if (not (mover and mover.active and pf_this and skill_id and target_id)
            or _yield_now(mover)):
        return False
    calls = [(ACTION_FN, (pf_this, ACTION_CODE))]
    if tile_x is not None and tile_y is not None:
        calls.append((CAST_FN, (skill_id, 0, int(tile_x), int(tile_y), 0)))
    calls.append((CAST_FN, (skill_id, target_id, 0, 0, 0)))
    return _send(mover, calls)
