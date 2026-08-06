"""快捷欄（F1~F12 × 4 頁）直接讀：哪個鍵放什麼技能／物品，不必按鍵。

為什麼需要它
------------
掛機要知道「使用者選的那個 F 鍵放的是哪個技能」才能改送封包攻擊。
以前只能「先按幾下那個鍵、再讀『最近使用的技能 ID』欄位」（player.read_last_skill）
—— 會真的放技能、有冷卻／無目標寫不進去的坑一堆。這裡是純讀取，零副作用。

版面（2026-08-06 反組譯 dragquickkey 0x58EFE0 / usequickkey 0x58F2C9）
--------------------------------------------------------------------
    管理物件 = [MGR_PTR]                  ← 快捷欄視窗物件的全域指標
    格子     = 管理物件 + TABLE_OFF + (頁*12 + 格)*9

    每格 9 bytes（⚠ 未對齊，u32 落在奇數位址）：
        +0  型別 u8：0 空、1 技能、2 物品；3/4 少見（拖曳來源是表情／場景物件）
        +1  u32：技能 ID（型別 1）或物品種類 ID（型別 2）
        +5  u32：第二值（只有型別 4 在用）

    Lua 端 OnUseQuickKey：控制項 ID − 211 = 格號 0~11（= F1~F12），
    再呼叫 game.usequickkey(QUICK_COMMAND_PAGE, 格號) —— 所以 F 鍵作用在
    「目前頁」，頁碼存在 Lua 全域 QUICK_COMMAND_PAGE（0~3，共 4 頁）。

    位址算式在組譯裡是 ((頁 + 0xE5) * 12 + 格) * 9 —— 0xE5*12*9 = 0x609C
    就是表在物件內的偏移，編譯器把它折進常數了。

驗證（2026-08-06，五台交叉比對）
--------------------------------
* 雪狐 F5/F6/F9 = 0x279/0x2D7/0x2DF，與 8/3 攔封包記到的技能 ID 完全一致；
  黑狐 F1~F3 = 0xF9/0x101/0x103 也對上。
* 藥水格型別 2、值 4836/4837（高效紅／藍藥水）—— 與精靈設定讀到的一致。
* 空鍵型別 0，對應「沒放東西的鍵按了不會寫最近技能」的舊觀察。

為什麼 8/3 的六種掃法都找不到：一格 9 bytes，技能 ID 全落在**未對齊位址**，
當時的差分掃描把未對齊命中當成繪圖資料流過濾掉了。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import lua

# ⚠ 遊戲改版會位移；locate.py 有 AOB 特徵（錨在 usequickkey 函式頭）會自動跟上。
MGR_PTR = 0x009B66AC

TABLE_OFF = 0x609C
ENTRY_SIZE = 9
PAGES = 4
SLOTS = 12                    # F1~F12

KIND_SKILL = 1
KIND_ITEM = 2

VK_F1 = 0x70                  # F1~F12 = 0x70~0x7B


@dataclass(frozen=True)
class QuickSlot:
    """快捷欄一格。`value`：技能 ID（技能格）或物品種類 ID（物品格）。"""

    kind: int
    value: int
    value2: int

    @property
    def is_skill(self) -> bool:
        return self.kind == KIND_SKILL

    @property
    def is_item(self) -> bool:
        return self.kind == KIND_ITEM


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw[:4]))[0]


def _manager(scanner) -> int | None:
    """快捷欄管理物件；還沒進遊戲（視窗沒建）就是讀不到。"""
    mgr = _u32(scanner, MGR_PTR)
    return mgr if mgr and mgr > 0x10000 else None


def read_page(scanner, page: int) -> list[QuickSlot | None] | None:
    """讀一頁 12 格；空格是 None。整頁讀不到（沒進遊戲／改版位移）回 None。"""
    if not 0 <= page < PAGES:
        return None
    mgr = _manager(scanner)
    if mgr is None:
        return None
    raw = scanner._read_bytes(mgr + TABLE_OFF + page * SLOTS * ENTRY_SIZE,
                              SLOTS * ENTRY_SIZE)
    if not raw or len(raw) < SLOTS * ENTRY_SIZE:
        return None
    b = bytes(raw)
    out: list[QuickSlot | None] = []
    for slot in range(SLOTS):
        off = slot * ENTRY_SIZE
        kind = b[off]
        if kind == 0:
            out.append(None)
            continue
        if kind > 4:
            return None       # 型別只該是 0~4，出現別的 = 版面不對，別亂回值
        a, v2 = struct.unpack_from("<II", b, off + 1)
        out.append(QuickSlot(kind=kind, value=a, value2=v2))
    return out


def read_all(scanner) -> list[list[QuickSlot | None]] | None:
    """四頁全讀（頁 0~3）。任何一頁失敗就整個回 None，不給半套資料。"""
    pages = [read_page(scanner, p) for p in range(PAGES)]
    return None if any(p is None for p in pages) else pages   # type: ignore[return-value]


def current_page(scanner) -> int:
    """目前顯示（＝F 鍵作用）的頁碼 0~3；讀不到就當第 0 頁。

    頁碼在 Lua 全域 QUICK_COMMAND_PAGE —— 純讀走全域表的雜湊節點，
    不呼叫 Lua（跟 tools/dump_lua_globals.py 同一套版面）。
    只在使用者開始掛機時讀一次，一次幾毫秒無所謂。
    """
    v = _lua_number_global(scanner, "QUICK_COMMAND_PAGE")
    return int(v) if v is not None and 0 <= v < PAGES else 0


def skill_on_vk(scanner, vk: int) -> int | None:
    """使用者選的按鍵（虛擬鍵碼）目前對到的技能 ID；不是技能格就回 None。

    只認 F1~F12 —— 其他鍵不在快捷欄上，回 None 讓呼叫端走舊的學法。
    """
    if not VK_F1 <= vk < VK_F1 + SLOTS:
        return None
    page = read_page(scanner, current_page(scanner))
    if page is None:
        return None
    cell = page[vk - VK_F1]
    return cell.value if cell is not None and cell.is_skill else None


def _key_matches(scanner, node_bytes: bytes, want: bytes) -> bool:
    """這個全域表節點（32B）的鍵是不是這個名字的字串。"""
    if struct.unpack_from("<I", node_bytes, 24)[0] != lua.T_STRING:
        return False
    ts = struct.unpack_from("<I", node_bytes, 16)[0]
    head = scanner._read_bytes(ts + lua.OFF_TSTRING_LEN, 4 + len(want))
    if not head or len(head) < 4 + len(want):
        return False
    head = bytes(head)
    return (struct.unpack_from("<I", head, 0)[0] == len(want)
            and head[4:4 + len(want)] == want)


def _node_number(scanner, node_addr: int, name: str) -> float | None:
    """讀一個（先前找到的）節點的數字值。

    每次都重驗鍵名 —— 全域表 rehash 之後節點會搬家，位址被別的鍵接手時
    這裡要擋下來（回 None 讓呼叫端重新走訪），不能把別人的值當頁碼。
    """
    raw = scanner._read_bytes(node_addr, 32)
    if not raw or len(raw) < 32:
        return None
    b = bytes(raw)
    if not _key_matches(scanner, b, name.encode("ascii")):
        return None
    if struct.unpack_from("<I", b, 8)[0] != lua.T_NUMBER:
        return None
    return struct.unpack_from("<d", b, 0)[0]


def _find_global_node(scanner, name: str) -> int | None:
    """走訪整張 Lua 全域表，找這個名字的節點位址；拿不到回 None。

    走訪要讀上千個小字串（一次約 10~20ms），所以值得把結果快取起來 ——
    見 Reader。純讀取，不呼叫 Lua。
    """
    ctx = _u32(scanner, lua.CTX_PTR)
    L = _u32(scanner, ctx + 8) if ctx else None
    if not L:
        return None
    tab = _u32(scanner, L + lua.OFF_L_GT)
    if not tab or _u32(scanner, L + lua.OFF_L_GT + 8) != lua.T_TABLE:
        return None
    lsize = scanner._read_bytes(tab + 0x07, 1)
    node = _u32(scanner, tab + 0x10)
    if not lsize or lsize[0] > 20 or not node:
        return None
    want = name.encode("ascii")
    blob = scanner._read_bytes(node, (1 << lsize[0]) * 32)
    if not blob:
        return None
    b = bytes(blob)
    for off in range(0, len(b) - 31, 32):
        # Node = 32B：值 TValue(16) + 鍵(TString* 8 + tt 4 + next 4)
        if _key_matches(scanner, b[off:off + 32], want):
            return node + off
    return None


def _lua_number_global(scanner, name: str) -> float | None:
    """純讀取撈一個數字型 Lua 全域；拿不到（還沒進場）回 None。"""
    node = _find_global_node(scanner, name)
    return _node_number(scanner, node, name) if node else None


class Reader:
    """綁一個 scanner 的快捷欄讀取器 —— 給「每隔幾秒重讀一次」的地方用。

    唯一的狀態是 QUICK_COMMAND_PAGE 節點位址的快取：第一次要走訪整張
    Lua 全域表（10~20ms），之後每次只剩三、四個小讀取（微秒級）。
    節點會因為表 rehash 搬家，所以每次讀都重驗鍵名，不對就重新走訪。
    """

    def __init__(self, scanner) -> None:
        self._sc = scanner
        self._page_node: int | None = None

    def page(self) -> int:
        """目前顯示（＝F 鍵作用）的頁碼 0~3；讀不到就當第 0 頁。"""
        if self._page_node is not None:
            v = _node_number(self._sc, self._page_node, "QUICK_COMMAND_PAGE")
            if v is not None:
                return int(v) if 0 <= v < PAGES else 0
        self._page_node = _find_global_node(self._sc, "QUICK_COMMAND_PAGE")
        if self._page_node is None:
            return 0
        v = _node_number(self._sc, self._page_node, "QUICK_COMMAND_PAGE")
        return int(v) if v is not None and 0 <= v < PAGES else 0

    def skills(self, vks) -> dict[int, int] | None:
        """這些（F 鍵）鍵碼各對到目前頁上的哪個技能 → {鍵碼: 技能 ID}。

        **只收技能格** —— 空格、物品格、非 F 鍵都不會出現在結果裡
        （使用者指定：非技能不進攻擊循環）。
        整頁讀不到（沒進遊戲／改版位移）回 None，跟「頁上沒技能」的
        空 dict 區分開，呼叫端才知道該不該退回按鍵舊法。
        """
        cells = read_page(self._sc, self.page())
        if cells is None:
            return None
        out: dict[int, int] = {}
        for vk in vks:
            if VK_F1 <= vk < VK_F1 + SLOTS:
                c = cells[vk - VK_F1]
                if c is not None and c.is_skill:
                    out[vk] = c.value
        return out
