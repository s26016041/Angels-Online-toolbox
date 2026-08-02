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

import math
import struct
from dataclasses import dataclass

import numpy as np

from app.core.memory import VALUE_TYPES

# --- vtable（angel.dat 內的絕對位址，無 ASLR）---
VT_ENTITY = 0x007D2A24    # 實體物件（掃這個；注意它在物件起點 +8）
VT_ENTITY2 = 0x007D29D8   # 同一物件的第二個 vtable，位於物件起點
VT_STATE = 0x007E3E40     # 玩家狀態物件（每個分身剛好 1 個）
VT_PLAYER = 0x007D8BE0    # 玩家自己的物件（每個分身剛好 1 個）

# --- 實體物件的欄位（基準 = 掃 VT_ENTITY 得到的位址）---
OFF_PREV = 0x08           # 雙向串列
OFF_NEXT = 0x0C
OFF_POS_X = 0xBC          # 位置 X（見 TILE_UNITS）
OFF_POS_Y = 0xC0          # 位置 Y
OFF_ID = 0x1C8            # 實體 ID，每隻唯一 —— 攻擊目標就是傳這個
OFF_TYPE = 0x1D0          # 種類 ID；0 = 其他玩家
OFF_NAME = 0x1D4          # 名字，內嵌 UTF-8

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

    @property
    def is_monster(self) -> bool:
        return self.type_id != 0

    def distance_to(self, pos: tuple[float, float] | None) -> float:
        """到某個座標幾格。pos 為 None（玩家位置不明）時回傳無限大。"""
        if pos is None:
            return float("inf")
        return math.hypot(self.x - pos[0], self.y - pos[1])


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
    for base, size in (regions if regions is not None
                       else scanner._iter_regions(writable_only=True)):
        if should_stop is not None and should_stop():
            return out, hot
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        found = False
        for target, bucket in targets:
            for i in np.flatnonzero(arr == target):
                bucket.append(base + int(i) * 4)
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


def locate_player(scanner, should_stop=None) -> int | None:
    """定位玩家自己的物件；找不到（或掃到多個）回傳 None。

    實測每個分身剛好 1 個，且 +0x1D4 的名字就是該帳號的角色名、+0x1D0 型別為 0。
    """
    hits = _scan_vtable(scanner, VT_PLAYER, should_stop)
    return hits[0] if len(hits) == 1 else None


def _build(scanner, addrs: list[int]) -> list[Entity]:
    out: list[Entity] = []
    for addr in addrs:
        raw = scanner._read_bytes(addr + OFF_NAME, NAME_MAX)
        if not raw:
            continue
        try:
            name = raw.split(b"\x00")[0].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not name:
            continue
        pos = read_pos(scanner, addr) or (0.0, 0.0)
        out.append(Entity(addr, _u32(scanner, addr + OFF_ID),
                          _u32(scanner, addr + OFF_TYPE), name, *pos))
    return out


def list_entities(scanner, should_stop=None) -> list[Entity]:
    """列出附近所有實體（怪、NPC、其他玩家）。名字解不出來的會被略過。"""
    return _build(scanner, _scan_vtable(scanner, VT_ENTITY, should_stop))


def monsters(scanner, should_stop=None) -> list[Entity]:
    """只要怪與 NPC，排除其他玩家。"""
    return [e for e in list_entities(scanner, should_stop) if e.is_monster]


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


def read_target(scanner, state: int) -> int:
    """目前選定的目標實體 ID；沒有選定時是 0。"""
    return _u32(scanner, state + OFF_TARGET)


def read_target_hp(scanner, state: int) -> int:
    """目標的血量百分比（0~100）。目標死亡或沒有目標時是 0。"""
    return _u32(scanner, state + OFF_TARGET_HP)


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
