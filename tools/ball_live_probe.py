"""自動換球「讀值」實機探測（純唯讀，不送任何封包、不動任何設定）。

    py tools\ball_live_probe.py [取樣秒數，預設 60]

為什麼要這支
------------
2026-08-24 使用者回報：生產職業「技能球滿了但沒換」，而生產頁自動換球那一列
**數字卡住**。要分辨的是兩件完全不同的事：

  (A) 我們讀到的值本來就沒動 —— 客戶端記憶體的累積值不是即時的
      （那 `balls.Ball.full` 永遠不會成立，功能等於死的）。
  (B) 值有在動，只是分頁沒去讀／沒去更新標籤（純畫面問題）。

所以這支**每 5 秒讀一次、連讀一段時間**，把飾品欄兩格與背包備球的
`+0xA0 累積值` 全部記下來，最後判定「有沒有動」。

順便把兩個分頁的判斷結果（`balls.worn` / `balls.spares` / `pick_spares`）
一起印出來 —— 掛機頁與生產頁走的是**同一份** `app/game/balls.py`，
所以這支的結論兩邊通用。
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

from app.core import charname, window as win
from app.core.memory import MemoryScanner
from app.game import bag, balls, inventory, itemname, locate

OUT = os.path.join("reports", "ball_live_probe.txt")
GAP = 5.0


def clients():
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title), w.hwnd))
    return out


def snap(sc):
    """一拍：飾品欄兩格 + 背包所有球的 (格號, 種類, 序號, 累積值, 上限)。"""
    rows = []
    got = bag.head(sc)
    if got is None:
        return None, "容器讀不到"
    begin, count = got
    items, complete = bag.scan(sc)
    worn_items, worn_ok = bag.scan(sc, bag.WORN_FIRST, bag.WORN_LAST)
    for it in list(worn_items) + list(items):
        if not it.is_ball:
            continue
        raw = sc._read_bytes(begin + it.slot * 4, 4)
        p = struct.unpack("<I", bytes(raw))[0] if raw and len(raw) == 4 else 0
        e = None
        if 0x10000 < p < 0x7FFF0000:
            r2 = sc._read_bytes(p + bag.ITEM_ENERGY, 4)
            if r2 and len(r2) == 4:
                e = struct.unpack("<I", bytes(r2))[0]
        rows.append({
            "slot": it.slot, "worn": it.slot in inventory.SLOT_ACCESSORY,
            "type": it.type_id, "kind": it.kind, "serial": it.serial,
            "name": it.name or (itemname.of(it.type_id) or "?"),
            "ptr": p, "energy": e, "cap": it.ball_cap,
        })
    note = ("" if (complete and worn_ok) else " ⚠ 這一拍背包/身上沒讀完整")
    return rows, note


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    cs = clients()
    if not cs:
        print("找不到遊戲視窗")
        return
    os.makedirs("reports", exist_ok=True)
    lines = []
    def w(s=""):
        lines.append(s)

    scs = {}
    for pid, acc, hwnd in cs:
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)
            scs[pid] = (sc, acc)
        except Exception as e:                      # noqa: BLE001
            w(f"### {acc} (pid {pid}) 接不上：{e!r}")

    hist = {pid: [] for pid in scs}
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < secs:
        n += 1
        for pid, (sc, acc) in scs.items():
            try:
                rows, note = snap(sc)
            except Exception as e:                  # noqa: BLE001
                rows, note = None, f"例外 {e!r}"
            hist[pid].append((round(time.monotonic() - t0, 1), rows, note))
        time.sleep(GAP)

    verdicts = []
    for pid, (sc, acc) in scs.items():
        w(f"\n################ {acc} (pid {pid}) ################")
        samples = hist[pid]
        # 以「序號」當身分（換裝時指標不變、內容互換 —— 見 memory
        # bag-container-path 的 ⛔⛔ 那條）。
        keys = {}
        for t, rows, note in samples:
            if not rows:
                continue
            for r in rows:
                keys.setdefault(r["serial"], r)
        if not keys:
            w("  沒有任何經驗球（飾品欄與背包都沒有）")
            verdicts.append(f"{acc}: 沒有球")
            continue
        for serial, first in keys.items():
            seq = []
            for t, rows, note in samples:
                if not rows:
                    continue
                for r in rows:
                    if r["serial"] == serial:
                        seq.append((t, r["energy"], r["slot"]))
            vals = [v for _, v, _ in seq if v is not None]
            moved = len(set(vals)) > 1
            where = "飾品欄" if first["worn"] else "背包"
            cap = first["cap"]
            v0 = vals[0] if vals else None
            v1 = vals[-1] if vals else None
            pct = f"{v1 / cap * 100:.1f}%" if (cap and v1 is not None) else "?"
            w(f"\n  [{where} 第{first['slot']}格] {first['name']} "
              f"(種類 {first['type']} 分類 {first['kind']} 序號 {serial})")
            w(f"    上限(+0x10C) = {cap:,}" if cap else
              "    上限(+0x10C) = 0  ⚠ 讀不到 → 判不了滿沒滿")
            w(f"    累積(+0xA0)  = {v0:,} → {v1:,}   ({pct}) "
              f"{'★ 有在動' if moved else '⚠ 整段沒動'}"
              if v0 is not None else "    累積(+0xA0)  = 讀不到")
            if cap and v1 is not None:
                w(f"    滿了嗎？ {'是' if v1 >= cap else f'否，還差 {cap - v1:,}'}")
            w("    逐拍：" + ", ".join(
                f"{t}s={'?' if v is None else v}" for t, v, _ in seq))
        # 分頁實際會怎麼判
        got = balls.worn(sc)
        if got is None:
            w("\n  balls.worn() → None（飾品欄讀不到）")
            verdicts.append(f"{acc}: 飾品欄讀不到")
            continue
        cur = got[0]
        if not cur:
            w("\n  balls.worn() → 飾品欄沒有裝經驗球 → 兩個分頁都不會動作")
            verdicts.append(f"{acc}: 飾品欄沒裝球")
            continue
        shown = "、".join(f"{b.value:,}/{b.cap:,}" for b in cur)
        allfull = all(b.full for b in cur)
        w(f"\n  balls.worn() → {shown}")
        w(f"  分頁標籤會顯示：經驗球：{shown}"
          + ("" if allfull else "（要兩顆都滿才換）"))
        w(f"  都滿了嗎？ {allfull}"
          + ("" if allfull else "  ← 只要有一顆沒滿，兩個分頁都不換"))
        pool = balls.spares(sc)
        if pool is None:
            w("  balls.spares() → None（背包讀不到）")
        else:
            w(f"  背包備球 {len(pool)} 顆：" + "、".join(
                f"{b.name}({b.value:,}/{b.cap:,}"
                + (" 滿了" if b.full else "") + ")" for b in pool) or "（沒有）")
            pairs, missing = balls.pick_spares(pool, cur)
            w(f"  配對結果：換得成 {len(pairs)} 顆、缺 {len(missing)} 顆")
        verdicts.append(f"{acc}: {shown} 都滿={allfull}")

    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print(f"取樣 {n} 拍 / {secs:.0f} 秒，寫到 {OUT}")
    for v in verdicts:
        print("  " + v)


if __name__ == "__main__":
    main()
