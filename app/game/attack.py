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

ACTION_FN = 0x005DA8E5      # ①動作。f(玩家物件−8, 動作碼)
SELECT_FN = 0x005D3D97      # ②選定。f(0x0C, 目標實體ID)
CAST_FN = 0x00559EDA        # ③施放。f(技能ID, 目標實體ID, 0, 0, 0)
# ④攻擊指令。f(目標實體ID, u16=0)　cdecl —— 兩個參數都只寫進封包緩衝
#   （+2 / +6）並把目標寫進全域 0x9B67D4，沒有指標解參考，呼叫安全。
#
# ★★ 這包是**近戰物理技能打得動的關鍵**（2026-08-06 攔包對照抓到的）：
#   官方掛的戰士 30 秒送 11 次，跟「動作包」成對出現；我們只送動作＋施放時
#   雪狐貼臉 0.5 格連放 45 秒，血一滴不掉、MP 一格不扣 —— 施放整包被
#   伺服器忽略。法系（施法動作，如電擊術）沒有它照樣打得動，黑狐才會一直正常。
#   舊 A/B「只送動作＋施放 0 隻、加上它 3~4 隻」也吻合。
# ⚠ 這就是以前拆掉的「第三包 0x559FBE」—— 遊戲 8/4 改版後函式搬到
#   0x559EA0（當時「實跑還是卡」是因為那晚走位邏輯本身還一團亂，錯怪它了）。
THIRD_FN = 0x00559EA0
# ★ ②選定的種類碼。出處：攔包實錄（檔頭①）＋反組譯遊戲自己的送包點就是推 0xC。
SELECT_CODE = 0x0C
# 交戰心跳的泛用代碼（0x5D3D97 是泛用送包，見 [[generic-send-fn]]）。
# 攔包實錄（官方掛戰士）：交戰中每 0.4~0.6 秒送一發 (5, 0)，
# 去別隻怪的路上會停；參數恆為 0。呼叫點反組譯是一對開關（旗標=1 送 4、
# 否則送 5），前面有兩道狀態檢查 —— 語意像「戰鬥狀態同步」。
HEARTBEAT_CODE = 0x05
# ①的動作碼。⚠⚠ 一定要 1，不能 0 —— 攔包對照：遊戲自己打怪送的是 1
#   （近戰全程 1、法師第一下 1 之後 2），0 在任何一份攔包裡都沒出現過。
#   法系（施法動作）不看這個值所以送 0 也打得動，但**物理技能（攻擊動作，
#   例如雪狐的破甲劈擊）搭動作碼 0 會被伺服器整包忽略**：唯讀實測貼臉
#   0.5 格連放 45 秒，血一滴不掉、MP 一格不扣（施放根本沒被受理）。
#   這個值曾在 a64e752 修成 1，又被 cc955d2 的大 revert 退回 0 —— 別再退。
ACTION_CODE = 1

CALL_TIMEOUT = 0.12         # 每一包等它被主執行緒執行的上限（一幀約 16ms）


# ── 「施放後鎖定（＝進 CD）」環狀清單 ──────────────────────────────
# 位置：玩家實體（bag.player_entity 那個基準）+0x418。
# 出處：usequickkey 的拒放檢查鏈 0x5B87C5 → 0x5B8666 → 0x549CD9 →
#   實體 vtable+0x3C(0x507C10)，查的就是這條清單（清單裡有這個技能就拒放）。
# 2026-08-17 黑狐受控實驗（reports/cast_trace_experiment.txt）：
#   · 真條目：節點 +0x0C == 1、+0x10 == 技能ID、+0x14 == 15。
#     伺服器**受理**後 ~0.2-0.5 秒出現，存活＝技能的後置時間
#     （瞬移術Ⅳ 1400ms 分毫不差），到期自動消失。
#   · 快捷鍵路徑、封包對地路徑（cast_at）都會長條目 ——
#     **對地技能不寫「最近使用的技能」欄位（0x549CD9 那條），這是唯一訊號**。
#   · 每一台都常駐一顆雜訊節點：+0x0C == 257(0x101)（五台盤點全中）——
#     用 +0x0C == 1 過濾就乾淨。
#   · 後搖內重複施放被伺服器拒收：MP 不扣、**不長新條目** ——
#     所以「新條目出現」＝真的放出去了，拒收不會誤判。
# ⚠ 這是會被遊戲同時改動的清單，走訪要逐跳驗指標、封頂節點數；
#   讀壞這一拍就當「沒看到」，下一拍再讀（純讀，絕不影響遊戲）。
RING_OFF = 0x418
RING_FLAG_OFF = 0x0C        # == 1 才是真條目（常駐雜訊是 0x101）
RING_SID_OFF = 0x10         # 技能 ID
RING_MAX_NODES = 8


def casting_marks(scanner) -> list[tuple[int, int]] | None:
    """環清單裡現在的真條目 [(節點位址, 技能ID), …]；讀不到基準回 None。

    純讀。回 None ＝「這一拍看不到」（實體重建空窗／改版），呼叫端
    當「不知道」處理；回空清單＝真的沒有人在後搖中。
    節點位址給呼叫端做「施放前快照 → 施放後看**新**節點」的邊緣偵測 ——
    同一顆技能上一發的殘留條目（位址在快照裡）不會被當成這一發的證據。
    """
    import struct

    from app.game import bag

    ent = bag.player_entity(scanner)
    if not ent:
        return None

    def u32(addr: int) -> int | None:
        raw = scanner._read_bytes(addr, 4)
        if not raw or len(raw) < 4:
            return None
        return struct.unpack("<I", bytes(raw[:4]))[0]

    def sane(p: int | None) -> bool:
        return p is not None and 0x10000 <= p < 0x7FFF0000

    head = ent + RING_OFF
    first = u32(head)
    if not sane(first):
        return None
    out: list[tuple[int, int]] = []
    node, hops = first, 0
    while sane(node) and hops < RING_MAX_NODES and node != head:
        flag = u32(node + RING_FLAG_OFF)
        sid = u32(node + RING_SID_OFF)
        if flag == 1 and sid is not None and 1 <= sid <= 0x61A8:
            out.append((node, sid))
        node = u32(node)                    # next 指標在節點開頭（std::list）
        hops += 1
        if node == first:
            break
    return out


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


def heartbeat(mover) -> bool:
    """交戰心跳：泛用送包 (5, 0)。掛機交戰中每半秒左右補一發。

    官方掛全程都在送這包、我們完全沒送 —— 是攔包對照裡最後一個差異。
    """
    if not (mover and mover.active) or _yield_now(mover):
        return False
    return _send(mover, ((SELECT_FN, (HEARTBEAT_CODE, 0)),))


def select(mover, target_id: int) -> bool:
    """選定目標。**換目標時送一次就好**，不必每次攻擊都送。

    mover: 已 start() 的 move.Mover（我們借它的 PeekMessageA 跳板呼叫遊戲函式）
    """
    if not (mover and mover.active and target_id) or _yield_now(mover):
        return False
    return _send(mover, ((SELECT_FN, (SELECT_CODE, target_id)),))


def cast_at(mover, skill_id: int, target_id: int,
            tile_x: float, tile_y: float) -> bool:
    """**對地技能專用**：帶著格子座標送施放包（對象＝地面的那 856 個）。

    為什麼這種技能不能用 quickbar.use（按 F2）：遊戲會跳出「選範圍」的游標
    等使用者點地板，角色就站在那不動 —— 使用者實際遇到（爆彈狙擊、
    麻痺荊棘、瞬移術都是這類）。封包自己帶位置就不必經過那道 UI。

    ⚠ 座標要跟技能放在**同一發**裡，不要另外多送一發「目標 ID = 0 + 座標」
      的對地施放（舊實測：多送那一發時怪連續 3.3 秒零傷害）。
    ⚠ 只送這一包：不送動作包、也不傳 `pf`（玩家物件−8 的裸指標）——
      少一個會讓遊戲當場崩潰的破口（見 [[game-crash-root-causes]] 第②條）。
      順移實測就是「施放函式帶格子座標」一包搞定。
    """
    if not (mover and mover.active and skill_id) or _yield_now(mover):
        return False
    return _send(mover, ((CAST_FN, (skill_id, target_id,
                                    int(tile_x), int(tile_y), 0)),))


def strike(mover, pf_this: int, skill_id: int, target_id: int,
           tile_x: float = 0.0, tile_y: float = 0.0) -> bool:
    """打一下：動作 + 攻擊指令 + 施放。選定之後就一直重複，直到怪死掉。

    pf_this: move.pathfinder_this() 的結果 —— **玩家物件 −8**
    tile_x/tile_y: 收下但**不再送**（保留參數是為了呼叫端不用改）。

    ★★ 施放的座標一律送 (0, 0) —— 跟遊戲自己一模一樣。兩份官方攔包
      （2026-08-03 法師 `0x664627(技能,目標,0,0,0)`、2026-08-06 官方掛戰士
      `(0x345,目標,0,0)`）都是 0；帶目標座標是我們自己發明的。
      法系帶不帶座標都打得到（黑狐 3 對 3 實測），物理近戰**帶座標打不打得到
      從來沒單獨驗過** —— 補齊第三包後雪狐仍零傷害，座標是最後一個線上差異。
    ⚠ 代價：「把順移放在攻擊鍵」的玩法會廢掉（順移要座標才會動，見
      [[teleport-skill]]）。目前沒有角色這樣用；要救它得先認得出哪些技能
      是順移類，再對那幾個例外帶座標。
    ⚠ 座標議題的舊實測（帶座標 3:3 都打得到、多送一發對地施放會 3.3 秒
      零傷害）都是**黑狐（法系）**量的，別再拿來推論近戰。

    ⚠ 順序照官方攔包：動作 → 攻擊指令 → 施放。攻擊指令（THIRD_FN）少了
      物理近戰技能整包被伺服器忽略（雪狐貼臉 45 秒零傷害）；法系多送它
      也無妨 —— 官方戰士就是三種混著送。
    """
    del tile_x, tile_y            # 見上：座標一律 0，參數只是佔位
    if (not (mover and mover.active and pf_this and skill_id and target_id)
            or _yield_now(mover)):
        return False
    return _send(mover, ((ACTION_FN, (pf_this, ACTION_CODE)),
                         (THIRD_FN, (target_id, 0)),
                         (CAST_FN, (skill_id, target_id, 0, 0, 0))))
