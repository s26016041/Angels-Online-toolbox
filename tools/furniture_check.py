"""傢俱魔力錘狀態機的離線回歸測試（不碰遊戲）。

    py tools\\furniture_check.py

把 furniture 的讀取／送出換成假的，逐條驗 `Run` 的判定表（見 app/game/furniture.py 檔頭）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.game import bag, furniture as fu    # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("✔ " if cond else "✘ ") + name)
    if not cond:
        FAILS.append(name)


class World:
    def __init__(self, bonus=0, hammers=10, base=164):
        self.bonus, self.hammers, self.base = bonus, hammers, base
        self.state = fu.READ_OK
        self.complete = True
        self.sent = 0
        self.on_strike = None        # callable(world) —— 模擬伺服器回應

    def item(self):
        return bag.Item(slot=76, serial=7, stamp=0, type_id=10315, count=1, dura=0,
                        kind=58, price=0, grade=0, dura_max=0)


def install(w: World) -> None:
    def read_state(_sc, _slot, _serial):
        if w.state != fu.READ_OK:
            return None, w.state
        return fu.Furn(item=w.item(), base=w.base, bonus=w.bonus), fu.READ_OK

    def hammers(_sc, items=None, complete=None):
        hs = [fu.Hammer(11118, 71, w.hammers, 35, 450, 0)] if w.hammers else []
        return hs, w.complete

    def strike(_mv, _h, _f):
        w.sent += 1
        if w.on_strike:
            w.on_strike(w)
        return True

    fu.read_state = read_state
    fu.hammers = hammers
    fu.strike = strike


def run_until(r: fu.Run, secs: float = 10.0) -> list[fu.Event]:
    evs: list[fu.Event] = []
    end = time.monotonic() + secs
    while not r.done and time.monotonic() < end:
        evs += r.tick()
        # 快轉：把送出時間往前推，省得真的等 WAIT_MS
        if r.sent_at:
            r.sent_at -= 0.5
        for k in list(r.grace):
            r.grace[k] -= 0.5
    return evs


def main() -> int:
    fu.SETTLE_MS, fu.WAIT_MS = 1000, 3000

    # ① 一路敲到目標
    rolls = iter([50, 120, 300])
    w = World()
    w.on_strike = lambda w: (setattr(w, "bonus", next(rolls)),
                             setattr(w, "hammers", w.hammers - 1))
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 400, "榻榻米"))
    check("① 敲三錘到 464 ≥ 400 停", w.sent == 3 and evs[-1].kind == fu.DONE
          and evs[-1].total == 464)

    # ② 擲出一樣的數字：錘子少了就算一錘
    w = World(bonus=35)
    seq = iter([35, 400])
    w.on_strike = lambda w: (setattr(w, "bonus", next(seq)),
                             setattr(w, "hammers", w.hammers - 1))
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"))
    check("② 同數字也記一錘、第二錘到標", w.sent == 2 and "（一樣）" in evs[0].text
          and evs[-1].kind == fu.DONE)

    # ③ 沒送出去 → 補送有上限
    w = World()
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"), 30)
    check("③ 沒反應補送 3 次後停", w.sent == 1 + fu.MAX_RESEND
          and evs[-1].kind == fu.UNKNOWN)

    # ④ 錘子用完
    w = World(hammers=1)
    w.on_strike = lambda w: (setattr(w, "bonus", 40), setattr(w, "hammers", 0))
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"))
    check("④ 錘子用完停 BLOCKED", w.sent == 1 and evs[-1].kind == fu.BLOCKED)

    # ⑤ 送出後讀不到 → 寬限後停，不猜
    w = World()
    w.on_strike = lambda w: setattr(w, "state", fu.READ_UNREADABLE)
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"))
    check("⑤ 讀不到停 UNKNOWN、只送一錘", w.sent == 1 and evs[-1].kind == fu.UNKNOWN)

    # ⑥ 格子換人 → 停
    w = World()
    w.on_strike = lambda w: setattr(w, "state", fu.READ_SWAPPED)
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"))
    check("⑥ 換人停", w.sent == 1 and evs[-1].kind == fu.UNKNOWN)

    # ⑦ 掃不完整 → 不拿數量比、不補送
    w = World()
    w.on_strike = lambda w: setattr(w, "complete", False)
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 500, "榻榻米"))
    check("⑦ 掃不完整不補送", w.sent == 1 and evs[-1].kind == fu.UNKNOWN)

    # ⑧ 一開始就達標 → 不送
    w = World(bonus=300)
    install(w)
    evs = run_until(fu.Run(None, None, 76, 7, 11118, 400, "榻榻米"))
    check("⑧ 已達標不送", w.sent == 0 and evs[-1].kind == fu.DONE)

    print("全部通過" if not FAILS else f"失敗 {len(FAILS)} 條")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
