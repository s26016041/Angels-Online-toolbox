"""物品欄：直接讀「哪一格裝了什麼」，不必再靠行為猜。

為什麼需要這個
--------------
原本判斷「哪幾顆球是裝備中」是靠「練功時它會增加」——那只是行為推論，不準：
球一滿就不再增加、剛裝上的球還沒開始漲、記憶體裡的舊副本也會混進來。
玩家快速換飾品時甚至會出現張冠李戴（35000 的球配到 120000 的上限）。

實際上遊戲有一張**物品指標陣列**，裝備欄與背包都在裡面，直接讀就是精確答案。

版面（跨 5 個分身驗證一致）
---------------------------
    陣列表頭 +0x00 起是裝備欄，每格 4 bytes 是一個指標（0 = 該格沒裝東西）
      第 8 格（+0x20）＝ 飾品欄 1
      第 9 格（+0x24）＝ 飾品欄 2
    第 12 格之後是空的裝備格，再往後（實測 +0x50 起）是背包。

    指標指向物品結構：
      結構 +0x08 = 種類 ID（就是 items.py 那張表的鍵）
      結構 +0xA0 = 技能經驗球的累積值（也就是 AOB 特徵找到的那個位址）

怎麼定位這張表
--------------
用 AOB 找到任一顆球的結構位址後，反向搜「誰精確指著它」——那個位置就是它所在的
格子，再往前掃過空格就到表頭。表頭確認方式：前 64 格裡要有夠多有效的物品指標
（實測真表有 90 幾個，巧合命中的只有零星一兩個）。

定位一次之後每輪只要讀兩個指標，成本趨近於零，而且換裝當下就會反映。
純讀記憶體。
"""
from __future__ import annotations

import struct

import numpy as np

# 陣列裡的格子索引
SLOT_ACCESSORY = (8, 9)      # 飾品欄兩格
EQUIP_SLOTS = 12             # 前 12 格是裝備欄，之後是空格與背包

# 物品結構內的偏移
ITEM_TYPE_OFF = 0x08         # 種類 ID
ITEM_BALL_OFF = 0xA0         # 技能經驗球的值

# 判定表頭用：前這麼多格裡至少要有這麼多個有效物品指標
HEAD_WINDOW = 64
HEAD_MIN_ITEMS = 12
MAX_ITEM_ID = 200_000


def _dword(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", raw)[0]


def item_type(scanner, ptr: int) -> int | None:
    """這個指標指到的像不像一個物品結構？是的話回傳種類 ID。"""
    if not (0x10000 < ptr < 0x7FFF0000):
        return None
    raw = scanner._read_bytes(ptr + ITEM_TYPE_OFF, 4)
    if not raw:
        return None
    tid = struct.unpack("<i", raw)[0]
    return tid if 0 < tid < MAX_ITEM_ID else None


def _head_from(scanner, slot: int) -> int:
    """從某一格往前掃到表頭。空格（0）算陣列的一部分，垃圾值代表已經越界。"""
    at = slot
    while at > 0x10000 and slot - at < 0x1000:
        p = _dword(scanner, at - 4)
        if p is None or (p != 0 and item_type(scanner, p) is None):
            break
        at -= 4
    return at


def _looks_like_inventory(scanner, head: int) -> bool:
    """真的物品陣列前 64 格會塞滿有效指標；巧合命中的只有零星一兩個。"""
    n = 0
    for i in range(HEAD_WINDOW):
        p = _dword(scanner, head + i * 4)
        if p is None:
            break
        if p and item_type(scanner, p) is not None:
            n += 1
            if n >= HEAD_MIN_ITEMS:
                return True
    return False


def locate(scanner, item_structs) -> int | None:
    """用已知的物品結構位址反向找出物品陣列表頭；找不到回傳 None。

    item_structs: 可疊代的物品結構起點（技能球的話 = AOB 命中位址 - ITEM_BALL_OFF）。
    需要全記憶體掃一次（跟 AOB 掃描同一個等級的成本），所以只在還沒定位、
    或既有表頭失效時才呼叫。
    """
    targets = np.array(sorted(set(item_structs)), dtype="<u4")
    if targets.size == 0:
        return None
    slots: list[int] = []
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        for i in np.flatnonzero(np.isin(arr, targets)):
            slots.append(base + int(i) * 4)
    seen = set()
    for at in sorted(slots):
        head = _head_from(scanner, at)
        if head in seen:
            continue
        seen.add(head)
        if _looks_like_inventory(scanner, head):
            return head
    return None


def read_slot(scanner, head: int, index: int) -> tuple[int, int, int] | None:
    """讀某一格：回傳 (種類 ID, 物品結構位址, 球值)；空格或無效回傳 None。

    球值對非球物品沒有意義，呼叫端要自己用種類 ID 判斷。
    """
    p = _dword(scanner, head + index * 4)
    if not p:
        return None
    tid = item_type(scanner, p)
    if tid is None:
        return None
    raw = scanner._read_bytes(p + ITEM_BALL_OFF, 4)
    val = struct.unpack("<i", raw)[0] if raw else 0
    return tid, p, val


def accessories(scanner, head: int) -> list[tuple[int, int, int]]:
    """飾品欄兩格的內容（空格會被略過）。"""
    out = []
    for i in SLOT_ACCESSORY:
        got = read_slot(scanner, head, i)
        if got:
            out.append(got)
    return out


def scan_slots(scanner, head: int, count: int = 128):
    """走一遍整張表，回傳 [(格號, 種類 ID, 結構位址, 球值), ...]。

    用來數背包裡還有幾顆球。128 格是保守上限，讀不到就提早停。
    """
    out = []
    for i in range(count):
        p = _dword(scanner, head + i * 4)
        if p is None:
            break
        if not p:
            continue
        tid = item_type(scanner, p)
        if tid is None:
            continue
        raw = scanner._read_bytes(p + ITEM_BALL_OFF, 4)
        out.append((i, tid, p, struct.unpack("<i", raw)[0] if raw else 0))
    return out


def is_valid(scanner, head: int) -> bool:
    """表頭還有效嗎？（換地圖 / 重連會讓它搬家）"""
    return bool(head) and _looks_like_inventory(scanner, head)
