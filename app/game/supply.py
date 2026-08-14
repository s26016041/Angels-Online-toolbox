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

import struct
import time

from app.game import (attack, bag, gather, quickbar, robot, itemname, scene,
                      entity, move, terrain, jumpmap, recall, inventory,
                      navigate, sell, lua)

# --- 位址：能沿用的都沿用已登記 AOB 的（改版自動跟上，呼叫時才讀值） -----
#   建包 / 送出 / 連線 → jumpmap.BUILD_FN / SEND_FN / CONN_PTR（0x50E0AE / 0x711380 / 0x9B6660）
#   talkaction        → sell.TALK_FN（0x5D9066，實測與買東西共用同一支）
#   只有「點 NPC 互動」是新的 → supply.INTERACT_FN，已登進 locate.py 由 warm() 定位。
BUY_OPCODE = 0x27          # 跟 NPC 買

# ★★★ 開交易的完整序列（2026-08-14 嵐狐實測跨圖冷交易 39→44 確認）：
#   ① 點 NPC＝呼叫**自動走路/互動狀態機** 0x54A520(this=pathfinder_this, 模式1, 目標eid, 0)
#      → 客戶端走到 NPC 旁自動互動、開對話框（WND_MESSAGE）。
#      ⚠ 手動用 WALK_FN 走過去**不會**觸發它；opcode 0x07 只是面向不是點擊。
#   ② talkaction(10)「我要買東西」→ 開販售頁（WND_NPCSALE）、伺服器端交易開啟。
#   ③ 買（0x27）。
#   ⚠ talkaction(10) 碼是對的，但**一定要先用 0x54A520 開真對話**才有效（少了它 talkaction 無效）。
INTERACT_FN = 0x0054A520   # ★ AOB 定位（locate.py supply.INTERACT_FN）：thiscall ecx=玩家物件−8
# ★ 出處同上段實測序列①：0x54A520 的第一參數，1＝走到 NPC 並互動（開對話）。
INTERACT_MODE = 1
# ★ 出處同上段實測序列②：TALK_OPTION1＝對話第一個選項（商人＝我要買東西）。
TALK_BUY = 10
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
DIALOG_WAIT = 1.3          # 點 NPC 後等對話框跳出來
SALE_WAIT = 0.8            # talkaction 後等販售頁/伺服器交易開起來

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
def _click_npc(mover, scanner, npc_ent: int) -> bool:
    """點 NPC：呼叫自動走路/互動狀態機（0x54A520），客戶端會走到它旁邊互動、開對話。

    ⚠ 目標用 **eid**（實體 +0xBC），不是選定 id；this = pathfinder_this()（玩家物件−8）。
    """
    if not INTERACT_FN:               # 定位失敗（改版？）→ 大聲停用，不亂叫
        return False
    pf = move.pathfinder_this(scanner)
    if not pf:
        return False
    eid = _u32(scanner, npc_ent + 0xBC)
    if not eid:
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
    """某個視窗真的開了嗎（讀它的執行期代號，非 0 = 開著）。讀不到當「沒開」。"""
    try:
        v = lua.get_global(mover, scanner, name)
    except Exception:                                      # noqa: BLE001
        return False
    return isinstance(v, (int, float)) and int(v) != 0


def _ent_tile_f(scanner, ent: int):
    """場景實體的 tile 座標（浮點，+0xC6/+0xCA world=tile×32）；讀不到回 None。"""
    raw = scanner._read_bytes(ent + 0xC6, 8)
    if not raw:
        return None
    x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
    return (x / 32.0, y / 32.0)


def _dist_to_npc(scanner, npc_id: int):
    """角色現在離某 NPC 幾格（給失敗訊息用，方便對距離）；算不出回 '?'。"""
    pf = move.pathfinder_this(scanner)
    here = entity.read_pos(scanner, pf + 8) if pf else None
    found = find_npc(scanner, npc_id)
    nt = _ent_tile_f(scanner, found[0]) if found else None
    if not here or not nt:
        return "?"
    return round(abs(here[0] - nt[0]) + abs(here[1] - nt[1]), 1)


def _engage_npc(mover, scanner, npc_id: int, fallback, talk_codes, wnd_name: str,
                tries: int = 4) -> bool:
    """點 NPC 開對話、送對話選項、**確認對應視窗開了**；沒開就重試。
    **銀行／修裝／買東西共用這一支**，只差 `talk_codes` 與 `wnd_name`。

    ★★★ 關鍵（2026-08-14 借白狐跳板逐步實測定案）：**`0x54A520` 自己會走到 NPC 旁開對話**
      （實測從 3 格外點下去就走過去、開對話框），所以**我們不該再自己算貼身格走位**——
      先前自己用 path_to 探點＋走位反而把角色移動干擾了開對話（實測那版 _engage 回 False 跑 68s，
      而「站著不動只 click→talk11→talk10」一次就開）。
    ⚠ **時序要放夠**：click 後要等 0x54A520 把角色走到、對話框冒出來（~1.5s）才送 talkaction；
      talkaction 之間也要等夠（~1.2s）——太快送對話會沒作用（實測）。
    ⚠⚠ 跨地圖回城後交易是「冷」的：**一定要先用 0x54A520 開真對話**，talkaction 才有效。
    talk_codes: 依序送的對話碼（買=[10]、修=[10]、銀行「我要用倉庫→自己的倉庫」=[11,10]）。
    """
    if not (mover and mover.active):
        return False
    if _wnd_open(mover, scanner, wnd_name):          # 已經開著就別重點
        return True
    for _ in range(tries):
        found = find_npc(scanner, npc_id)
        if not found:                                # NPC 不在視野 → 用地形圖靠近再試
            _walk_to_npc(mover, scanner, npc_id, fallback, 30.0)
            continue
        _walk_close(mover, scanner, npc_id)          # ★ 遊戲尋路貼到 NPC ~1 格（見下）
        found = find_npc(scanner, npc_id) or found
        npc_ent, _ = found
        if not _click_npc(mover, scanner, npc_ent):  # 0x54A520 開對話
            time.sleep(0.5)
            continue
        time.sleep(0.6)                              # 讓 0x54A520 開始走
        _wait_still(scanner, timeout=12.0)           # 等它走到 NPC 旁停下
        time.sleep(1.2)                              # ★ 等對話框冒出來（太快送 talkaction 會沒用）
        for code in talk_codes:
            _talkaction(mover, scanner, code)
            time.sleep(1.2)                          # ★ 每個對話選項之間等夠
        if _wnd_open(mover, scanner, wnd_name):
            return True
    return _wnd_open(mover, scanner, wnd_name)


def _walk_route_to(mover, scanner, tx: float, ty: float, want: float,
                   timeout: float, stop_short: float = 0.0) -> bool:
    """用遊戲尋路 walk_route 走到 (tx,ty) 到 want 格內。到了回 True。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        pf, here = _player_tile(scanner)
        if pf is None or here is None:
            time.sleep(0.3)
            continue
        if abs(here[0] - tx) + abs(here[1] - ty) <= want:
            return True
        mover.walk_route(scanner, pf + 8, float(tx), float(ty),
                         stop_short=stop_short)
        time.sleep(0.4)
    return False


def _walk_close(mover, scanner, npc_id: int, want: float = 1.6,
                timeout: float = 15.0) -> bool:
    """★★★ 用**遊戲自己的尋路** `walk_route` 貼到 NPC 旁 ~1 格，**確保是「剛走近」狀態**。到了回 True。

    ⚠⚠ 兩個實測鐵則（2026-08-14 借白狐跳板逐步定案）：
    ① **不用地形圖（terrain.py）走貼身**：地形圖在 NPC 櫃檯附近會誤判「走不到」、只停在 2~4 格外；
       **遊戲尋路 path_to 說 NPC 周圍每格都走得到**，walk_route 能穩定貼到 1 格。stop_short 留一點免撞 NPC 本格。
    ② **點 NPC 開對話要在「角色剛走近」時送才穩定**（站著不動時點＝flaky，實測 ~50%；先退4格再走近＝4/4）。
       → 若角色已經太近（沒有 approach 動作），**先退到 ~4 格再走回來**，製造「剛走近」狀態。
    """
    found = find_npc(scanner, npc_id)
    nt = _ent_tile_f(scanner, found[0]) if found else None
    if nt is None:
        return False
    _, here = _player_tile(scanner)
    # ② 已經太近（沒 approach 動作）→ 先退到 ~4 格（挑 path_to 走得到的方向）
    if here and abs(here[0] - nt[0]) + abs(here[1] - nt[1]) < 3.5:
        for ox, oy in ((4, 0), (0, 4), (-4, 0), (0, -4),
                       (4, 4), (-4, -4), (4, -4), (-4, 4)):
            bx, by = nt[0] + ox, nt[1] + oy
            if mover.path_to(scanner, bx, by) > 0:
                _walk_route_to(mover, scanner, bx, by, 1.2, 8.0)
                break
    # 走近（fresh approach）到 want 格內
    if _walk_route_to(mover, scanner, nt[0], nt[1], want, timeout, stop_short=1.5):
        return True
    _, here = _player_tile(scanner)
    found = find_npc(scanner, npc_id)
    nt = _ent_tile_f(scanner, found[0]) if found else None
    return (here is not None and nt is not None
            and abs(here[0] - nt[0]) + abs(here[1] - nt[1]) <= want + 0.6)


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
    if not REPAIR_CLOSE_FN:
        return False
    with mover.lock:
        return mover.call_sync(REPAIR_CLOSE_FN, timeout=CALL_TIMEOUT) is not None


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
    time.sleep(0.3)
    if not _repair_all(mover, scanner):
        _repair_close(mover, scanner)          # 開了窗就算失敗也把它關掉，別卡住
        return False, "全修送不出去（指令槽忙）"
    time.sleep(0.8)
    _repair_close(mover, scanner)              # ⚠ 一定要關維修畫面，不然角色卡住不能走
    time.sleep(0.3)
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
    try:
        if attack.SELECT_FN:
            with mover.lock:
                mover.call_sync(attack.SELECT_FN, LEAVE_NPC_CODE, 0,
                                timeout=CALL_TIMEOUT)
    except Exception:                                      # noqa: BLE001
        pass
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
            time.sleep(DEPOSIT_WAIT)
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
    # ⚠ 不在這裡先用地形圖 _walk_to_npc（它在銀行櫃檯區會誤判「走不到」）——
    #   run_bank 內的 _engage_npc→_walk_close 會用**遊戲尋路**貼到 NPC 旁，那才走得到。
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
        time.sleep(0.15)


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


def run_buy(mover, scanner, npc_id: int, fallback) -> tuple[bool, str]:
    """跑一次「商人購買」：讀清單 → 開交易 → 把不足的買到目標數量。

    npc_id: 這城補給商（藥水雜貨商人）的確切編號（NPC_TABLE 給）；fallback: .MPC 表座標。
    回傳 (有沒有全部補齊, 給人看的說明)。假設角色**已經在補給商附近**（走到了）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"

    want = read_buy_list(scanner)
    if want is None:
        return False, "讀不到補給頁的購買清單（清單還沒建？補給頁先設一項）"
    if not want:
        return True, "購買清單是空的，沒東西要買"

    lines = []
    for _ in range(MAX_ROUNDS):
        have = bag_counts(scanner)
        if have is None:
            return False, "背包讀不到，先不動（避免把「讀不到」當成「沒有」）"
        need = [(tid, keep - have.get(tid, 0))
                for tid, keep in want if keep - have.get(tid, 0) > 0]
        if not need:
            done = "、".join(f"{itemname.label(t)}={k}" for t, k in want)
            return True, f"都夠了：{done}"

        if not _engage_npc(mover, scanner, npc_id, fallback,
                           [TALK_BUY], WND_SALE):
            return False, "開交易失敗（靠不夠近或對話碼不對）"
        time.sleep(0.2)

        ok, msg = buy(mover, scanner, need)
        if not ok:
            return False, msg + "（" + "；".join(lines) + "）" if lines else msg
        lines.append("買 " + "、".join(
            f"{itemname.label(t)}×{q}" for t, q in need))
        time.sleep(SETTLE)

    # 幾輪之後還沒補齊：回報現況（可能商人沒賣、或每次限量）
    have = bag_counts(scanner) or {}
    short = [f"{itemname.label(t)} 還缺 {keep - have.get(t, 0)}"
             for t, keep in want if keep - have.get(t, 0) > 0]
    return (not short), ("；".join(lines) + "；" + "、".join(short)
                         if short else "、".join(lines))


# ---------------------------------------------------------------------------
# 完整流程：記錄練功點 → 天使之翼回城 → 查表走到商人 → 買 → 趴趴GO 跳回
# ---------------------------------------------------------------------------
# 買/修 商人的編號＋位置 = NPC_TABLE（模組頂端從 assets/supply_merchants.json 載入，
#   由 tools/build_supply_merchants.py 從 GAMEDATA/map 的 .MPC 自動抽）。

WING_WAIT = 12.0       # 回城後等地圖變的上限（秒）
JUMP_TRIES = 3         # 回程趴趴GO 最多重送幾次（★ 送出去≠到得了，見 run_full_supply）
JUMP_WAIT = 10.0       # 每次送出後等落地的上限（秒）
WALK_TIMEOUT = 90.0    # 走到 NPC 的上限（秒）——銀行常在城另一頭（永夜城實測離落點 176 格），
                       #   放寬保險；正常走到就提早返回，只有真的走不到才等滿
ARRIVE_TILES = 2       # 離商人這麼近就算真的到了
# ★ 主城人多，navigate 常被玩家擋在最後幾格。但 0x54A520 點 NPC 會自己走完
#   最後那段再互動，所以走到「夠近」就交給它收尾（不必硬擠到 2 格）。
NEAR_ENOUGH = 8


def _player_tile(scanner):
    pf = move.pathfinder_this(scanner)
    if not pf:
        return None, None
    return pf, entity.read_pos(scanner, pf + 8)


def _wing_slot(scanner):
    """背包裡天使之翼（回程道具）的格號（物品自記 +0x25）；沒有回 None。"""
    h = bag.head(scanner)
    if not h:
        return None
    got = inventory.find_by_type(scanner, h[0], recall.RECALL_ITEM)
    return got[0] if got else None


def _wait_map_change(scanner, from_map: int, timeout: float):
    """等地圖變成別的（回城）。回新地圖 id；逾時回 None。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = scene.current_id(scanner)
        if cur is not None and cur != from_map:
            return cur
        time.sleep(0.3)
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


def _nearest_reachable(reach, target, max_r: int = 50):
    """`reach`（從玩家可達的格子集合）裡離 `target` 最近的那一格；`reach` 空才回 None。

    為什麼要它：**錨點那格常常不可走**——
      ① NPC 站櫃檯後（銀行實測真座標 (326,140) 不可達）；
      ② .MPC 表座標不準又落在不可達處（永夜城銀行 (129,168) 不可達）。
    直接走錨點 navigate 算不出路、角色不動。改走「離錨點最近的可走格」（走近後 NPC 串流
    進來再精修，剩下交 0x54A520 隔櫃檯互動）。先一圈圈往外找（近的話很快），找不到再全掃保底。
    """
    tx, ty = target
    if (tx, ty) in reach:
        return (tx, ty)
    for r in range(1, max_r + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:      # 只掃這一圈的外框
                    continue
                c = (tx + dx, ty + dy)
                if c in reach:
                    return c
    # 錨點離所有可走格都 > max_r（罕見）→ 全掃找最近的，保證有解（reach 非空）
    best, bd = None, 1 << 30
    for (x, y) in reach:
        d = abs(x - tx) + abs(y - ty)
        if d < bd:
            bd, best = d, (x, y)
    return best


def _walk_to_npc(mover, scanner, npc_id: int, fallback, timeout: float) -> bool:
    """走到某個 NPC 旁。到了/夠近回 True。

    ★★★ 目標＝**離 NPC 真實座標最近的「可走」格**（NPC 本身常在櫃檯後不可走）。
      NPC 看得到（已串流進來）就用它的真實座標算；看不到才退回 `fallback`（.MPC 表座標，
      只為把人帶進串流範圍）。
    ⚠ 為什麼要這樣：實測**和風林銀行** .MPC 表座標 (322,148) 離真實 NPC (326,140) 差 12 格
      （表座標不準），而真實 NPC 那格**不可走**——直接走任一個都不對。改成「走到 NPC 旁
      最近的可走格」才穩（買/修也共用這支，本來就更可靠）。每 ~2 秒重規劃一次。
    """
    g, _ = terrain.load(scanner)              # 地形圖載一次（同一張圖不變）
    nav = None
    target = None
    best = 999
    t0 = last_plan = 0.0
    t0 = time.time()
    while time.time() - t0 < timeout:
        pf, here = _player_tile(scanner)
        if pf is None or here is None:
            time.sleep(0.3)
            continue
        now = time.time()
        if target is None or now - last_plan > 2.0:      # 每 ~2s 重規劃目標
            last_plan = now
            # 錨點：NPC 串流進來就用它的真座標（準），否則用 .MPC 表座標把人帶近。
            # 兩者那格都常不可走 → 一律走「離錨點最近的可走格」。
            anchor = _npc_tile(scanner, npc_id) or tuple(fallback)
            newt = None
            if g is not None:
                reach = g.reachable(round(here[0]), round(here[1]))
                newt = _nearest_reachable(reach, anchor) if reach else None
            if newt is None:
                newt = tuple(fallback)                    # 地形讀不到才硬走表座標
            if newt != target:
                target = newt
                best = 999
                nav = navigate.Navigator()
                nav.reset((float(target[0]), float(target[1])))
        d = abs(round(here[0]) - target[0]) + abs(round(here[1]) - target[1])
        best = min(best, d)
        if d <= ARRIVE_TILES:
            return True
        nav.step(scanner, mover, pf + 8, float(target[0]), float(target[1]))
        if nav.stuck:
            nav = navigate.Navigator()
            nav.reset((float(target[0]), float(target[1])))
            time.sleep(0.3)
        time.sleep(0.2)
    return best <= NEAR_ENOUGH


def run_full_supply(mover, scanner, say=None) -> tuple[bool, str]:
    """完整補給一趟。say(訊息) 可選，用來即時回報進度。

    記錄地圖與座標 → 天使之翼回城 → 查表 →（有要存的才去）銀行存 → 修裝全修 →
    照清單買 → 趴趴GO 跳回原練功點。順序＝**銀行 → 修裝 → 買**（使用者要求）。
    回 (整趟有沒有成功, 說明)。
    """
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"

    # 1. 記錄練功點（地圖 + 座標，回程要跳回這裡）
    start_map = scene.current_id(scanner)
    if start_map is None:
        return False, "讀不到目前地圖"
    _, start_pos = _player_tile(scanner)
    back = jumpmap.nearest(start_map,
                           start_pos[0] if start_pos else None,
                           start_pos[1] if start_pos else None)
    note(f"記錄練功點：{scene.scene_name(start_map)}"
         + (f"（回程點：{back.name}）" if back else "（⚠ 沒有回程傳送點）"))

    # 2. 天使之翼回城
    slot = _wing_slot(scanner)
    if slot is None:
        return False, f"背包沒有{itemname.label(recall.RECALL_ITEM)}（回程道具）"
    if not recall.use_item(mover, slot):
        return False, "回程道具送不出去"
    note("用天使之翼回城中…")
    home = _wait_map_change(scanner, start_map, WING_WAIT)
    if home is None:
        return False, "回城後地圖沒變（回程可能失敗）"
    note(f"回到 {scene.scene_name(home)}")
    time.sleep(1.0)                       # 落地穩定一下

    # ── 回程收尾（★★ 不准射後不理）────────────────────────────
    # 「送出去≠到得了」是這個專案抓過的真根因（memory jump-back-channel-fix，
    # 0c56fb4 修的就是這型）。呼叫端一拿到結果就會重開採集／掛機，人還在城裡
    # 就重開＝把採集中心寫成「城的地圖＋練功區座標」，精靈直接迷路。
    # 所以送了要**等落地、驗地圖、沒到就重送**，確定回到練功圖才把結果交回去。
    # ⚠ 這裡是背景執行緒，重送設有限次（不回報比重試更糟）；還是沒到就大聲說，
    #   呼叫端看得到「人不在採集圖」自己接手。
    def _jump_back() -> tuple[bool, str]:
        if back is None:
            return False, "⚠ 沒有回練功點的傳送點，留在城裡"
        for _ in range(JUMP_TRIES):
            jok, _jm = jumpmap.teleport(mover, scanner, back.jump_id)
            note(f"跳回 {scene.scene_name(start_map)}…")
            landed = _wait_map_change(scanner, home, JUMP_WAIT) if jok else None
            if landed is not None and scene.same_map(landed, start_map):
                return True, "已回到練功點"
            if not jok:
                time.sleep(1.0)          # 送不出去（槽忙）→ 喘口氣再重送
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
    # ⚠ 各步驟**不在這裡先用地形圖 _walk_to_npc**（它在 NPC 櫃檯區會誤判「走不到」）——
    #   run_bank/run_repair/run_buy 內的 _engage_npc→_walk_close 會用**遊戲尋路**貼到 NPC 旁，
    #   遠距離則由 _engage 自己在 NPC 沒串流時退回地形圖靠近。
    bank_npc = entry.get("bank")
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

    # 5. 修裝：有維修商就走去全修（★無腦修，不看裝備——使用者指定）
    # ★ 修裝的成敗要進整趟的結果：壞裝觸發的那趟若修裝失敗，回去馬上又會
    #   觸發下一趟（又燒一張翼）——呼叫端要看得到 False 才能踩煞車。
    rep = entry.get("repair")
    repair_ok = True
    if rep:
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
        note(f"走去補給商 ({bx},{by}) 開店購買…")
        bought_ok, bmsg = run_buy(mover, scanner, bid, (bx, by))
        note(bmsg)
        results.append("購買:" + bmsg)
    else:
        results.append("這城沒補給商（不賣天使之翼）")

    # 7. 趴趴GO 跳回練功點（等落地＋重送，見 _jump_back）
    summary = "；".join(results)
    jok, jmsg = _jump_back()
    return (bought_ok and repair_ok and jok), f"{summary}；{jmsg}"
