"""購買記帳離線測試 —— supply.run_buy 的 ledger 對帳（不碰遊戲、不碰 Qt）。

驗的規格（2026-08-20「購買紀錄」）：
    · 記的是**下一輪對帳時背包的實測差額**，不是送出的數量
      （送 50 只進 30 就記 30）
    · 分好幾輪買到的，每輪各記一筆
    · 買了但一顆都沒進來 → 一筆都不記（不猜）
    · MAX_ROUNDS 用完，最後一輪買到的也要記（迴圈外的收尾對帳）

用法：py tools\\purchase_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import supply                     # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


MOVER = types.SimpleNamespace(active=True)
SC = object()


def run(want, haves, buy_ok=True, use_ledger=True):
    """跑一次 run_buy：want=購買清單、haves=每次 bag_counts 的回傳（照順序）。
    回 (ok, msg, ledger 收到的 [(tid, qty), …], buy 被叫了幾次)。"""
    state = {"i": 0, "buys": 0}

    def fake_bag_counts(sc):
        i = min(state["i"], len(haves) - 1)
        state["i"] += 1
        return dict(haves[i])

    def fake_buy(mv, sc, entries):
        state["buys"] += 1
        return (True, "") if buy_ok else (False, "沒送出去")

    old = (supply.read_buy_list, supply.bag_counts, supply._engage_npc,
           supply.buy, supply.time)
    got: list[tuple[int, int]] = []
    try:
        supply.read_buy_list = lambda sc: list(want)
        supply.bag_counts = fake_bag_counts
        supply._engage_npc = lambda *a, **k: True
        supply.buy = fake_buy
        supply.time = types.SimpleNamespace(sleep=lambda s: None)
        ok, msg = supply.run_buy(
            MOVER, SC, 1878, (0, 0),
            ledger=(lambda t, q: got.append((t, q))) if use_ledger else None)
    finally:
        (supply.read_buy_list, supply.bag_counts, supply._engage_npc,
         supply.buy, supply.time) = old
    return ok, msg, got, state["buys"]


print("① 一輪買齊：記實收差額")
ok, _m, got, _n = run([(1905, 50)], [{1905: 10}, {1905: 50}])
check("成功", ok is True)
check("記 (1905, 40)", got == [(1905, 40)], f"實得 {got}")

print("② 限量分兩輪：每輪各記一筆、加總＝實收")
ok, _m, got, _n = run([(1905, 50)], [{1905: 10}, {1905: 30}, {1905: 50}])
check("成功", ok is True)
check("兩筆各 20", got == [(1905, 20), (1905, 20)], f"實得 {got}")

print("③ 送了但一顆都沒進來：一筆都不記（不猜）")
ok, _m, got, n = run([(1905, 50)], [{1905: 10}])   # 背包永遠停在 10
check("回報沒補齊", ok is False)
check("零筆", got == [], f"實得 {got}")
check(f"買了 {supply.MAX_ROUNDS} 輪（防呆上限）", n == supply.MAX_ROUNDS)

print("④ 輪數用完：最後一輪買到的也要記（迴圈外收尾對帳）")
# 每輪只進 5 顆，MAX_ROUNDS(6) 輪後還缺 → 迴圈外那次 bag_counts 也要對到帳
haves = [{1905: 10 + 5 * i} for i in range(supply.MAX_ROUNDS + 1)]
ok, _m, got, _n = run([(1905, 50)], haves)
check("回報沒補齊", ok is False)
check(f"記了 {supply.MAX_ROUNDS} 筆各 5",
      got == [(1905, 5)] * supply.MAX_ROUNDS, f"實得 {got}")

print("⑤ 兩種東西同一包：各記各的差額")
ok, _m, got, _n = run([(1905, 50), (4836, 20)],
                      [{1905: 40, 4836: 0}, {1905: 50, 4836: 15},
                       {1905: 50, 4836: 20}])
check("成功", ok is True)
check("三筆對帳", sorted(got) == [(1905, 10), (4836, 5), (4836, 15)],
      f"實得 {sorted(got)}")

print("⑥ 沒帶 ledger：行為不變（不記也不炸）")
ok, _m, got, _n = run([(1905, 50)], [{1905: 10}, {1905: 50}],
                      use_ledger=False)
check("照樣買齊成功", ok is True)
check("沒有任何記帳", got == [])

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
