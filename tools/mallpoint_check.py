"""商城點數的離線驗證 —— **跑真的 `app/game/mall.py`**，只換掉記憶體 I/O。

2026-08-23 使用者：「當商城點數不夠要通知不要卡在那邊」＋
「另外一次換兩顆應該是缺 90」。

驗的規格：
    ① 點數讀得到（商品表有載入時）
    ② 商品表**沒**載入 → `points()` 回 None（不知道 ≠ 沒點數）
    ③ 缺一顆＝45 點、**缺兩顆＝90 點**（逐項查價，不是拿第一顆乘）
    ④ 商城倉庫已經有現成的那顆**不算錢**（restock 直接領）
    ⑤ 點數不夠 → `blocked()` 擋下、訊息帶 SHORT_TAG（呼叫端據此通知）
    ⑥ 點數剛好夠 → 不擋
    ⑦ 點數讀不到 → **不擋**（讀不到不是「沒錢」的證據）
    ⑧ `buy()` 送出**前**當場重讀點數：不夠就一發都不送

⚠⚠ 為什麼不用「假的 mall 模組」：替身跟真的不一樣就是測到替身
   （[[test-via-button]] 記著踩過三次）。這裡的假貨只到 `_read_bytes`
   為止 —— 版面、驗證、算錢全部走真的那一份。

    py tools\\mallpoint_check.py
"""
from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.game import gather, mall                       # noqa: E402

FAILS: list[str] = []
MGR = 0x30000000                    # 假的世界管理器
TABLE = 0x20000000                  # 假的商品表基底


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeMem:
    """只實作 `_read_bytes`：位址 → 位元組。其他一律讀不到（回 None）。"""

    def __init__(self) -> None:
        self.mem: dict[int, bytes] = {}
        self.write(gather.WORLD_PTR, struct.pack("<I", MGR))
        self.set_points(0)
        self.set_storage([])
        self.set_goods({})

    # -- 擺放 ---------------------------------------------------------
    def write(self, addr: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            self.mem[addr + i] = bytes([byte])

    def set_points(self, val: int) -> None:
        self.write(MGR + mall.POINTS_OFF, struct.pack("<I", val))

    def set_storage(self, rows) -> None:
        """rows = [(流水號, 道具編號, 數量)]，其餘格清空。"""
        for slot in range(mall.STORAGE_SLOTS):
            base = MGR + mall.STORAGE_OFF + slot * mall.STORAGE_STRIDE
            self.write(base, b"\0" * mall.STORAGE_STRIDE)
        for slot, (serial, tid, num) in enumerate(rows):
            base = MGR + mall.STORAGE_OFF + slot * mall.STORAGE_STRIDE
            self.write(base + mall.ST_SERIAL, struct.pack("<I", serial))
            self.write(base + mall.ST_ITEM, struct.pack("<I", tid))
            self.write(base + mall.ST_COUNT, struct.pack("<H", num))

    def set_goods(self, items) -> None:
        """items = {商城編號: (道具編號, 一份幾個, 售價)}；空 dict ＝ 表沒載入。"""
        self.write(mall.TABLE_PTR, struct.pack("<I", TABLE))
        self.write(TABLE + 4, b"\0" * (mall.MAX_ID * 4))       # 全部清成 0
        for n, (tid, count, price) in items.items():
            rec = TABLE + 0x10000 + n * mall.G_SPAN
            self.write(TABLE + 4 + (n - 1) * 4, struct.pack("<I", rec))
            self.write(rec, b"\0" * mall.G_SPAN)
            self.write(rec + mall.G_ITEM, struct.pack("<i", tid))
            self.write(rec + mall.G_NUM, struct.pack("<i", count))
            self.write(rec + mall.G_PRICE, struct.pack("<i", price))
            self.write(rec + mall.G_CAT, struct.pack("<i", 30))
            self.write(rec + mall.G_ORDER, struct.pack("<i", n))

    # -- 記憶體介面 ----------------------------------------------------
    def _read_bytes(self, addr: int, size: int):
        out = bytearray()
        for i in range(size):
            byte = self.mem.get(addr + i)
            if byte is None:
                return None                     # 沒擺的位址＝讀不到
            out += byte
        return bytes(out)


class Ball:
    """缺的備球（`balls.pick_spares` 回來的那種，只有這兩個欄位會被用到）。"""

    def __init__(self, type_id: int, name: str) -> None:
        self.type_id, self.name = type_id, name


BALL, OTHER = 4937, 4938
sc = FakeMem()
sc.set_goods({363: (BALL, 1, 45), 364: (BALL, 2, 90), 400: (OTHER, 1, 60)})


def settle(val: int) -> None:
    """擺點數並當作「已經穩定讀到很久」—— 預檢要同一個值撐滿 POINTS_SETTLE 秒才擋。"""
    sc.set_points(val)
    mall._pt_seen[0] = (val, time.monotonic() - mall.POINTS_SETTLE - 1.0)

print("① 點數讀得到")
sc.set_points(152)
check("讀到 152", mall.points(sc) == 152, f"實得 {mall.points(sc)!r}")

print("② 商品表沒載入 → 不知道（不是 0 點）")
sc.set_goods({})
check("回 None", mall.points(sc) is None, f"實得 {mall.points(sc)!r}")
sc.set_goods({363: (BALL, 1, 45), 364: (BALL, 2, 90), 400: (OTHER, 1, 60)})

print("③ 一次換兩顆＝90 點（使用者 2026-08-23 指正）")
one = [Ball(BALL, "三階技能經驗球")]
two = [Ball(BALL, "三階技能經驗球"), Ball(BALL, "三階技能經驗球")]
check("缺一顆要 45", mall.quote(sc, one)[:2] == (45, 1), f"實得 {mall.quote(sc, one)}")
check("缺兩顆要 90", mall.quote(sc, two)[:2] == (90, 2), f"實得 {mall.quote(sc, two)}")
mix = [Ball(BALL, "三階"), Ball(OTHER, "四階")]
check("兩顆不同種類＝各自查價 105", mall.quote(sc, mix)[:2] == (105, 2),
      f"實得 {mall.quote(sc, mix)}")

print("④ 商城倉庫已經有現成的那顆不算錢")
sc.set_storage([(1000, BALL, 1)])
check("兩顆只要買一顆＝45", mall.quote(sc, two)[:2] == (45, 1),
      f"實得 {mall.quote(sc, two)}")
sc.set_storage([])

print("⑤ 點數不夠 → 擋下、訊息帶得起通知")
settle(36)
why = mall.blocked(sc, two)
check("擋下來了", bool(why), f"實得 {why!r}")
check("認得出是點數不足", mall.short_of_points(why or ""), f"實得 {why!r}")
check("講得出差多少", "差 54 點" in (why or ""), f"實得 {why!r}")

print("⑥ 點數剛好夠 → 不擋")
sc.set_points(90)
check("90 點買兩顆不擋", mall.blocked(sc, two) is None,
      f"實得 {mall.blocked(sc, two)!r}")

print("⑦ 點數讀不到 → 不擋（讀不到 ≠ 沒錢）")
sc.mem.pop(MGR + mall.POINTS_OFF)               # 那四個 byte 讀不到
check("points 回 None", mall.points(sc) is None)
check("不擋", mall.blocked(sc, two) is None, f"實得 {mall.blocked(sc, two)!r}")
sc.set_points(36)

print("⑧ buy() 送出前當場重讀點數 → 不夠就一發都不送")
sent: list = []


class FakeMover:
    active = True


class FakeGate:
    """如果 buy() 真的送出去，這裡就會被叫到（不該發生）。"""

    def __getattr__(self, name):                       # noqa: D105
        def _boom(*a, **k):
            sent.append(name)
            raise AssertionError(f"點數不夠還送了封包（{name}）")
        return _boom


g = mall.cheapest(sc, BALL)
check("最省的一份是 45 點那筆", g is not None and g.price == 45,
      f"實得 {g!r}")
real_gate, real_settle = mall.actiongate, mall.POINTS_SETTLE
mall.actiongate = FakeGate()
mall.POINTS_SETTLE = 0.3                       # 盯的那段縮短，測試別等 3 秒
try:
    ok, msg = mall.buy(FakeMover(), sc, g)
finally:
    mall.actiongate, mall.POINTS_SETTLE = real_gate, real_settle
check("沒買成", not ok)
check("一發都沒送", not sent, f"實得送了 {sent}")
check("說得出是點數不足", mall.short_of_points(msg), f"實得 {msg}")

print("⑨ 一拍讀到 0 不算沒錢：同一個「不夠」要撐滿 POINTS_SETTLE 秒才擋（2026-09-05）")
mall._pt_seen.clear()
sc.set_points(0)
why = mall.blocked(sc, two)
check("第一次讀到 0 → 先擋住不動手", bool(why), f"實得 {why!r}")
check("　但不帶 SHORT_TAG（不通知）", not mall.short_of_points(why or ""),
      f"實得 {why!r}")
sc.set_points(152)
check("下一拍讀到 152 → 不擋", mall.blocked(sc, two) is None,
      f"實得 {mall.blocked(sc, two)!r}")
sc.set_points(0)
why = mall.blocked(sc, two)
check("又變回 0 → 重新起算，還是不通知",
      bool(why) and not mall.short_of_points(why or ""), f"實得 {why!r}")
mall._pt_seen[0] = (0, time.monotonic() - mall.POINTS_SETTLE - 1.0)
why = mall.blocked(sc, two)
check("0 撐滿 POINTS_SETTLE 秒 → 才是真的不夠、帶 SHORT_TAG",
      mall.short_of_points(why or ""), f"實得 {why!r}")
check("　講得出差多少", "差 90 點" in (why or ""), f"實得 {why!r}")
sc.mem.pop(MGR + mall.POINTS_OFF)
check("讀不到 → 歷史清掉、不擋", mall.blocked(sc, two) is None
      and 0 not in mall._pt_seen, f"實得 {mall.blocked(sc, two)!r}")
sc.set_points(36)

print("⑩ buy() 送出前不夠就盯一段：途中變夠就不擋、一直不夠才擋")


class FlipMem(FakeMem):
    """點數前兩次讀是 0、之後 152（＝伺服器回填晚了一拍那種）。"""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def _read_bytes(self, addr: int, size: int):
        if addr == MGR + mall.POINTS_OFF:
            self.reads += 1
            return struct.pack("<I", 0 if self.reads <= 2 else 152)
        return super()._read_bytes(addr, size)


fm = FlipMem()
fm.set_goods({363: (BALL, 1, 45)})
mall.POINTS_SETTLE = 1.0
try:
    t0 = time.monotonic()
    check("前兩拍 0、第三拍 152 → 不算不夠",
          mall.confirmed_short(fm, 45) is None)
    check("　變夠就馬上放行，沒等滿整段", time.monotonic() - t0 < 0.9,
          f"等了 {time.monotonic() - t0:.2f} 秒")
    t0 = time.monotonic()
    check("一直 36 → 盯滿才回 36", mall.confirmed_short(sc, 45) == 36)
    check("　有盯滿 POINTS_SETTLE 秒", time.monotonic() - t0 >= 0.9,
          f"只等了 {time.monotonic() - t0:.2f} 秒")
    sc.mem.pop(MGR + mall.POINTS_OFF)
    check("讀不到 → None（不是沒錢）", mall.confirmed_short(sc, 45) is None)
finally:
    mall.POINTS_SETTLE = real_settle

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
