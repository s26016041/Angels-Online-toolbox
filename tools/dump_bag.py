"""把整個物品容器倒出來（診斷用，純讀取）。

    py tools\\dump_bag.py                  → 每台分身各倒一份到 reports\\
    py tools\\dump_bag.py --find 5346      → 另外標出這個種類在哪幾格

為什麼要有這支
--------------
`bag.items()` 預設只給遊戲賣東西視窗那段格號（0x14~0xA9），要查「東西明明在
身上卻讀不到」的時候，得看**整條容器**（0 ~ 格數-1）才知道是「不在那段範圍」
還是「根本讀不到」。

⚠ 純讀取：不寫記憶體、不注入、不送封包。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.core import charname, window as win           # noqa: E402
from app.core.memory import MemoryScanner              # noqa: E402
from app.game import bag, itemname, locate             # noqa: E402


def clients() -> list[tuple[int, str]]:
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title)))
    return out


def dump_one(sc, find: int, write) -> None:
    ent = bag.player_entity(sc)
    write(f"玩家實體：{ent:#x}" if ent else "玩家實體：讀不到（沒進場？）")
    if ent is None:
        return
    got = bag.head(sc)
    if got is None:
        write("容器表頭：讀不到 —— items() 這時會回空清單（假的『沒有東西』）")
        return
    begin, count = got
    write(f"容器表頭：{begin:#x}　格數 {count}"
          f"（items() 預設只看 {bag.FIRST_SLOT}~{bag.LAST_SLOT}）")

    # 指標陣列一次讀完；讀不到就一格一格讀，才分得出「整段失敗」與「尾端截斷」
    raw = sc._read_bytes(begin, count * 4)
    if raw and len(raw) == count * 4:
        ptrs = list(struct.unpack(f"<{count}I", bytes(raw)))
        write(f"指標陣列：一次讀滿 {count} 格 ✔")
    else:
        write(f"指標陣列：⚠ 一次讀 {count} 格失敗（拿到 "
              f"{len(raw) if raw else 0} bytes）→ 改逐格讀")
        ptrs = []
        for i in range(count):
            r = sc._read_bytes(begin + i * 4, 4)
            ptrs.append(struct.unpack("<I", bytes(r))[0] if r else -1)

    hits: list[int] = []
    write("")
    write(f"{'格號':>5} {'指標':>10} {'種類':>7} {'數量':>5} {'分類':>4} "
          f"{'耐久':>5} {'時限':>10}  名稱")
    for slot, ptr in enumerate(ptrs):
        if ptr == -1:
            write(f"{slot:>5} {'讀取失敗':>10}")
            continue
        if not ptr:
            continue
        blob = sc._read_bytes(ptr, bag.ITEM_SPAN)
        if not blob:
            write(f"{slot:>5} {ptr:>#10x}  ⚠ 物件讀不到")
            continue
        b = bytes(blob)
        type_id = struct.unpack_from("<I", b, bag.ITEM_TYPE)[0]
        cnt = struct.unpack_from("<H", b, bag.ITEM_COUNT)[0]
        selfslot = struct.unpack_from("<H", b, bag.ITEM_SLOT)[0]
        dura = struct.unpack_from("<H", b, bag.ITEM_DURA)[0]
        tlimit = struct.unpack_from("<I", b, bag.ITEM_TIMELIMIT)[0]
        tmpl = struct.unpack_from("<I", b, bag.ITEM_TMPL)[0]
        kind = 0
        if 0x10000 < tmpl < 0x7FFF0000:
            traw = sc._read_bytes(tmpl, bag.TMPL_SPAN)
            if traw:
                kind = struct.unpack_from("<I", bytes(traw), bag.TMPL_KIND)[0]
        name = itemname.of(type_id) or "?"
        mark = "  ★" if type_id == find else ""
        note = "" if selfslot == slot else f"  ⚠自記格號={selfslot}"
        write(f"{slot:>5} {ptr:>#10x} {type_id:>7} {cnt:>5} {kind:>4} "
              f"{dura:>5} {tlimit:>10}  {name}{note}{mark}")
        if type_id == find:
            hits.append(slot)

    write("")
    nm = itemname.of(find) or f"種類 {find}"
    if hits:
        inside = [s for s in hits if bag.FIRST_SLOT <= s <= bag.LAST_SLOT]
        write(f"★ {nm}：找到 {len(hits)} 格 → {hits}")
        write(f"   其中落在 items() 預設範圍內的：{len(inside)} 格 → {inside}")
    else:
        write(f"⚠ 整條容器（0~{count - 1}）都沒有 {nm}")

    # bag.items() 自己怎麼看
    tok = [it for it in bag.items(sc) if it.type_id == find]
    write(f"bag.items() 預設範圍數到：{len(tok)} 件、"
          f"合計數量 {sum(it.count for it in tok)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--find", type=int, default=5346,
                    help="要標出來的種類 ID（預設 5346＝勤奮在線獎勵卷）")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    cs = clients()
    if not cs:
        print("找不到遊戲視窗 —— 先把遊戲開起來。")
        return 1
    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.out, f"bag_dump_{stamp}.txt")

    lines: list[str] = []
    def write(s: str = "") -> None:
        lines.append(s)

    summary: list[str] = []
    for pid, acc in cs:
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)
        except Exception as exc:                       # noqa: BLE001
            write(f"=== {acc}（pid {pid}）=== 開不起來：{exc}")
            summary.append(f"{acc}: 開不起來")
            continue
        write(f"=== {acc}（pid {pid}）===")
        n0 = len(lines)
        try:
            dump_one(sc, args.find, write)
        finally:
            sc.close()
        hit = next((l for l in lines[n0:] if l.startswith(("★", "⚠ 整條"))),
                   "（沒結論）")
        tail = next((l for l in lines[n0:]
                     if l.startswith("bag.items()")), "")
        summary.append(f"{acc}: {hit}　{tail}")
        write("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    for s in summary:
        print(s)
    print(f"\n全量：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
