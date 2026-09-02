r"""傳點／觸發物件模組的回歸測試（不必開遊戲）。

    py tools\portal_check.py

驗的是三件事，每一件都是 CLAUDE.md 明列的失效模式：
  ① 動作碼要跟遊戲的判斷式一模一樣（安靜地算錯 = bug）。
  ② 認不出來（AOB 沒定位到／讀不到）一律回 **None**，不可以回空清單
     —— 「這裡沒有傳點」跟「我讀不到」是完全不同的兩句話。
  ③ 去重欄的語意：記著我 = 站著不動不會再送，要退開再進。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import entity, move, portal                # noqa: E402

PASS = FAIL = 0


def ck(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}　{note}")


class FakeScanner:
    """最小的假記憶體：位址 → bytes。沒填的位址一律讀不到（回 None）。"""

    def __init__(self) -> None:
        self.mem: dict[int, bytes] = {}

    def put(self, addr: int, raw: bytes) -> None:
        self.mem[addr] = raw

    def put_u32(self, addr: int, val: int) -> None:
        self.mem[addr] = struct.pack("<I", val)

    def _read_bytes(self, addr: int, n: int):
        for base, raw in self.mem.items():
            if base <= addr and addr + n <= base + len(raw):
                return raw[addr - base:addr - base + n]
        return None


THUNK = 0x00546E2B          # vtable 第 3 槽放的跳板
VT = 0x00803804             # CStaticObject 的 vtable
MGR = 0x03000000
TBL = 0x03100000
OBJ0 = 0x04000000           # 真的觸發物件
OBJ1 = 0x04010000           # 別的類別（vtable 沒有那支）
MY_EID = 0xF580075B


def thunk_bytes(target: int, at: int = THUNK) -> bytes:
    """`56 8B F1 E8 rel32 8B CE 5E E9 rel32` —— 尾巴 jmp 指向 target。"""
    rel = target - (at + portal.THUNK_LEN)
    return (b"\x56\x8b\xf1\xe8\x00\x00\x00\x00\x8b\xce\x5e\xe9"
            + struct.pack("<i", rel))


def make_object(sc: FakeScanner, addr: int, vt: int, oid: int,
                flags: int, x: float, y: float, last: int = 0,
                throttle: int = 0, model: int = 60001) -> None:
    buf = bytearray(portal._SPAN)
    struct.pack_into("<I", buf, 0, vt)
    struct.pack_into("<I", buf, move.MGR.OBJ_ID, oid)
    struct.pack_into("<I", buf, portal.OFF_MODEL, model)
    struct.pack_into("<I", buf, portal.OFF_SELECT_ID, 0x5E1400D3)
    struct.pack_into("<H", buf, portal.OFF_FLAGS, flags)
    struct.pack_into("<i", buf, portal.OFF_THROTTLE, throttle)
    struct.pack_into("<I", buf, portal.OFF_LAST, last)
    struct.pack_into("<II", buf, portal.E + entity.OFF_POS_X,
                     int(x * entity.TILE_UNITS) << 16,
                     int(y * entity.TILE_UNITS) << 16)
    sc.put(addr, bytes(buf))


def build(objs=((OBJ0, VT, 0x11110000, 0x8104, 252.0, 24.0),
                (OBJ1, 0x00803188, 0x22220001, 0xFFFF, 253.0, 24.0))
          ) -> FakeScanner:
    sc = FakeScanner()
    sc.put_u32(move.MGR_PTR, MGR)
    sc.put_u32(MGR + move.MGR.TBL, TBL)
    # ⚠ MAX 是「表格上限」，nearby() 會讀 MAX+1 格；只有一個物件時也要留一格
    #   空的（真遊戲的表本來就有很多空格），不然 MAX=0 會被合理性檢查擋掉。
    mx = max(len(objs), 1)
    sc.put_u32(MGR + move.MGR.MAX, mx)
    sc.put(TBL, b"".join(struct.pack("<I", a) for a, *_ in objs)
           + b"\x00" * 4 * (mx + 1 - len(objs)))
    for i, (addr, vt, oid, flags, x, y) in enumerate(objs):
        make_object(sc, addr, vt, (oid & 0xFFFF0000) | i, flags, x, y)
    # 只有 VT 這個類別的第 3 槽是那支跳板
    sc.put_u32(VT + portal.VT_SLOT, THUNK)
    sc.put_u32(0x00803188 + portal.VT_SLOT, 0x00500000)
    sc.put(THUNK, thunk_bytes(portal.TRIGGER_FN))
    sc.put(0x00500000, b"\x55\x8b\xec" + b"\x90" * 13)
    return sc


def main() -> int:
    print("① 動作碼（照 0x546A62~0x546A9E 的判斷式）")
    ck("旗標 bit2 → 碼 3（實測進副本那一下就是這個）",
       portal.code_for(0x8104) == 3)
    ck("旗標只有 bit0 → 碼 1", portal.code_for(0x8001) == 1)
    ck("bit2 優先於 bit0", portal.code_for(0x0005) == 3)
    ck("兩個位元都沒有 → 不會送（None）", portal.code_for(0x0002) is None)
    ck("bit27 那條：bit8 → 碼 9", portal.code_for(0x0100, True) == 9)
    ck("bit27 那條：bit15 → 碼 8", portal.code_for(0x8000, True) == 8)
    ck("bit27 那條：都沒有 → None", portal.code_for(0x0004, True) is None)
    ck("觸發遮罩就是遊戲的 test eax,0x185", portal.TRIGGER_MASK == 0x185)

    print("② 認不出來要回 None，不可以回空清單")
    sc = build()
    got = portal.nearby(sc)
    ck("正常情況掃得到那一個觸發物件",
       got is not None and len(got) == 1, f"got={got}")
    if got:
        t = got[0]
        ck("座標讀對", abs(t.x - 252.0) < 0.05 and abs(t.y - 24.0) < 0.05,
           f"({t.x}, {t.y})")
        ck("動作碼跟旗標算得起來", t.code == 3)
        ck("封包裡那個 id 讀的是 +0x1D0（不是物件表的 id）",
           t.select_id == 0x5E1400D3 and t.oid != t.select_id)
    ck("別的類別（vtable 沒有那支）不會被當成傳點",
       got is not None and all(t.addr != OBJ1 for t in got))

    keep = portal.TRIGGER_FN
    try:
        portal.TRIGGER_FN = 0            # 模擬 AOB 定位失敗
        ck("TRIGGER_FN 定位失敗 → nearby() 回 None（大聲停用）",
           portal.nearby(sc) is None)
        ck("定位失敗時 class_triggers() 一律 False",
           portal.class_triggers(sc, VT) is False)
    finally:
        portal.TRIGGER_FN = keep

    ck("讀不到管理器 → None（不是空清單）",
       portal.nearby(FakeScanner()) is None)

    bad = build()
    bad.put(THUNK, b"\x55\x8b\xec" + b"\x90" * 13)   # 槽裡不是那支跳板
    ck("vtable 第 3 槽換成別支 → 一個都不算（不亂報）",
       portal.nearby(bad) == [])
    bad2 = build()
    bad2.put(THUNK, thunk_bytes(portal.TRIGGER_FN + 0x10))  # jmp 到別的地方
    ck("跳板 jmp 到別支 → 不算觸發物件", portal.nearby(bad2) == [])

    stale = build(((OBJ0, VT, 0x11110000, 0x8104, 252.0, 24.0),))
    # 表格第 0 格指著它，但物件自己記的 id 低 16 位是 7 ＝ 回收後的殘留
    make_object(stale, OBJ0, VT, 0x11110007, 0x8104, 252.0, 24.0)
    ck("物件回存的 id 跟表格索引對不上（殘留）→ 跳過",
       portal.nearby(stale) == [])

    print("③ 去重欄：記著我 = 不會再送，要先退開")
    sc2 = build()
    pf = 0x05000000
    sc2.put_u32(pf + move.MGR.OBJ_ID, MY_EID)
    t = portal.nearby(sc2)[0]
    ck("沒人踩過 → 踩上去會送", portal.armed(sc2, t, pf) is True)
    make_object(sc2, OBJ0, VT, 0x11110000, 0x8104, 252.0, 24.0, last=MY_EID)
    ck("去重欄已經記著我 → 不會再送（要退開再進）",
       portal.armed(sc2, t, pf) is False)
    make_object(sc2, OBJ0, VT, 0x11110000, 0x8104, 252.0, 24.0,
                last=0x11223344)
    ck("去重欄記著別人 → 我踩上去照樣會送",
       portal.armed(sc2, t, pf) is True)
    ck("讀不到玩家 → None（不知道，不下結論）",
       portal.armed(sc2, t, 0) is None)
    ck("物件不見了 → armed() 回 None",
       portal.armed(FakeScanner(), t, pf) is None)

    print("④ still_there / read")
    ck("同一個物件 → still_there 成立", portal.still_there(sc2, t) is True)
    gone = build()
    make_object(gone, OBJ0, VT, 0x99990000, 0x8104, 252.0, 24.0)
    ck("位址被別的東西佔走（id 變了）→ still_there 不成立",
       portal.still_there(gone, t) is False)
    ck("read() 拿得到即時的去重欄",
       (portal.read(sc2, t) or portal.read(sc2, t)).last == 0x11223344)

    print("⑤ enter()：送出前重讀重驗、按一下只送一發")

    class FakeMover:
        """假跳板：只記下「被叫了什麼」，不真的呼叫遊戲。"""

        def __init__(self, ok: bool = True) -> None:
            self.ok, self.calls = ok, []

        def call(self, fn, *a, **kw):
            self.calls.append((fn, a))
            return self.ok

    sc3 = build()
    sc3.put_u32(pf + move.MGR.OBJ_ID, MY_EID)
    sc3.put_u32(pf + portal.OFF_SELECT_ID, 0x14890089)
    t3 = portal.nearby(sc3)[0]
    mv = FakeMover()
    ok, msg = portal.enter(mv, sc3, t3, pf)
    ck("送得出去", ok, msg)
    ck("參數版面＝(玩家+0x1D0, 物件+0x1D0, 碼)　⛔ 不是物件表那個 id",
       mv.calls == [(portal.SEND_FN, (0x14890089, 0x5E1400D3, 3))],
       str(mv.calls))
    ck("按一下只送一發", len(mv.calls) == 1)

    busy = FakeMover(ok=False)
    ok, msg = portal.enter(busy, sc3, t3, pf)
    ck("指令槽忙碌 → 回 False（⛔ 不可以當成送出去了）",
       ok is False and "沒送出去" in msg, msg)

    keep2 = portal.SEND_FN
    try:
        portal.SEND_FN = 0
        ok, msg = portal.enter(FakeMover(), sc3, t3, pf)
        ck("SEND_FN 定位失敗 → 不叫遊戲（大聲停用）", ok is False, msg)
    finally:
        portal.SEND_FN = keep2

    dead = build()
    make_object(dead, OBJ0, VT, 0x99990000, 0x8104, 252.0, 24.0)
    mv2 = FakeMover()
    ok, _ = portal.enter(mv2, dead, t3, pf)
    ck("物件被回收（id 變了）→ 一包都不送", ok is False and not mv2.calls)

    nosend = build()
    nosend.put_u32(pf + move.MGR.OBJ_ID, MY_EID)
    nosend.put_u32(pf + portal.OFF_SELECT_ID, 0x14890089)
    make_object(nosend, OBJ0, VT, 0x11110000, 0x0002, 252.0, 24.0)
    mv3 = FakeMover()
    ok, _ = portal.enter(mv3, nosend, t3, pf)
    ck("旗標算出來「不會送」→ 我們也不送", ok is False and not mv3.calls)

    noid = build()
    noid.put_u32(pf + move.MGR.OBJ_ID, MY_EID)
    noid.put_u32(pf + portal.OFF_SELECT_ID, 0x14890089)
    buf = bytearray(noid.mem[OBJ0])
    struct.pack_into("<I", buf, portal.OFF_SELECT_ID, 0)
    noid.put(OBJ0, bytes(buf))
    mv4 = FakeMover()
    ok, _ = portal.enter(mv4, noid, t3, pf)
    ck("物件的 +0x1D0 是 0 → 不送（不拿 0 給遊戲）",
       ok is False and not mv4.calls)

    print(f"\n通過 {PASS} 項，失敗 {FAIL} 項。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
