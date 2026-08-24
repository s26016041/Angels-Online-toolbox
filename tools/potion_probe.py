"""藥水見底判斷的實機對照（純唯讀，不送封包、不改設定）。

    py tools\potion_probe.py

2026-08-24 使用者回報：北極狐補給回來訊息寫「HP藥水+359、MP藥水+143」
（那是**買完重讀背包對帳出來的實收差額**，藥水真的進背包了），
掛機卻同一拍判定「連續 2 趟補給回來藥水還是見底」→ 停機。

兩條路數同一件事，這支把它們並排印出來：
  A. `bag.scan()` 整袋列舉（補給流程 `supply.bag_counts` 走這條）
  B. `inventory.count_by_types()`（掛機見底判斷 `robot._potion_out` 走這條）
再把 `robot.potions_out()` 的最終結論一起印出來。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core import charname, window as win
from app.core.memory import MemoryScanner
from app.game import bag, inventory, itemname, locate, robot

OUT = os.path.join("reports", "potion_probe.txt")


def clients():
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title)))
    return out


def probe(sc, pid, w):
    info = robot.potion_slots(None, sc, pid)
    if not info:
        w("  精靈藥水設定讀不到（變數樹？）")
        return
    w(f"  POTION_LOW = {robot.POTION_LOW}（加總 ≤ 這個數就叫見底）")
    got = bag.head(sc)
    w(f"  遊戲容器 bag.head() = {got}")
    items, complete = bag.scan(sc)
    w(f"  bag.scan() 整袋 {len(items)} 件，走完了嗎={complete}")
    counts_a = {}
    for it in items:
        counts_a[it.type_id] = counts_a.get(it.type_id, 0) + it.count

    for label, slots in (("HP", robot.HP_ITEM_SLOTS),
                         ("MP", robot.MP_ITEM_SLOTS)):
        w(f"\n  ── {label} 那幾格 ──")
        ids = []
        for base in slots:
            kind, tid = info.get(base, (None, None))
            nm = itemname.of(tid) if tid else ""
            tag = ("技能" if kind == robot.SKILLITEM_TYPE_SKILL else
                   "物品" if kind == robot.SKILLITEM_TYPE_ITEM else f"型別{kind}")
            note = ""
            if kind == robot.SKILLITEM_TYPE_ITEM and tid:
                note = "（算藥水）" if robot._is_potion(tid) else "（名字沒有「藥水」→ 無視）"
                if robot._is_potion(tid):
                    ids.append(tid)
            w(f"    DATAID {base}: {tag} 值={tid} 名稱={nm!r}{note}")
        if not ids:
            w("    → 這一組沒有任何算數的藥水 → 不判斷（不觸發）")
            continue
        a = {t: counts_a.get(t, 0) for t in ids}
        b, bcomplete = inventory.count_by_types(sc, got[0] if got else 0, ids)
        w(f"    A. bag.scan 整袋加總      = {a}  合計 {sum(a.values())}")
        w(f"    B. count_by_types 加總    = {dict(b)}  合計 {sum(b.values())}"
          f"（整條走完={bcomplete}）")
        if sum(a.values()) != sum(b.values()):
            w("    ⚠⚠ 兩條路數出來**不一樣** —— 見底判斷會跟補給對帳吵架")
        w(f"    → _potion_out 判定：{robot._potion_out(info, sc, got[0] if got else 0, slots)}")
    w(f"\n  bag.synced() = {bag.synced(sc)}")
    w(f"  robot.potions_out() 最終結論 = "
      f"{robot.potions_out(None, sc, got[0] if got else 0, pid)}")


def main():
    cs = clients()
    if not cs:
        print("找不到遊戲視窗")
        return
    os.makedirs("reports", exist_ok=True)
    lines = []
    def w(s=""):
        lines.append(s)
    verdict = []
    for pid, acc in cs:
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)
        except Exception as e:                       # noqa: BLE001
            w(f"\n### {acc} (pid {pid}) 接不上：{e!r}")
            continue
        w(f"\n################ {acc} (pid {pid}) ################")
        try:
            probe(sc, pid, w)
            out = robot.potions_out(None, sc, (bag.head(sc) or (0,))[0], pid)
            verdict.append(f"{acc}: 見底={out}")
        except Exception as e:                       # noqa: BLE001
            w(f"  探測失敗：{e!r}")
            verdict.append(f"{acc}: 探測失敗 {e!r}")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print(f"寫到 {OUT}（{len(lines)} 行）")
    for v in verdict:
        print("  " + v)


if __name__ == "__main__":
    main()
