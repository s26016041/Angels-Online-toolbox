r"""找出這張圖上「踩上去會送 0x0D 的觸發物件」（＝傳點／機關）。純讀。

    py tools\portal_probe.py

出處（2026-09-02 從使用者「進副本」的擷取反組譯出來，見 memory
`dungeon-farm-scoping`）：遊戲自己每一拍對每個場景物件跑 `0x546A13`——

    ax = word[物件+0x1FE]                 ; 伺服器發的物件旗標
    if ax == 0 or (ax & 0x185) == 0: 不是觸發物件
    p = 「站在我這一格上的玩家」
    if [物件+0x208] == [p+0xBC]: 同一個玩家，不重送
    碼 = (p+0x1D4 bit27) ? (ax&0x100 ? 9 : ax&0x8000 ? 8 : 不送)
                         : (ax&4    ? 3 : ax&1     ? 1 : 不送)
    送封包 0x0D([p+0x1D0], [物件+0x1D0], 碼)

所以「哪些東西踩上去會有事發生」是**讀得到的**，不必寫死座標。
全量寫 reports\portal_probe.txt，主控台只印摘要。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                     # noqa: E402
from app.core.memory import MemoryScanner              # noqa: E402
from app.game import (entity, gather, locate, mapobj,   # noqa: E402
                      move, scene)

REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "reports", "portal_probe.txt")

OFF_FLAGS = 0x1FE          # word：伺服器發的物件旗標
OFF_FLAGS2 = 0x200         # word：旁邊那個（同一包一起填的）
OFF_SELECT = 0x1D0         # 封包用的 id
OFF_LAST = 0x208           # 上一個踩上來的玩家 eid（去重欄）
OFF_MODEL = 0xB4
TRIGGER_MASK = 0x185       # 0x546A3B  test eax, 0x185
SPAN = 0x20C
_LO, _HI = 0x10000, 0x7FFF0000


def _u32(sc, a):
    raw = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def main() -> int:
    wins = [w for w in win.enumerate_windows(title_contains="Angels Online")
            if "_MIDAGEONL_" in w.class_name]
    if not wins:
        print("找不到遊戲視窗。")
        return 2
    lines: list[str] = []
    first = True
    for w in wins:
        sc = MemoryScanner()
        sc.open(w.pid)
        try:
            if first:
                locate.warm(sc)
                first = False
            sid = scene.current_id(sc)
            head = f"=== pid {w.pid}　{w.title[:44]}　{scene.scene_name(sid)}（{sid}）"
            mgr = _u32(sc, move.MGR_PTR)
            tbl = _u32(sc, mgr + move.MGR.TBL) if mgr else None
            mx = _u32(sc, mgr + move.MGR.MAX) if mgr else None
            if not tbl or not mx or not 0 < mx <= 0x10000:
                print(head + "　讀不到物件表")
                lines.append(head + "　讀不到物件表")
                continue
            raw = sc._read_bytes(tbl, (mx + 1) * 4)
            slots = struct.unpack_from(f"<{mx + 1}I", bytes(raw), 0)
            hits = []
            for i, obj in enumerate(slots):
                if not obj or not _LO <= obj <= _HI:
                    continue
                blob = sc._read_bytes(obj, SPAN)
                if not blob or len(blob) < SPAN:
                    continue
                b = bytes(blob)
                flags = struct.unpack_from("<H", b, OFF_FLAGS)[0]
                if not flags or not (flags & TRIGGER_MASK):
                    continue
                oid = struct.unpack_from("<I", b, move.MGR.OBJ_ID)[0]
                if (oid & 0xFFFF) != i:
                    continue                       # 殘留格，不算
                vt = struct.unpack_from("<I", b, 0)[0]
                vx, vy = struct.unpack_from("<II", b, 8 + entity.OFF_POS_X)
                x = (vx >> 16) / entity.TILE_UNITS
                y = (vy >> 16) / entity.TILE_UNITS
                model = struct.unpack_from("<I", b, OFF_MODEL)[0]
                hits.append((
                    obj, oid, x, y, flags,
                    struct.unpack_from("<H", b, OFF_FLAGS2)[0],
                    struct.unpack_from("<I", b, OFF_SELECT)[0],
                    struct.unpack_from("<I", b, OFF_LAST)[0],
                    model, vt))
            print(f"{head}　觸發物件 {len(hits)} 個"
                  f"（掃了 {sum(1 for s in slots if s)} 個物件）")
            lines.append(head + f"　觸發物件 {len(hits)} 個")
            hits.sort(key=lambda h: (h[2], h[3]))
            for (obj, oid, x, y, fl, fl2, sel, last, model, vt) in hits:
                code = ("3" if fl & 4 else "1" if fl & 1 else "—")
                code2 = ("9" if fl & 0x100 else "8" if fl & 0x8000 else "—")
                lines.append(
                    f"   ({x:6.1f},{y:6.1f})  旗標={fl:#06x}/{fl2:#06x}  "
                    f"碼={code}（bit27時 {code2}）  外觀={model} "
                    f"{mapobj.name_of(model) or ''}  選定id={sel:#010x}  "
                    f"上次踩={last:#010x}  vt={vt:#010x}  物件={obj:#010x}")
            for row in lines[-min(len(hits), 12):]:
                print(row)
        finally:
            sc.close()
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n全量：", REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
