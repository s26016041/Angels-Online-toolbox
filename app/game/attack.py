"""直接送出「完整攻擊」三連包，不必按鍵。

三連包（使用者攔到、本檔又自己攔一次確認參數）
------------------------------------------------
    ① 0x5DA9F4(玩家物件−8, 動作碼)      動作／位置同步
    ② 0x5D3EB5(0x0C, 目標實體ID)        選定目標
    ③ 0x559FF8(技能ID, 目標實體ID,0,0,0) 施放技能

⚠⚠ ①的第一個參數是 **玩家物件 −8**（= move.pathfinder_this()），
    **不是** entity.py 用 VT_PLAYER 掃到的那個位址。
    這是本專案踩過兩次的老坑：同一個物件有兩個 vtable、相隔 8 bytes，
    傳錯會當場讓遊戲崩潰。實測攔到的值就等於 pathfinder_this()。

為什麼要自己送 ②
-----------------
掛機時我們是**直接寫記憶體**選怪（entity.set_target），遊戲因此不會送「選定」
那一包 —— 實測攔到的只有 ①③。自己送三連包正好把它補上。

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


def send_trio(mover, pf_this: int, skill_id: int, target_id: int) -> bool:
    """照 ①②③ 的順序送出完整攻擊；有任何一包排不進去就回 False。

    mover: 已 start() 的 move.Mover（我們借它的 PeekMessageA 跳板呼叫遊戲函式）
    pf_this: move.pathfinder_this() 的結果 —— **玩家物件 −8**
    """
    if not (mover and mover.active and pf_this and skill_id and target_id):
        return False
    # ⚠ 指令槽只有一個，一定要一包一包等它做完才排下一包，
    #   否則後面那包會被 call() 擋掉（回 False），變成只送出前面幾包。
    # ⚠ 整組再抓一次 mover 的鎖：移動指令是別條執行緒下的，
    #   不能讓它插進 ①②③ 中間（順序錯了這三包就不成立）。
    with mover.lock:
        for fn, args in ((ACTION_FN, (pf_this, ACTION_CODE)),
                         (SELECT_FN, (SELECT_CODE, target_id)),
                         (CAST_FN, (skill_id, target_id, 0, 0, 0))):
            if mover.call_sync(fn, *args, timeout=CALL_TIMEOUT) is None:
                return False
    return True
