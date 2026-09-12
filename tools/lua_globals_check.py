r"""lua_globals_check — `lua.globals_of` 索引快取的回歸（離線、秒殺）。

    py tools\lua_globals_check.py

為什麼要有它（2026-09-12，使用者：「對話 NPC 很慢」「都卡很久才知道對話框出現」）：
判「對話框開了沒」唯一的純讀路徑就是 `lua.globals_of`（`WND_MESSAGE`／`MESSAGE_*`），
舊寫法**一個節點一次 ReadProcessMemory**、一次呼叫近萬次 syscall（實測 ~38ms），
所有呼叫端只好加 0.3~0.5 秒節流 → 對話框早就開了卻要等半秒才看到。
改成「索引快取＋一次讀整塊節點陣列」之後，這支測試要證明三件事：

  ① **答案一個字都沒變** —— 拿舊演算法當參考實作，對同一份假記憶體逐一比對。
  ② **真的變快** —— 數 ReadProcessMemory 的次數（穩定狀態下少兩個數量級）。
  ③ **不會安靜地做錯事** —— 表搬家（rehash）、某一格的鍵換人住、後來才新增的
     全域、整塊讀不到、字串跨頁讀不到，這五種情況答案都還要對。

⚠ 全部用假記憶體（FakeScanner），不需要開遊戲。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import lua                                 # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 假記憶體：照 Lua 5.1 的版面擺一張全域表
# ---------------------------------------------------------------------------
CTX = 0x01000000
L_ADDR = 0x02000000
TAB = 0x03000000
NODE = 0x04000000
TS_BASE = 0x05000000


class FakeScanner:
    """一塊假記憶體＋讀取次數計數器。`_read_bytes` 的語意跟真的一樣：
    要求的範圍只要有一個 byte 沒定義就整發回 None（ReadProcessMemory 的行為）。"""

    pid = 4321

    def __init__(self, blobs: dict, hole: set | None = None):
        self.mem = bytearray(0x100)
        self.map: dict = {}
        for addr, data in blobs.items():
            self.map[addr] = bytes(data)
        self.reads = 0
        self.bytes_read = 0
        self.hole = hole or set()        # 這些 (addr, size) 假裝跨頁讀失敗
        self.no_big = False              # True＝整塊讀一律失敗（退路測試）

    def _read_bytes(self, addr: int, size: int):
        self.reads += 1
        self.bytes_read += size
        if (addr, size) in self.hole:
            return None
        if self.no_big and size > 0x200:
            return None
        out = bytearray()
        for i in range(size):
            b = self._byte(addr + i)
            if b is None:
                return None
            out.append(b)
        return bytes(out)

    def _byte(self, addr: int):
        for base, data in self.map.items():
            if base <= addr < base + len(data):
                return data[addr - base]
        return None


def _tv(value, tt: int) -> bytes:
    """TValue（16 bytes）：值 8 ＋ tt 4 ＋ 補 4。"""
    if tt == lua.T_NUMBER:
        raw = struct.pack("<d", float(value))
    else:
        raw = struct.pack("<II", int(value), 0)
    return raw + struct.pack("<II", tt, 0)


def build(entries, lsize: int = 4, node: int = NODE, tab: int = TAB,
          gt_tt: int = lua.T_TABLE, **kw) -> FakeScanner:
    """`entries` = [(節點序號, 名字, 值, 值的型別)]；名字 None＝這一格不是字串鍵。"""
    count = 1 << lsize
    nodes = bytearray(count * lua._NODE)
    blobs = {
        lua.CTX_PTR: struct.pack("<I", CTX),
        CTX + 8: struct.pack("<I", L_ADDR),
        L_ADDR + lua.OFF_L_GT: struct.pack("<I", tab) + struct.pack("<II", 0, gt_tt),
        tab + 7: bytes([lsize]),
        tab + 0x10: struct.pack("<I", node),
    }
    for i, name, value, tt in entries:
        off = i * lua._NODE
        nodes[off:off + 16] = _tv(value, tt)
        if name is None:
            nodes[off + lua._K_TT:off + lua._K_TT + 4] = struct.pack(
                "<I", lua.T_NIL)
            continue
        ts = TS_BASE + i * 0x80
        nodes[off + lua._K_VAL:off + lua._K_VAL + 4] = struct.pack("<I", ts)
        nodes[off + lua._K_TT:off + lua._K_TT + 4] = struct.pack(
            "<I", lua.T_STRING)
        raw = name.encode("ascii")
        blobs[ts + lua.OFF_TSTRING_LEN] = struct.pack("<I", len(raw))
        blobs[ts + lua.OFF_TSTRING_DATA] = raw + bytes(
            4 + lua._NAME_MAX)        # 後面留白，一次讀 67 bytes 也讀得到
    blobs[node] = bytes(nodes)
    return FakeScanner(blobs, **kw)


def reset() -> None:
    """清索引快取（每個案例之間都要清，否則上一個案例的索引會被沿用）。"""
    lua._index.clear()
    lua._rescan_t.clear()


# ---------------------------------------------------------------------------
# 參考實作＝**改之前**那段程式（一個節點一次讀），用來證明答案沒變
# ---------------------------------------------------------------------------
def reference(scanner, names):
    want = set(names)
    L = lua.state(scanner)
    if L is None:
        return None
    tab = lua._u32(scanner, L + lua.OFF_L_GT)
    if not 0x10000 < tab < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(tab + 7, 1)
    node = lua._u32(scanner, tab + 0x10)
    if not raw or not 0x10000 < node < 0x7FFF0000:
        return None
    lsize = raw[0]
    if lsize > 20:
        return None
    out: dict = {}
    for i in range(1 << lsize):
        blob = scanner._read_bytes(node + i * 32, 32)
        if not blob or len(blob) < 32:
            continue
        b = bytes(blob)
        if struct.unpack_from("<I", b, 24)[0] != lua.T_STRING:
            continue
        ts = struct.unpack_from("<I", b, 16)[0]
        n = lua._u32(scanner, ts + lua.OFF_TSTRING_LEN)
        if not n or not 0 < n < 64:
            continue
        s = scanner._read_bytes(ts + lua.OFF_TSTRING_DATA, n)
        if not s:
            continue
        try:
            name = bytes(s).decode("ascii")
        except UnicodeDecodeError:
            continue
        if name not in want:
            continue
        vtt = struct.unpack_from("<I", b, 8)[0]
        if vtt == lua.T_NUMBER:
            v = struct.unpack_from("<d", b, 0)[0]
            out[name] = v if v != v or v in (float("inf"), float("-inf")) \
                else (int(v) if v == int(v) else v)
        elif vtt == lua.T_BOOL:
            out[name] = bool(struct.unpack_from("<I", b, 0)[0])
        elif vtt == lua.T_NIL:
            pass
        else:
            out[name] = vtt
    return out


SAMPLE = [
    (1, "WND_MESSAGE", 0x1234, lua.T_NUMBER),
    (2, "MESSAGE_MSG_ID", 9001, lua.T_NUMBER),
    (3, "MESSAGE_IS_TALK", 1, lua.T_BOOL),
    (4, "MESSAGE_OPTION1", 5190, lua.T_NUMBER),
    (5, "MESSAGE_OPTION2", 0, lua.T_NUMBER),
    (6, "WND_BANK", 0, lua.T_NIL),                 # nil ＝ 沒有這個全域
    (7, "SOME_TABLE", 0x777, lua.T_TABLE),         # 別的型別 → 回型別碼
    (8, None, 0, lua.T_NUMBER),                    # 不是字串鍵的格子
    (9, "MESSAGE_FACE", 12.5, lua.T_NUMBER),       # 非整數
]
WANT = ["WND_MESSAGE", "MESSAGE_MSG_ID", "MESSAGE_IS_TALK", "MESSAGE_OPTION1",
        "MESSAGE_OPTION2", "WND_BANK", "SOME_TABLE", "MESSAGE_FACE",
        "NOT_THERE_AT_ALL"]


def main() -> int:
    print("① 答案跟舊演算法一致")
    reset()
    sc = build(SAMPLE)
    got = lua.globals_of(sc, WANT)
    ref = reference(build(SAMPLE), WANT)
    check("值完全一致", got == ref, f"新={got} 舊={ref}")
    check("nil 不出現在結果裡", "WND_BANK" not in (got or {}))
    check("不存在的名字不出現", "NOT_THERE_AT_ALL" not in (got or {}))
    check("別的型別回型別碼", (got or {}).get("SOME_TABLE") == lua.T_TABLE)
    check("非整數保留小數", (got or {}).get("MESSAGE_FACE") == 12.5)
    check("布林是 True", (got or {}).get("MESSAGE_IS_TALK") is True)

    print("② 真的變快（數 ReadProcessMemory 次數）")
    # 大表：4096 格、2800 個字串鍵 —— 跟實機一個量級（reports/lua_allglobals 2817 行）
    big = [(i, f"G_{i:04d}", i, lua.T_NUMBER) for i in range(2800)]
    big[1] = (1, "WND_MESSAGE", 0x1234, lua.T_NUMBER)
    reset()
    sc_new = build(big, lsize=12)
    first = lua.globals_of(sc_new, WANT)
    n_build = sc_new.reads
    sc_new.reads = 0
    for _ in range(10):
        lua.globals_of(sc_new, WANT)
    n_warm = sc_new.reads / 10.0
    sc_old = build(big, lsize=12)
    reference(sc_old, WANT)
    n_old = sc_old.reads
    print(f"    舊：{n_old} 次/呼叫　新：建索引 {n_build} 次、"
          f"之後 {n_warm:.0f} 次/呼叫（省 {n_old / max(n_warm, 1):.0f}×）")
    check("穩定狀態下 ≤ 20 次讀取", n_warm <= 20, f"{n_warm}")
    check("建索引也比舊版少", n_build < n_old, f"{n_build} vs {n_old}")
    check("大表答案照樣對", (first or {}).get("WND_MESSAGE") == 0x1234)

    print("③ 表搬家（rehash 換了節點陣列位址）")
    reset()
    sc = build(SAMPLE)
    lua.globals_of(sc, WANT)
    moved = build([(1, "WND_MESSAGE", 0x9999, lua.T_NUMBER)],
                  node=NODE + 0x8000)
    got = lua.globals_of(moved, WANT)
    check("換位址後讀到新值", (got or {}).get("WND_MESSAGE") == 0x9999,
          str(got))

    print("④ 同一格的鍵換人住了（讀得到 ≠ 還是它）")
    reset()
    sc = build(SAMPLE)
    lua.globals_of(sc, WANT)
    # 把 1 號格換成別的全域（TString 位址不同）→ 舊索引指著它就會拿到別人的值
    swapped = build([(1, "SOMETHING_ELSE", 0x4242, lua.T_NUMBER),
                     (2, "WND_MESSAGE", 0x5555, lua.T_NUMBER)])
    got = lua.globals_of(swapped, WANT)
    check("不會把別人的值當成 WND_MESSAGE",
          (got or {}).get("WND_MESSAGE") in (0x5555, None), str(got))
    check("重建後讀到搬到別格的那個值",
          (got or {}).get("WND_MESSAGE") == 0x5555, str(got))

    print("⑤ 後來才新增的全域（同一塊節點陣列，索引裡本來沒有）")
    reset()
    sc = build(SAMPLE)
    lua.globals_of(sc, WANT)
    added = build(SAMPLE + [(11, "WND_NPCSALE", 0x321, lua.T_NUMBER)])
    got = lua.globals_of(added, ["WND_NPCSALE"])
    check("節流期內先當沒有（不會亂掃）", not got, str(got))
    lua._rescan_t.clear()                      # 假裝已經過了 _RESCAN_GAP 秒
    got = lua.globals_of(added, ["WND_NPCSALE"])
    check("節流過後重掃就讀到了", (got or {}).get("WND_NPCSALE") == 0x321,
          str(got))

    print("⑥ 整塊讀不到 → 退回逐節點讀")
    reset()
    sc = build(SAMPLE)
    sc.no_big = True
    got = lua.globals_of(sc, WANT)
    check("答案還是對", (got or {}).get("WND_MESSAGE") == 0x1234, str(got))

    print("⑦ 字串一次讀 67 bytes 踩到頁尾 → 退回兩次讀")
    reset()
    ts1 = TS_BASE + 1 * 0x80
    sc = build(SAMPLE, hole={(ts1 + lua.OFF_TSTRING_LEN, 4 + lua._NAME_MAX)})
    got = lua.globals_of(sc, WANT)
    check("名字照樣讀得到", (got or {}).get("WND_MESSAGE") == 0x1234, str(got))

    print("⑧ 版面對不上／讀不到 → None（⛔ 不是空字典）")
    reset()
    check("全域表不是 table 回 None",
          lua.globals_of(build(SAMPLE, gt_tt=lua.T_NUMBER), WANT) is None)
    reset()
    check("節點陣列位址不合理回 None",
          lua.globals_of(build(SAMPLE, node=0x10), WANT) is None)
    reset()
    check("lsize 荒謬回 None",
          lua.globals_of(build(SAMPLE, lsize=21), WANT) is None)

    print()
    if FAILS:
        print(f"✘ {len(FAILS)} 項沒過：" + "、".join(FAILS))
        return 1
    print("✔ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
