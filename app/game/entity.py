"""附近實體（怪物 / NPC / 其他玩家）與「目前選定的目標」。

這是自動掛機的基礎：列出附近有什麼可以打，以及指定要打誰。

怎麼定位（全部認 vtable，angel.dat 無 ASLR，重開遊戲照樣有效）
--------------------------------------------------------------
    實體物件 = 開頭是 VT_ENTITY 的物件
    狀態物件 = 開頭是 VT_STATE 的物件（實測每個分身剛好 1 個）
    玩家物件 = 開頭是 VT_PLAYER 的物件（實測每個分身剛好 1 個）

⚠ 玩家自己**不在**實體串列上（它是另一個類別），但版面跟怪一模一樣
  —— 名字同樣在 +0x1D4、位置同樣在 +0xBC/+0xC0，所以雙方座標可以直接相減。

⚠ 實體物件有**兩個 vtable，相差 8 bytes**：真正的物件起點是 VT_ENTITY2，
  +8 才是 VT_ENTITY。本檔所有偏移都以「掃 VT_ENTITY 得到的位址」為基準。
  反組譯遊戲程式碼時看到的 `edi+0x1D0`，就是這裡的 OFF_ID(+0x1C8) —— 差的正是這 8 bytes。
  曾經因為沒注意到，誤以為遊戲拿「種類 ID」當攻擊目標，卡了很久。

怎麼攻擊指定的怪
----------------
遊戲攻擊時的組語是 `push [狀態物件+0x2D8] ; push 0xc ; call 送出函式`
—— **它自己會重讀那個欄位**。所以只要把目標的實體 ID 寫進去，
再送一個攻擊按鍵（例如 F2）給視窗，角色就會打那隻。

不需要注入任何會執行的程式碼、不需要自己組封包、不需要搶視窗焦點。
（曾經走過「注入 stub 讓遊戲主執行緒呼叫送出函式」那條路，能跑但畫面沒反應
——因為血條是客戶端自己畫的，它看的就是 +0x2D8 這個欄位。）

要打哪一隻 —— 用距離挑最近的
----------------------------
實體串列本身沒有順序可言，直接拿第一隻會挑到天邊那隻。實測 +0xBC/+0xC0
是位置（見下方常數），按「離玩家的距離」排序取最近的就穩了。

而且實測**遊戲的攻擊指令內建自動接近**：鎖定 22 格外的怪並送 F2 後，
玩家座標會自己一路走過去，8 秒後把牠打死。所以距離只影響效率，不影響可行性
——先前「有時打得到有時打不到」純粹是挑到了很遠的那隻。

驗證狀況（2026-08-02）：五個分身、五張不同地圖全部通過；其中一台當下有目標，
且該值確實等於清單裡某隻的實體 ID（兩個獨立發現的交叉驗證）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from app.core.memory import VALUE_TYPES

# --- vtable（angel.dat 內的絕對位址，無 ASLR）---
VT_ENTITY = 0x007D2A24    # 實體物件（掃這個；注意它在物件起點 +8）
VT_ENTITY2 = 0x007D29D8   # 同一物件的第二個 vtable，位於物件起點
# ⚠⚠ **遊戲改版會讓 vtable 位移** —— 2026-08-04 12:22 那次改版：
#       VT_STATE   0x7E3E40 → 0x7E3E50（+0x10）
#       VT_PLAYER  0x7D8BE0 → 0x7D8BF8（+0x18）
#       VT_ENTITY  沒有變
#   症狀是「突然掃不到怪物」（其實是掃不到玩家/狀態物件，整個掃描判定失敗）。
#   ⚠ 啟動器的 patch.log 會寫 "no update needed"，**不能當作沒改版** ——
#     那是版本號檢查，angel.dat 本身還是被換掉了（看檔案時間）。
#   重新定位的方法（scratchpad 有腳本）：
#     · 玩家物件 —— move.pathfinder_this() 不靠 vtable，算出來 +8 讀 [+0] 就是新值
#     · 狀態物件 —— 掃舊值附近的候選，取「+0x2DC 是 0~100 的百分比、
#                   而且 +0x2D8 有真實目標 ID」的那個
VT_STATE = 0x007E3E50     # 玩家狀態物件（每個分身剛好 1 個）
VT_PLAYER = 0x007D8BF8    # 玩家自己的物件（每個分身剛好 1 個）

# --- 實體物件的欄位（基準 = 掃 VT_ENTITY 得到的位址）---
OFF_PREV = 0x08           # 雙向串列
OFF_NEXT = 0x0C
OFF_POS_X = 0xBC          # 位置 X（見 TILE_UNITS）
OFF_POS_Y = 0xC0          # 位置 Y
OFF_ID = 0x1C8            # 實體 ID，每隻唯一 —— 攻擊目標就是傳這個
OFF_TYPE = 0x1D0          # 種類 ID；0 = 其他玩家
OFF_NAME = 0x1D4          # 名字，內嵌 UTF-8
# ★ 實體種類。實測（同一畫面同時有三種）：
#       7 = 怪物（沼澤青鱗戰士 0xE12、猩大嬸 0xE11）
#       2 = 其他玩家（超級菲比，種類 ID 是 0）
#       0 = NPC（隱士查洛 0x995）
#   比「種類 ID != 0」準得多 —— 那個只排得掉玩家，NPC、寵物、召喚物都排不掉。
OFF_KIND = 0x2E4
KIND_MONSTER = 7
# ★ 「這隻怪跟誰交戰過」—— 存的是**指向對方角色物件的指標**（不是實體 ID），
#   出現在 +0x4D8 / +0x4E0 / +0x4E8 三個槽。
# ⚠⚠ **這是弱訊號，不是即時的「正在打誰」**（2026-08-07 黑狐 423 秒唯讀跟拍）：
#   · 怪出手的當下（動畫 'Att'、我方同拍掉血）三個槽**全部是空的**（17/17）
#   · 玩家指標反而殘留在發呆中、甚至**屍體**身上（Dead＋三槽全滿 47 筆）
#   · 只看 +0x4D8 一個槽，在有標記的觀測裡還會漏 25%（僅 +0x4E0 有 65 筆）
#   所以「正在打我」一律要用聯集判定：attacking()（三槽任一）**或**
#   「動畫是 ATT_STATES 且離我很近」——見 farm_tab 的 _fighting_me()。
OFF_FOE = 0x4D8
FOE_SPAN = 0x4E8 + 4 - OFF_FOE      # 三個槽一次讀回來（+0x4D8 ~ +0x4E8）
# 攻擊型動畫狀態（出手的那幾拍）。搭配距離就是「正在打我」的另一半訊號。
ATT_STATES = ("Att", "Att2", "Cast")
# ★★ 動畫狀態的 ASCII（不是指標，字串直接內嵌在物件裡）。
#   實測 90 秒、98 隻怪、7,626 次觀測只出現五種值
#   （'Wait' 5579、'Dead' 1483、'Run' 317、'Att' 129、'Att2' 118），
#   另外在別的分身看到 'Cast'。**除了 'Dead' 以外一律當活的**，
#   所以之後多出新狀態也不會誤殺（五台實測沒有一次讀不到）。
#
# ★ **玩家自己的物件用同一個偏移**（版面跟怪一樣，見 VT_PLAYER 那段），
#   而且多一個怪不會有的狀態：**'Sit'（坐下）**。
#   實測（黑狐，送 Insert）：'Wait' → 'Sit' 91ms、'Sit' → 'Wait' 92ms。
#   → 見 is_sitting()。
#   **`'Dead'` 就是屍體**，而且是唯一可靠又即時的死活訊號：
#     · 64 隻死掉的怪裡只有 1 隻後來又變回 'Wait'（該格被回收再用）
#     · 屍體會一直留在實體清單裡：中位 5.0 秒、最久 79.8 秒，
#       64 隻裡有 50 隻超過 5 秒
#     · 任何一個瞬間，清單裡有 **中位 20%、最高 50%** 是屍體
#   → 不篩掉的話「挑最近的一隻」很常挑到別人剛殺掉的，這就是搶怪區
#     「卡卡的、一直對著空氣」的來源。見 Entity.dead。
OFF_STATE = 0x12C
STATE_MAX = 8             # 讀幾 bytes；最長的是 'Att2'


def attacking(scanner, ent, player_obj: int) -> bool:
    """交戰槽（三個都看）裡有沒有我。

    ⚠ 這只是「跟我交戰過」的**弱訊號**：怪出手的當下三個槽反而是空的
      （見 OFF_FOE 的實測）。要判「正在打我」請再聯集動畫狀態＋距離
      （farm_tab._fighting_me），單獨用這個一定漏。
    """
    if not player_obj:
        return False
    raw = scanner._read_bytes(ent.addr + OFF_FOE, FOE_SPAN)
    if not raw:
        return False
    vals = struct.unpack("<5I", bytes(raw))     # +0x4D8/+0x4DC/…/+0x4E8
    return player_obj in (vals[0], vals[2], vals[4])

# 位置是 16.16 定點數：高 16 位是世界單位，一格 = 32 個世界單位。
# （怪站定時值恆為 tile*32+16，也就是格子中心；移動中才會出現小數。）
# 玩家物件用的是同一組偏移、同一種編碼 —— 所以能直接相減算距離。
TILE_UNITS = 32.0

# --- 狀態物件的欄位 ---
OFF_TARGET = 0x2D8        # 目前選定的目標（實體 ID）
OFF_TARGET_HP = 0x2DC     # 目標的血量百分比；**攻擊前會檢查它 > 0**
TARGET_HP_FULL = 100      # 實測選中滿血目標時這裡是 0x64 = 100

NAME_MAX = 40             # 名字最多讀幾 bytes


@dataclass(frozen=True)
class Entity:
    """一個附近的實體。type_id 為 0 代表是其他玩家，不是怪。

    x / y 是掃描當下的格子座標（怪會走動，要最新的請用 read_pos）。
    """

    addr: int
    eid: int
    type_id: int
    name: str
    x: float = 0.0
    y: float = 0.0
    kind: int = -1            # 見 OFF_KIND；-1 = 沒讀到
    state: str = ""           # 動畫狀態，見 OFF_STATE

    @property
    def dead(self) -> bool:
        """牠是不是屍體 —— **挑目標之前一定要問這個**。

        遊戲把動畫狀態的 ASCII 放在 OFF_STATE，死掉就是 `'Dead'`。
        屍體不會馬上從實體清單消失（實測中位賴 5 秒、最久 79.8 秒），
        `is_alive()`（只比對 vtable + 實體 ID）**分不出死活**，所以搶怪的
        地方會一直挑到別人剛殺掉的那具。
        ⚠ 讀不到狀態（空字串）時一律當成活的 —— 寧可多打一隻，
          也不要因為讀取失敗把整批怪都跳過。
        """
        return self.state == "Dead"

    @property
    def is_monster(self) -> bool:
        """是不是**可以打的怪**（不是玩家、不是 NPC、不是寵物／召喚物）。

        ★ 用 OFF_KIND 判斷。以前用 `type_id != 0`，那只排得掉其他玩家 ——
          NPC、別人的寵物、戰鬥化身的種類 ID 也不是 0，會一起跑進清單裡
          （使用者回報「還是有 NPC 以及別人寵物」）。
        ⚠ 讀不到 kind（-1）時退回舊判斷，寧可多列也不要整個列不出來。
        """
        if self.kind < 0:
            return self.type_id != 0
        return self.kind == KIND_MONSTER


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", raw)[0] if raw else 0


def _scan_vtables(scanner, vts, should_stop=None,
                  regions=None) -> tuple[dict[int, list[int]], list]:
    """一次掃完，同時找出好幾種 vtable 的物件。

    回傳 (命中位址, 有命中的區塊清單)。後者拿去當下次的 regions 就是「熱區掃描」。

    ★ 全記憶體掃描的成本幾乎全在「把 700MB 讀出來」，比對本身很便宜。
      所以要三種物件時，讀一遍比對三次，比掃三遍快將近三倍。

    ★ regions 不是 None 時**只掃指定的區塊**。實測這些物件全部集中在
      單一一塊記憶體（佔全部的 0.1%～1.9%），所以拿上次的命中區塊再掃一次，
      成本只有全掃的百分之幾 —— 這是「刷新怪物清單」能做到即時的關鍵。
      ⚠ 但堆積是會變的，熱區掃描必須搭配定期的全掃當保險（見 snapshot）。

    should_stop: 可選 callable，每個區塊前呼叫；回傳 True 就中止。
    """
    vts = list(vts)
    out: dict[int, list[int]] = {v: [] for v in vts}
    targets = [(np.uint32(v), out[v]) for v in vts]
    hot: list = []
    if not targets:
        return out, hot            # 沒有目標就不必把記憶體讀出來
    # 多目標時的粗篩範圍（見下面）。⚠ 一定要每次算 —— vtable 會被
    # locate.warm() 依 AOB 改寫，snapshot 也會多帶一個角色屬性的 vtable。
    # ⚠ 純量一定要是 np.uint32：傳 Python int 給 uint32 陣列比較會整個重轉型。
    lo, hi = np.uint32(min(vts)), np.uint32(max(vts))
    for base, size in (regions if regions is not None
                       else scanner._iter_regions(writable_only=True)):
        if should_stop is not None and should_stop():
            return out, hot
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        found = False
        if len(targets) == 1:
            target, bucket = targets[0]
            idx = np.flatnonzero(arr == target)
            if idx.size:
                bucket.extend((base + idx.astype(np.int64) * 4).tolist())
                found = True
        else:
            # ★ 多個目標時**先用範圍刷一次**再分類：以前是每個 vtable 各掃一遍
            #   整個陣列（四個目標 = 四趟 700MB 的比對，外加四個等長的暫時陣列）。
            #   所有 vtable 都擠在 angel.dat 的一小段位址裡，所以一次
            #   `lo <= x <= hi` 就把 99.99% 刷掉，剩下的才逐個比對。
            #   命中集合與順序都跟以前一模一樣（flatnonzero 本來就是遞增）。
            idx = np.flatnonzero((arr >= lo) & (arr <= hi))
            if idx.size:
                vals = arr[idx]
                for target, bucket in targets:
                    sel = idx[vals == target]
                    if sel.size:
                        bucket.extend((base + sel.astype(np.int64) * 4).tolist())
                        found = True
        if found:
            hot.append((base, size))
    return out, hot


def _scan_vtable(scanner, vt: int, should_stop=None) -> list[int]:
    """找出所有「開頭是這個 vtable」的物件。要掃全記憶體，約 0.4 秒。"""
    return _scan_vtables(scanner, (vt,), should_stop)[0][vt]


def read_pos(scanner, addr: int) -> tuple[float, float] | None:
    """讀出格子座標；讀不到回傳 None。實體與玩家物件通用。"""
    raw = scanner._read_bytes(addr + OFF_POS_X, 8)
    if not raw:
        return None
    vx, vy = struct.unpack("<II", raw)
    return (vx >> 16) / TILE_UNITS, (vy >> 16) / TILE_UNITS


def locate_state(scanner, should_stop=None) -> int | None:
    """定位玩家狀態物件；找不到回傳 None。

    實測每個分身剛好 1 個。若掃到多個，取第一個並不可靠，所以這裡回傳 None
    ——寧可讓上層知道情況不對，也不要拿錯的物件去寫入。
    """
    hits = _scan_vtable(scanner, VT_STATE, should_stop)
    return hits[0] if len(hits) == 1 else None


# ⛔ locate_player() 移除了：玩家物件一律跟其他實體**同一遍掃出來**
#   （`snapshot()` 一次比對三種 vtable），單獨再掃一遍等於多花一次全記憶體
#   讀取。preload.py 的註解講的就是這件事。


# 一個實體要讀到的最後一個欄位是 OFF_KIND(0x2E4)+4 —— 一次讀這麼多就全有了。
ENT_SPAN = OFF_KIND + 4


def _entity_from_blob(addr: int, blob: bytes) -> Entity | None:
    """從「一次讀回來的整塊物件」切出各欄位。判定規則跟逐格讀完全相同。"""
    try:
        name = blob[OFF_NAME:OFF_NAME + NAME_MAX].split(b"\x00")[0] \
            .decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not name:
        return None
    vx, vy = struct.unpack_from("<II", blob, OFF_POS_X)
    try:
        state = blob[OFF_STATE:OFF_STATE + STATE_MAX].split(b"\x00")[0] \
            .decode("ascii")
    except UnicodeDecodeError:
        state = ""
    return Entity(addr,
                  struct.unpack_from("<I", blob, OFF_ID)[0],
                  struct.unpack_from("<I", blob, OFF_TYPE)[0],
                  name,
                  (vx >> 16) / TILE_UNITS, (vy >> 16) / TILE_UNITS,
                  kind=struct.unpack_from("<I", blob, OFF_KIND)[0],
                  state=state)


def _build_slow(scanner, addr: int) -> Entity | None:
    """逐欄位讀（舊做法）。只在整塊讀不到時走這裡 —— 物件尾巴壓在未映射頁時，
    名字那幾個欄位其實還讀得到，不能因此把整隻怪丟掉。"""
    raw = scanner._read_bytes(addr + OFF_NAME, NAME_MAX)
    if not raw:
        return None
    try:
        name = raw.split(b"\x00")[0].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not name:
        return None
    pos = read_pos(scanner, addr) or (0.0, 0.0)
    return Entity(addr, _u32(scanner, addr + OFF_ID),
                  _u32(scanner, addr + OFF_TYPE), name, *pos,
                  kind=_u32(scanner, addr + OFF_KIND),
                  state=read_state(scanner, addr))


def _build(scanner, addrs: list[int]) -> list[Entity]:
    """把掃到的位址變成 Entity 清單。

    ★ 每隻怪**只發一次 ReadProcessMemory**（整塊 0x2E8 bytes 一起讀，再自己切）。
      以前是名字、座標、ID、種類、kind、狀態各發一次 —— 六倍的系統呼叫。
      掃描執行緒每秒要處理幾十到上百隻，這是它最大的一筆開銷。
    ★ 附帶好處：六個欄位變成**同一瞬間**的快照，不會出現「座標是新的、
      狀態是舊的」這種跨時間點的組合。
    """
    out: list[Entity] = []
    for addr in addrs:
        blob = scanner._read_bytes(addr, ENT_SPAN)
        ent = (_entity_from_blob(addr, blob) if blob
               else _build_slow(scanner, addr))
        if ent is not None:
            out.append(ent)
    return out


STATE_SIT = "Sit"     # 只有玩家會有；遊戲裡按 Insert 切換


def read_state(scanner, addr: int) -> str:
    """動畫狀態。

    怪：`'Wait'` / `'Run'` / `'Att'` / `'Att2'` / `'Cast'` / `'Dead'`
    玩家：同上，另外有 `'Sit'`（坐下）

    讀不到就回空字串 —— 呼叫端一律把空字串當「活的」（見 Entity.dead）。
    """
    raw = scanner._read_bytes(addr + OFF_STATE, STATE_MAX)
    if not raw:
        return ""
    try:
        return bytes(raw).split(b"\x00")[0].decode("ascii")
    except UnicodeDecodeError:
        return ""


STATE_RUN = "Run"


def is_walking(scanner, player_obj: int) -> bool:
    """角色現在是不是正在走路。

    ★★ 這是「這一段走完了沒」的**即時**訊號 —— 比隔 0.3 秒比一次位置準太多。
      實測（黑狐）下了移動指令之後：
          +0.46 秒 狀態變 'Run'（開始走）
          +2.45 秒 狀態變 'Wait'（走完，21 格）
      靠比位置的話，走完之後最久要 0.3 秒才發現，加上指令冷卻 0.4 秒
      → 每個路段中間空等約 0.5 秒。長距離要走好幾段，看起來就是**一頓一頓的**。
      實測同一條來回路線：比位置 28% 的時間站著不動（卡頓 11 次），
      改讀這個欄位之後剩 10%（卡頓 4 次）。
    ⚠ 讀不到（空字串）回 False —— 不知道就當停著，寧可多下一次指令。
    """
    return bool(player_obj) and read_state(scanner, player_obj) == STATE_RUN


def is_sitting(scanner, player_obj: int) -> bool:
    """角色現在是不是坐著（遊戲裡按 Insert 切換）。

    ★ 讀的就是跟怪共用的那個動畫狀態欄位（OFF_STATE），所以**零成本**、
      也已經在 AOB 自動定位的保護範圍內。

    實測（黑狐，用 win.send_key 送 Insert）：
        'Wait' → 'Sit'  91 ms
        'Sit'  → 'Wait' 92 ms
    另外五台同時觀察時，剛好坐著的那一台直接讀到 'Sit'，其餘為 'Wait'。

    ⚠ 讀不到（空字串）時回 False —— 不知道就當沒坐著，不要據此做動作。
    """
    return bool(player_obj) and read_state(scanner, player_obj) == STATE_SIT


def snapshot(scanner, should_stop=None, regions=None,
             extra_vts=()) -> tuple[int | None, int | None,
                                    list[Entity], list, dict]:
    """掛機要的東西一次拿齊：(狀態物件, 玩家物件, 附近實體, 命中的區塊, 額外命中)。

    extra_vts: 呼叫端想順便掃的其他 vtable（例如角色屬性物件，用來讀 HP）。
    多比對一種幾乎不花錢 —— 成本全在把記憶體讀出來 —— 所以要什麼一次掃完。
    回傳的「額外命中」是 {vtable: [位址,...]}，怎麼認由呼叫端自己決定。

    三種都要掃，合成一遍讀取 —— 這是掃描能不能夠快的第一個關鍵。
    狀態／玩家物件掃到的數量不是剛好 1 個時回傳 None（寧可讓上層知道情況不對，
    也不要拿錯的物件去寫入）。

    ★ 第二個關鍵：把回傳的「命中的區塊」記下來，下次當 regions 傳回來，
      就只掃那幾塊（實測快兩位數倍）。⚠ **必須定期做一次全掃當保險** ——
      堆積會變，換地圖或重連之後物件可能搬到別的區塊；只要熱區掃描的結果
      看起來不對（狀態或玩家物件不見了），呼叫端就該退回全掃。
    """
    extra = [v for v in extra_vts if v]
    hits, hot = _scan_vtables(scanner,
                              [VT_STATE, VT_PLAYER, VT_ENTITY] + extra,
                              should_stop, regions)
    state = hits[VT_STATE][0] if len(hits[VT_STATE]) == 1 else None
    player = hits[VT_PLAYER][0] if len(hits[VT_PLAYER]) == 1 else None
    return (state, player, _build(scanner, hits[VT_ENTITY]), hot,
            {v: hits[v] for v in extra})


# --- 逆向筆記：兩條走過但**沒有採用**的路 -------------------------------
# 程式碼已經移除（沒有任何呼叫者），保留結論免得下次又走一遍。
#
# ① 已載入的怪物類型表　vtable 0x007D8E88（2026-08-04 當時的值）
#      +0x00 vtable  +0x04 容量(0x3FF/0x400)  +0x0C 種類數
#      +0x10 起 指向名稱字串的指標陣列
#    本來想拿它當「這是不是真正的怪」的白名單。
#    ⚠ 它**不是「這張地圖的怪」**：實測同時存在好幾個這種物件，內容跨地圖
#      累積（在烏托邦讀到 7 種，混著前一張圖的史萊姆／水元素／天國百合）。
#    ⛔ 已被 OFF_KIND（+0x2E4，7 = 怪物）取代 —— 那個直接、即時、不必掃描。
#      farm_tab 那邊也標了「別再試」。
#
# ② 客戶端算出來的落腳點　玩家物件 +0x144（16.16 定點，同座標編碼）
#    按技能鍵時客戶端會依**該招射程**算出落腳點寫在那裡，
#    所以 距離(落腳點, 怪) 就是官方射程（實測雪狐近戰 1.5 格）。
#    ⛔ 使用者已否決「問客戶端射程」這條路（見 [[skill-range]]）。


def read_target(scanner, state: int) -> int:
    """目前選定的目標實體 ID；沒有選定時是 0。"""
    return _u32(scanner, state + OFF_TARGET)


def read_target_hp(scanner, state: int) -> int:
    """目標的血量百分比（0~100）。目標死亡或沒有目標時是 0。"""
    return _u32(scanner, state + OFF_TARGET_HP)


# 從狀態物件開頭一路讀到目標血量 —— 涵蓋 vtable(+0) 與 +0x2D8/+0x2DC
# （一次整塊讀，兩個值才是同一瞬間的：分開讀會拿到「舊 ID 配新血量」的組合）
STATE_SPAN = OFF_TARGET_HP + 4


def read_target_checked(scanner, state: int) -> tuple[bool, int, int]:
    """回傳 (這個位址還是狀態物件嗎, 目標實體 ID, 血量)。

    ⚠⚠⚠ **寫目標之前一定要先問這個。**

    狀態物件會搬家（換地圖、死亡重生、斷線重連、傳送），而寫入執行緒每 20ms
    就往 `state + 0x2D8` 寫 4 bytes。位址一旦過期，那塊記憶體早就被遊戲拿去
    放別的東西了 —— 我們就是在**每秒 50 次亂改遊戲的堆積**。
    症狀正是「掛一段時間之後遊戲自己跳錯誤視窗掛掉」，而且離真正的元凶
    （某一次換地圖）已經很遠，看起來完全沒關聯。

    ★ 檢查方式是比對物件開頭的 vtable，**跟讀目標同一次系統呼叫**
      （0 到 0x2DC 一起讀回來），所以這道保險是免費的。
    ★ vtable 值本身會被 `locate.warm()` 依 AOB 重新定位，改版也跟得上。
    """
    if not state:
        return False, 0, 0
    raw = scanner._read_bytes(state, STATE_SPAN)
    if not raw:
        # 讀不到 ≠ 位址錯了（可能只是這一瞬間讀失敗）。回報「還沒失效」但
        # 沒有數值，讓呼叫端照原本的邏輯處理讀不到的情形。
        return True, 0, 0
    if struct.unpack_from("<I", raw, 0)[0] != VT_STATE:
        return False, 0, 0
    return (True,
            struct.unpack_from("<I", raw, OFF_TARGET)[0],
            struct.unpack_from("<I", raw, OFF_TARGET_HP)[0])


def _write_u32(scanner, addr: int, value: int) -> None:
    """寫入一個無號 32 位元值。

    ⚠ 實體 ID 是**無號** 32 位元（實測有 0x8E8D04DA = 23 億這種值），
    但 MemoryScanner 只提供有號的 int32，直接傳會炸：
        'i' format requires -2147483648 <= number <= 2147483647
    先轉成等價的有號值 —— 寫進記憶體的 4 個位元組完全相同。
    """
    signed = struct.unpack("<i", struct.pack("<I", value & 0xFFFFFFFF))[0]
    scanner.write_value(addr, VALUE_TYPES["int32"], signed)


def set_target_id(scanner, state: int, eid: int) -> None:
    """**只寫目標 ID，不碰血量欄位。**

    ★ 用封包攻擊時要用這個，不要用 set_target()。
      血量欄位（+0x2DC）是遊戲用來告訴我們「這隻剩多少血、死了沒」的，
      set_target() 會把它寫成 100 —— 那是為了餵飽**按鍵**攻擊的前置檢查
      （`cmp [esi+0x2dc],0 / jle 跳過`）。封包攻擊是直接呼叫施放函式，
      不經過那個檢查，所以沒必要寫；一寫就把死亡訊號蓋掉了，
      結果是打死了還一直打屍體（使用者回報的「鎖定一隻怪發呆」）。
    """
    _write_u32(scanner, state + OFF_TARGET, eid)


def set_target(scanner, state: int, eid: int,
               hp_pct: int = TARGET_HP_FULL) -> None:
    """把目標寫進客戶端狀態。之後送出攻擊按鍵，角色就會打這隻。

    ★ 必須**同時寫兩個欄位**，只寫 ID 是不夠的：

        +0x2D8  目標實體 ID
        +0x2DC  目標血量百分比

    因為攻擊的程式碼長這樣（反組譯 0x60266C）：

        cmp  dword ptr [esi+0x2dc], 0
        jle  跳過攻擊                    ← 血量 ≤ 0 就當成目標已死，不出手
        push [esi+0x2d8] ; push 0xc ; call 0x5d3eb5

    只寫 +0x2D8 的話，+0x2DC 停在 0，遊戲會認為那隻已經死了而直接跳過攻擊
    —— 症狀是「血條出現了（那只看 +0x2D8），但按 F2 完全沒反應」。

    遊戲自己選目標時也是兩個一起設（0x5FA550 寫 +0x2DC、0x5FA5F0 寫 +0x2D8）。
    """
    _write_u32(scanner, state + OFF_TARGET, eid)
    _write_u32(scanner, state + OFF_TARGET_HP, hp_pct)


def is_alive(scanner, ent: Entity) -> bool:
    """這個實體物件還在嗎？（怪死掉 / 離開視野後物件會被回收再利用）

    用「vtable 還在、而且實體 ID 沒變」判斷 —— 只看 vtable 不夠，
    因為同一塊記憶體很快就會被下一隻怪拿去用。
    """
    return (_u32(scanner, ent.addr) == VT_ENTITY
            and _u32(scanner, ent.addr + OFF_ID) == ent.eid)


# 一次讀到 OFF_ID 就涵蓋 vtable(+0)、座標(+0xBC)、狀態(+0x12C)、實體 ID(+0x1C8)
LIVE_SPAN = OFF_ID + 4


def read_live(scanner, ent: Entity) -> tuple[bool, str, tuple[float, float] | None]:
    """一次讀回 (物件還在嗎, 動畫狀態, 座標)。

    ★ 挑目標時這三件事本來各發一次系統呼叫（`is_alive` 自己就兩次），
      一隻怪四次、場上幾十隻，而且每殺一隻就重挑一次。四次併成一次。
    ★ 三個值變成**同一瞬間**的快照，不會出現「還活著但座標是死前的」。
    讀不到就退回逐欄位讀，結果跟以前完全一樣。
    """
    blob = scanner._read_bytes(ent.addr, LIVE_SPAN)
    if not blob:
        return (is_alive(scanner, ent), read_state(scanner, ent.addr),
                read_pos(scanner, ent.addr))
    alive = (struct.unpack_from("<I", blob, 0)[0] == VT_ENTITY
             and struct.unpack_from("<I", blob, OFF_ID)[0] == ent.eid)
    try:
        state = blob[OFF_STATE:OFF_STATE + STATE_MAX].split(b"\x00")[0] \
            .decode("ascii")
    except UnicodeDecodeError:
        state = ""
    vx, vy = struct.unpack_from("<II", blob, OFF_POS_X)
    return alive, state, ((vx >> 16) / TILE_UNITS, (vy >> 16) / TILE_UNITS)
