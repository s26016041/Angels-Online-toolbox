r"""找「商城點數」欄位：給畫面上看到的數字，回報它住在哪（純讀）。

    py tools\find_point.py 1234            所有在線分身都掃
    py tools\find_point.py 1234 黑狐        只掃這隻

★ 為什麼要人工給值：商城點數**沒有**現成的讀法 ——
  · 客戶端買東西前不檢查點數（`mallbuy` 0x5D49BE 直接送 0x12B，
    「Mile點數不足」是伺服器回的訊息）；
  · Lua 的 `MallBuyCheck` 只在 `game.isdef('__MILE_MALL')` 成立時才查，
    這一版沒定義（`checkmilemall` 也沒註冊進 game 表）；
  · 精靈變數 `AM_INT_CURRENTPOINT`(1602) 只有**開了自動商城**才會被填，
    五台實測全是 0 —— 讀到 0 ≠ 沒點數，不能當判據。
  所以只剩「拿畫面上的真值回頭定位」這條硬路。

命中之後會順便算：那個位址落在**世界管理器**（`gather.WORLD_PTR`）
或**玩家實體**的第幾個 byte —— 有穩定偏移才敢寫進產品。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core import charname                              # noqa: E402
from app.core.memory import VALUE_TYPES, MemoryScanner     # noqa: E402
from app.game import bag, gather, locate                   # noqa: E402
from tools.buff_probe import clients                       # noqa: E402

OUT = Path("reports") / "point_hunt.txt"
NEAR = 0x40000          # 離基底這麼近才算「掛在這個物件上」


def anchors(sc):
    """可以拿來算偏移的基底（讀不到就跳過）。"""
    out = {}
    try:
        raw = sc._read_bytes(gather.WORLD_PTR, 4)
        if raw:
            out["世界管理器"] = struct.unpack("<I", bytes(raw))[0]
    except Exception:                                      # noqa: BLE001
        pass
    try:
        ent = bag.player_entity(sc)
        if ent:
            out["玩家實體"] = ent
    except Exception:                                      # noqa: BLE001
        pass
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    want = int(sys.argv[1].replace(",", ""))
    only = sys.argv[2] if len(sys.argv) > 2 else None
    lines = [f"# 找值 {want:,}"]
    for pid, acc in clients():
        sc = MemoryScanner()
        sc.open(pid)
        locate.warm(sc)
        try:
            nm = charname.read_character_name(sc, acc) or acc
        except Exception:                                  # noqa: BLE001
            nm = acc
        if only and only != nm:
            continue
        n = sc.first_scan(VALUE_TYPES["int32"], "exact", want)
        hits = [a for a, _v in sc.results(limit=4000)]
        base = anchors(sc)
        print(f"\n{nm}（pid {pid}）：{n} 個位址是 {want:,}")
        lines.append(f"\n## {nm} pid={pid} 命中 {n}")
        for label, b in base.items():
            near = [a for a in hits if 0 <= a - b < NEAR]
            print(f"   掛在{label} {b:#x} 的：{len(near)} 個"
                  + ("　→ " + "、".join(f"+{a - b:#x}" for a in near[:12])
                     if near else ""))
            lines.append(f"{label} {b:#x}：" + "、".join(
                f"+{a - b:#x}" for a in near) if near else f"{label}：無")
        lines += [f"  {a:#x}" for a in hits[:400]]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n全量寫到 {OUT}")


if __name__ == "__main__":
    main()
