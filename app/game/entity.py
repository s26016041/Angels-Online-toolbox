"""附近實體（怪物 / NPC / 其他玩家）與「目前選定的目標」。

這是自動掛機的基礎：列出附近有什麼可以打，以及指定要打誰。

怎麼定位（全部認 vtable，angel.dat 無 ASLR，重開遊戲照樣有效）
--------------------------------------------------------------
    實體物件 = 開頭是 VT_ENTITY 的物件
    狀態物件 = 開頭是 VT_STATE 的物件（實測每個分身剛好 1 個）

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
VT_STATE = 0x007E3E40     # 玩家狀態物件（每個分身剛好 1 個）

# --- 實體物件的欄位（基準 = 掃 VT_ENTITY 得到的位址）---
OFF_PREV = 0x08           # 雙向串列
OFF_NEXT = 0x0C
OFF_ID = 0x1C8            # 實體 ID，每隻唯一 —— 攻擊目標就是傳這個
OFF_TYPE = 0x1D0          # 種類 ID；0 = 其他玩家
OFF_NAME = 0x1D4          # 名字，內嵌 UTF-8

# --- 狀態物件的欄位 ---
OFF_TARGET = 0x2D8        # 目前選定的目標（實體 ID）

NAME_MAX = 40             # 名字最多讀幾 bytes


@dataclass(frozen=True)
class Entity:
    """一個附近的實體。type_id 為 0 代表是其他玩家，不是怪。"""

    addr: int
    eid: int
    type_id: int
    name: str

    @property
    def is_monster(self) -> bool:
        return self.type_id != 0


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", raw)[0] if raw else 0


def _scan_vtable(scanner, vt: int, should_stop=None) -> list[int]:
    """找出所有「開頭是這個 vtable」的物件。要掃全記憶體，約 1～3 秒。

    should_stop: 可選 callable，每個區塊前呼叫；回傳 True 就中止。
    """
    target = np.uint32(vt)
    out: list[int] = []
    for base, size in scanner._iter_regions(writable_only=True):
        if should_stop is not None and should_stop():
            return out
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        for i in np.flatnonzero(arr == target):
            out.append(base + int(i) * 4)
    return out


def locate_state(scanner, should_stop=None) -> int | None:
    """定位玩家狀態物件；找不到回傳 None。

    實測每個分身剛好 1 個。若掃到多個，取第一個並不可靠，所以這裡回傳 None
    ——寧可讓上層知道情況不對，也不要拿錯的物件去寫入。
    """
    hits = _scan_vtable(scanner, VT_STATE, should_stop)
    return hits[0] if len(hits) == 1 else None


def list_entities(scanner, should_stop=None) -> list[Entity]:
    """列出附近所有實體（怪、NPC、其他玩家）。名字解不出來的會被略過。"""
    out: list[Entity] = []
    for addr in _scan_vtable(scanner, VT_ENTITY, should_stop):
        raw = scanner._read_bytes(addr + OFF_NAME, NAME_MAX)
        if not raw:
            continue
        try:
            name = raw.split(b"\x00")[0].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not name:
            continue
        out.append(Entity(addr, _u32(scanner, addr + OFF_ID),
                          _u32(scanner, addr + OFF_TYPE), name))
    return out


def monsters(scanner, should_stop=None) -> list[Entity]:
    """只要怪與 NPC，排除其他玩家。"""
    return [e for e in list_entities(scanner, should_stop) if e.is_monster]


def read_target(scanner, state: int) -> int:
    """目前選定的目標實體 ID；沒有選定時是 0。"""
    return _u32(scanner, state + OFF_TARGET)


def set_target(scanner, state: int, eid: int) -> None:
    """把目標寫進客戶端狀態。之後送出攻擊按鍵，角色就會打這隻。

    只寫這 4 bytes —— 這是遊戲自己每次選目標都會寫的欄位。
    """
    scanner.write_value(state + OFF_TARGET, VALUE_TYPES["int32"], eid)


def is_alive(scanner, ent: Entity) -> bool:
    """這個實體物件還在嗎？（怪死掉 / 離開視野後物件會被回收再利用）

    用「vtable 還在、而且實體 ID 沒變」判斷 —— 只看 vtable 不夠，
    因為同一塊記憶體很快就會被下一隻怪拿去用。
    """
    return (_u32(scanner, ent.addr) == VT_ENTITY
            and _u32(scanner, ent.addr + OFF_ID) == ent.eid)
