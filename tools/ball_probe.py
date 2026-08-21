"""換球功能的探路（純唯讀，不送任何封包）。

    py tools\ball_probe.py

會倒出：
  1. 身上 0~11 格（含飾品欄）每格的種類／球值／範本位址
  2. 範本裡有沒有欄位剛好等於 items.py 記的「球上限」（找得到就不用寫死表）
  3. 商城倉庫清單（管理器 +0xCEC4，每筆 0x37 bytes，反組譯 0x5D38CF 得到）
  4. 背包裡所有經驗球（種類、球值）
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core import charname, window as win
from app.core.memory import MemoryScanner
from app.game import bag, gather, items, itemname, locate

# ⚠ 位址一律借已登記 AOB 的那一份（CLAUDE.md：同一個位址不准寫第二次）
# 商城倉庫清單（反組譯 0x5D38CF「領取商城倉庫」得到的版面）。
# ⚠ 探路中、還沒實機驗證 —— 只給這支診斷用，產品端還沒有人讀它。
MALL_BAG = 0xCEC4             # 管理器 + 這裡 = 商城倉庫第 0 筆
MALL_STRIDE = 0x37
MALL_MAX = 80
OUT = os.path.join("reports", "ball_probe.txt")


def u32(sc, a):
    r = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(r))[0] if r and len(r) == 4 else None


def clients():
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title)))
    return out


def probe(sc, write):
    got = bag.head(sc)
    if got is None:
        write("  容器讀不到（沒進場？）")
        return
    begin, count = got
    write(f"  容器 {begin:#x} 格數 {count}")
    raw = sc._read_bytes(begin, count * 4)
    ptrs = list(struct.unpack(f"<{count}I", bytes(raw))) if raw else []

    def row(slot, ptr):
        blob = sc._read_bytes(ptr, bag.ITEM_SPAN)
        if not blob or len(blob) < bag.ITEM_SPAN:
            return None
        b = bytes(blob)
        tid = struct.unpack_from("<I", b, bag.ITEM_TYPE)[0]
        own = struct.unpack_from("<H", b, bag.ITEM_SLOT)[0]
        cnt = struct.unpack_from("<H", b, bag.ITEM_COUNT)[0]
        tmpl = struct.unpack_from("<I", b, bag.ITEM_TMPL)[0]
        e = sc._read_bytes(ptr + bag.ITEM_ENERGY, 4)
        val = struct.unpack("<I", bytes(e))[0] if e else None
        return tid, own, cnt, tmpl, val

    write("\n  == 身上 0~11 格 ==")
    worn = []
    for slot in range(min(12, len(ptrs))):
        p = ptrs[slot]
        if not p:
            continue
        r = row(slot, p)
        if not r:
            write(f"   {slot:>3} 讀不到")
            continue
        tid, own, cnt, tmpl, val = r
        bt = items.ball_type(tid)
        write(f"   陣列{slot:>3} 自記{own:>3} 種類{tid:>7} 值{val!s:>12} "
              f"範本{tmpl:#x}  {itemname.of(tid) or '?'}"
              f"{'  [球 上限=' + format(bt.cap, ',') + ']' if bt else ''}")
        worn.append((slot, tid, tmpl, val, bt))

    write("\n  == 範本裡找得到『球上限』嗎（拿已知上限去比對範本每個 dword）==")
    for slot, tid, tmpl, val, bt in worn:
        if not bt or bt.cap <= 0 or not tmpl:
            continue
        t = sc._read_bytes(tmpl, 0x200)
        if not t:
            write(f"   種類{tid} 範本讀不到")
            continue
        tb = bytes(t)
        hit = [f"+0x{o:03x}" for o in range(0, 0x200 - 4, 4)
               if struct.unpack_from("<I", tb, o)[0] == bt.cap]
        write(f"   種類{tid}（上限 {bt.cap:,}）→ 命中 {hit or '無'}")

    write("\n  == 背包裡的經驗球（球值 != 0 或認得的種類）==")
    for slot in range(bag.FIRST_SLOT, min(bag.LAST_SLOT + 1, len(ptrs))):
        p = ptrs[slot]
        if not p:
            continue
        r = row(slot, p)
        if not r:
            continue
        tid, own, cnt, tmpl, val = r
        bt = items.ball_type(tid)
        if not bt and not val:
            continue
        write(f"   陣列{slot:>3} 自記{own:>3} 種類{tid:>7} 值{val!s:>12} "
              f"範本{tmpl:#x}  {itemname.of(tid) or '?'}"
              f"{'  [上限=' + format(bt.cap, ',') + ']' if bt else ''}")

    mgr = u32(sc, gather.WORLD_PTR)
    write(f"\n  == 商城倉庫清單（管理器 {mgr:#x} + {MALL_BAG:#x}）=="
          if mgr else "\n  == 商城倉庫：管理器讀不到 ==")
    if mgr:
        blob = sc._read_bytes(mgr + MALL_BAG, MALL_STRIDE * MALL_MAX)
        if not blob:
            write("   讀不到")
        else:
            bb = bytes(blob)
            shown = 0
            for i in range(MALL_MAX):
                o = i * MALL_STRIDE
                a, b_, c = struct.unpack_from("<IIH", bb, o)
                if not a and not b_ and not c:
                    continue
                tail = bb[o + 10:o + MALL_STRIDE]
                txt = tail.split(b"\0")[0]
                write(f"   [{i:>2}] +0={a:<12} +4={b_:<8}({itemname.of(b_) or '?'})"
                      f" +8={c:<5} 尾={txt[:24]!r}")
                shown += 1
            if not shown:
                write("   全空（商城倉庫沒東西，或還沒跟伺服器要過資料）")


def main():
    cs = clients()
    if not cs:
        print("找不到遊戲視窗")
        return
    os.makedirs("reports", exist_ok=True)
    lines = []
    def write(s=""):
        lines.append(s)
    for pid, acc in cs:
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)
        except Exception as e:
            write(f"\n### {acc} (pid {pid}) 接不上：{e}")
            continue
        write(f"\n################ {acc} (pid {pid}) ################")
        try:
            probe(sc, write)
        except Exception as e:
            write(f"  探測失敗：{e!r}")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print(f"寫到 {OUT}（{len(lines)} 行）")


if __name__ == "__main__":
    main()
