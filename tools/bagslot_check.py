"""「放得進東西的背包格」回歸測試（純假記憶體，不碰遊戲）。

    py tools\\bagslot_check.py

驗的是 2026-09-04 那個 bug 的根因：`mall.free_slot()` 拿「賣東西視窗看的範圍」
（20~169）當可用格，角色那 40 格一滿就挑到鎖住的第 60 格 → 伺服器回
「領取商品失敗」。正解＝照遊戲「領商城倉庫」本體（0x5D336A）的數法：
20~59 ＋（第 742 格有擴充通行證才有）60~69 ＋ 穿著的背包（第 7 格）給的 70 起。

假貨只做到 `_read_bytes`（位址 → 位元組），`bag.usable_slots` / `pass_ok` /
`bag_capacity` / `mall._free_slots` 全部跑真的。
"""
from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.game import bag, mall  # noqa: E402

FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(("  ✔ " if ok else "  ✘ ") + name + ("" if ok else f"　← {detail}"))
    if not ok:
        FAILS.append(name)


BEGIN = 0x1000000          # 容器指標陣列
COUNT = 743
ITEM_BASE = 0x2000000      # 物品物件從這裡往上配
TMPL_BASE = 0x3000000


class FakeMem:
    def __init__(self) -> None:
        self.mem: dict[int, bytes] = {}
        self.next_item = ITEM_BASE
        self.next_tmpl = TMPL_BASE
        # 容器陣列先全部擺 0（空格），讀得到但沒東西
        self.write(BEGIN, b"\0" * (COUNT * 4))

    def write(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.mem[addr + i] = bytes([b])

    def _read_bytes(self, addr: int, size: int):
        out = bytearray()
        for i in range(size):
            b = self.mem.get(addr + i)
            if b is None:
                return None
            out += b
        return bytes(out)

    def put(self, slot: int, *, time_limit: int = 0, tmpl_param1: int | None = None,
            adv: tuple[int, ...] = (), extra: dict[int, int] | None = None,
            no_tmpl: bool = False) -> int:
        """在第 slot 格擺一件物品；回物件位址。"""
        ptr = self.next_item
        self.next_item += 0x200
        blob = bytearray(bag.ITEM_CAP_SPAN)
        struct.pack_into("<I", blob, bag.ITEM_SERIAL, 1000 + slot)
        struct.pack_into("<I", blob, bag.ITEM_TYPE, 5000 + slot)
        struct.pack_into("<H", blob, bag.ITEM_SLOT, slot)
        struct.pack_into("<H", blob, bag.ITEM_COUNT, 1)
        struct.pack_into("<I", blob, bag.ITEM_TIMELIMIT, time_limit)
        for i, v in enumerate(adv):
            struct.pack_into("<I", blob, bag.ITEM_ADV + i * 4, v)
        for i, (code, val) in enumerate((extra or {}).items()):
            blob[bag.ITEM_EXTRA_ID + i] = code
            struct.pack_into("<i", blob, bag.ITEM_EXTRA_VAL + i * 4, val)
        tmpl = self.next_tmpl
        self.next_tmpl += 0x200
        struct.pack_into("<I", blob, bag.ITEM_TMPL, tmpl)
        self.write(ptr, bytes(blob))
        if not no_tmpl:
            t = bytearray(bag.TMPL_SPAN)
            if tmpl_param1 is not None:
                struct.pack_into("<i", t, bag.TMPL_PARAM1, tmpl_param1)
            self.write(tmpl, bytes(t))
        self.write(BEGIN + slot * 4, struct.pack("<I", ptr))
        return ptr

    def clear(self, slot: int) -> None:
        self.write(BEGIN + slot * 4, b"\0\0\0\0")


HEAD = (BEGIN, COUNT)
NOW = 1_800_000_000


def usable(sc):
    return bag.usable_slots(sc, HEAD, now=NOW)


print("① 沒穿背包、沒通行證 → 只有 20~59")
sc = FakeMem()
check("20~59 共 40 格", usable(sc) == list(range(20, 60)), f"實得 {usable(sc)}")
check("容量 0", bag.bag_capacity(sc, BEGIN, COUNT) == 0)
check("60~69 沒開", not bag.pass_ok(sc, BEGIN, COUNT, NOW))

print("② 穿 30 格背包（實機五台：開學-旅行背包 範本+0x108=30）→ 多 70~99")
sc.put(bag.BAG_WORN_SLOT, tmpl_param1=30)
check("容量 30", bag.bag_capacity(sc, BEGIN, COUNT) == 30,
      f"實得 {bag.bag_capacity(sc, BEGIN, COUNT)}")
check("20~59 ＋ 70~99", usable(sc) == list(range(20, 60)) + list(range(70, 100)),
      f"實得 {usable(sc)}")
check("⚠ 60~69 與 100 以上都不在清單", not any(60 <= s <= 69 or s >= 100
                                          for s in usable(sc)))

print("③ 進階屬性 0x30 與 +0xB0 那組都要加進容量（0x532E67 / 0x532E9E）")
sc = FakeMem()
sc.put(bag.BAG_WORN_SLOT, tmpl_param1=30,
       adv=((bag.BAG_CAP_ATTR << 26) | 5, (0x11 << 26) | 99),
       extra={0x12: 7, bag.BAG_CAP_ATTR: 4})
check("30 + 5 + 4 = 39", bag.bag_capacity(sc, BEGIN, COUNT) == 39,
      f"實得 {bag.bag_capacity(sc, BEGIN, COUNT)}")
check("別的屬性編號不算", 99 not in (bag.bag_capacity(sc, BEGIN, COUNT),))

print("④ 擴充通行證（第 742 格）")
sc = FakeMem()
sc.put(bag.PASS_SLOT, time_limit=0)
check("時限 0（永久）→ 60~69 開", bag.pass_ok(sc, BEGIN, COUNT, NOW))
check("清單含 60~69", all(s in usable(sc) for s in range(60, 70)))
sc = FakeMem()
sc.put(bag.PASS_SLOT, time_limit=NOW + 3600)
check("還沒到期 → 開", bag.pass_ok(sc, BEGIN, COUNT, NOW))
sc = FakeMem()
sc.put(bag.PASS_SLOT, time_limit=NOW - 1)
check("過期 → 不開", not bag.pass_ok(sc, BEGIN, COUNT, NOW))
check("清單不含 60~69", not any(60 <= s <= 69 for s in usable(sc)))
sc = FakeMem()
sc.put(bag.PASS_SLOT, time_limit=1_000_000_000)            # 2001 年，早過期
check("不傳 now 就用現在時間：2001 年到期 → 不開",
      bag.pass_ok(sc, BEGIN, COUNT) is False)
sc = FakeMem()
sc.put(bag.PASS_SLOT, time_limit=int(time.time()) + 86400)
check("不傳 now：明天到期 → 開", bag.pass_ok(sc, BEGIN, COUNT) is True)

print("⑤ 讀不到一律往少算退（少幾格＝下一輪重試；多算＝送錯格）")
sc = FakeMem()
sc.put(bag.BAG_WORN_SLOT, tmpl_param1=30, no_tmpl=True)
check("背包範本讀不到 → 容量 0", bag.bag_capacity(sc, BEGIN, COUNT) == 0)
sc = FakeMem()
sc.write(BEGIN + bag.BAG_WORN_SLOT * 4, struct.pack("<I", 0x7FFFFFF0))
check("指標不合理 → 容量 0", bag.bag_capacity(sc, BEGIN, COUNT) == 0)
sc = FakeMem()
sc.write(BEGIN + bag.PASS_SLOT * 4, struct.pack("<I", 0x5000000))   # 指到沒東西
check("通行證物件讀不到 → 不開", not bag.pass_ok(sc, BEGIN, COUNT, NOW))
check("容器比 742 短 → 不開、不炸", not bag.pass_ok(sc, BEGIN, 100, NOW))
check("容器讀不到 → None", bag.usable_slots(sc, None) is None
      or bag.usable_slots(FakeMem(), None) is None)

print("⑥ 容量上限：超過 0xA9 的格遊戲自己也不認（0x5AF037）")
sc = FakeMem()
sc.put(bag.BAG_WORN_SLOT, tmpl_param1=500)
check("容量夾在 100", bag.bag_capacity(sc, BEGIN, COUNT) == 100,
      f"實得 {bag.bag_capacity(sc, BEGIN, COUNT)}")
check("最後一格是 0xA9", usable(sc)[-1] == bag.LAST_SLOT)

print("⑦ mall.free_slot：角色 40 格全滿時要挑 70（不是鎖住的 60）")
_head, _scan = bag.head, bag.scan


class Used:
    def __init__(self, slot):
        self.slot = slot


def fake_head(_sc):
    return HEAD


def fake_scan(_sc, first=bag.FIRST_SLOT, last=bag.LAST_SLOT):
    return [Used(s) for s in range(20, 60)] + [Used(70), Used(71)], True


bag.head, bag.scan = fake_head, fake_scan
try:
    sc = FakeMem()
    sc.put(bag.BAG_WORN_SLOT, tmpl_param1=30)
    check("挑到 72", mall.free_slot(sc) == 72, f"實得 {mall.free_slot(sc)}")
    check("free_count 只算開著的格（28）", mall.free_count(sc) == 28,
          f"實得 {mall.free_count(sc)}")
    sc = FakeMem()                                  # 沒穿背包：40 格滿＝沒地方放
    check("沒背包＋40 格滿 → None（不猜 60）", mall.free_slot(sc) is None,
          f"實得 {mall.free_slot(sc)}")
    check("free_count 0（不是 None、不是 120）", mall.free_count(sc) == 0,
          f"實得 {mall.free_count(sc)}")
    sc.put(bag.PASS_SLOT, time_limit=0)             # 有通行證才准挑 60
    check("有通行證 → 挑 60", mall.free_slot(sc) == 60, f"實得 {mall.free_slot(sc)}")
    bag.scan = lambda _sc, first=0, last=0: ([], False)
    check("整袋沒讀完 → None（不下結論）", mall.free_slot(sc) is None)
finally:
    bag.head, bag.scan = _head, _scan

print()
if FAILS:
    print(f"✘ {len(FAILS)}/{N} 項失敗：{FAILS}")
    sys.exit(1)
print(f"✔ {N} 項全過")
