"""組隊流程實測台：同一套動作、不同節奏，各跑 N 趟，量「多久組得起來、穩不穩」。

    py tools\\team_bench.py 24712 33464 --trials 6      # 各種節奏比一輪
    py tools\\team_bench.py 24712 33464 --new-flow       # 只驗現行流程

⚠⚠ 跑之前**工具箱要關掉**：這支會自己 `move.acquire` 那兩台的跳板，
   兩份搶同一個 PID 的跳板會把對方的 hook 拔掉（[[mover-per-pid-conflict]]）。
⚠ 每一趟結束一定把兩邊退乾淨（不計時），下一趟才從同一個起點開始。
⚠ 只用**真訊號**判成功：`team.members()` 兩邊互相看得到對方。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, injector, preload                # noqa: E402
from app.core.memory import MemoryScanner                       # noqa: E402
from app.game import channel, locate, move, team                # noqa: E402

FAIL_AFTER = 25.0          # 一趟超過這麼久沒組成＝失敗
RESET_MAX = 20.0           # 趟與趟之間把兩邊退乾淨的上限


class Side:
    def __init__(self, pid: int):
        self.pid = pid
        self.sc = MemoryScanner()
        self.sc.open(pid)
        locate.warm(self.sc)
        self.mover = move.acquire(pid, injector.process_path(pid), self)
        w = next((x for x in preload.windows() if x.pid == pid), None)
        self.hwnd = w.hwnd if w else 0
        self.acct = charname.account_from_title(w.title) if w else ""
        self.name = preload.name_of(pid, self.sc, self.acct, force=True)
        self.ch = charname.channel_from_title(w.title) if w else ""

    def members(self):
        return team.members(self.sc)

    def names(self) -> set[str]:
        m = self.members()
        return {x.name for x in m} if m else set()

    def close(self):
        try:
            move.release(self.pid, self)
        except Exception:                                       # noqa: BLE001
            pass
        self.sc.close()


def both_empty(a: Side, b: Side) -> bool:
    ma, mb = a.members(), b.members()
    return ma == [] and mb == []


def reset(a: Side, b: Side) -> bool:
    """兩邊都退到「沒有隊伍」。回 True＝乾淨了。"""
    t0 = time.time()
    last = 0.0
    while time.time() - t0 < RESET_MAX:
        if both_empty(a, b):
            return True
        if time.time() - last >= 1.0:
            last = time.time()
            if a.members():
                team.leave(a.mover)
            if b.members():
                team.leave(b.mover)
        time.sleep(0.15)
    return both_empty(a, b)


def partied(a: Side, b: Side) -> bool:
    """★ 成功＝**兩邊互相看得到對方**（單邊看到不算，畫面殘影騙過人很多次）。"""
    return b.name in a.names() and a.name in b.names()


def trial(a: Side, b: Side, cfg: dict) -> tuple[bool, float, int]:
    """跑一趟。回 (成功?, 秒數, 送了幾次同意)。a＝隊長（送邀請），b＝分身。"""
    gap = cfg["gap"]
    t0 = time.time()
    if cfg["deny"]:
        team.deny(a.mover)
        team.deny(b.mover)
        if gap:
            time.sleep(gap)
    ok, _why = team.invite(a.mover, b.name, team.SHARE_EVEN)
    if not ok:
        return False, time.time() - t0, 0
    if gap:
        time.sleep(gap)
    joins = 0
    next_join = 0.0
    while time.time() - t0 < FAIL_AFTER:
        if partied(a, b):
            return True, time.time() - t0, joins
        now = time.time()
        if now >= next_join:
            next_join = now + cfg["join_every"]
            team.join(b.mover, b.sc)
            joins += 1
        time.sleep(0.1)
    return False, time.time() - t0, joins


JOIN_EVERY = 0.4
CLEAR_MAX = 8.0            # 等「兩邊名單都空了」的上限


def both_clear(a: Side, b: Side) -> bool:
    return a.members() == [] and b.members() == []


def new_flow(a: Side, b: Side, deny: bool) -> tuple[bool, float, float, int]:
    """新流程一趟。回 (成功?, 總秒數, 等名單清空花的秒數, 同意送幾次)。

    ① 兩隻都拒絕（清掉別人掛著的邀請）＋兩隻都退組 —— 同一拍送，之後不再碰拒絕
    ② 等兩邊名單**真的**清空（硬訊號，不是睡固定秒數）
    ③ 隊長送邀請
    ④ 分身每 JOIN_EVERY 秒補送同意，直到兩邊互相看得到對方
    """
    t0 = time.time()
    if deny:
        team.deny(a.mover)
        team.deny(b.mover)
    team.leave(a.mover)
    team.leave(b.mover)
    tc = time.time()
    while time.time() - tc < CLEAR_MAX:
        if both_clear(a, b):
            break
        time.sleep(0.05)
        if time.time() - tc > 1.0 and int((time.time() - tc) * 2) % 4 == 0:
            if a.members():
                team.leave(a.mover)
            if b.members():
                team.leave(b.mover)
    else:
        return False, time.time() - t0, time.time() - tc, 0
    clear_s = time.time() - tc
    team.invite(a.mover, b.name, team.SHARE_EVEN)
    joins, nxt = 0, 0.0
    while time.time() - t0 < FAIL_AFTER:
        if partied(a, b):
            return True, time.time() - t0, clear_s, joins
        now = time.time()
        if now >= nxt:
            nxt = now + JOIN_EVERY
            team.join(b.mover, b.sc)
            joins += 1
        time.sleep(0.05)
    return False, time.time() - t0, clear_s, joins



CONFIGS = [
    # 名稱, 動作間隔, 要不要先拒絕, 補送同意的間隔
    ("現行（間隔 3 秒）", dict(gap=3.0, deny=True, join_every=3.0)),
    ("間隔 1 秒", dict(gap=1.0, deny=True, join_every=1.0)),
    ("間隔 0.5 秒", dict(gap=0.5, deny=True, join_every=0.5)),
    ("不先拒絕・間隔 0.5 秒", dict(gap=0.5, deny=False, join_every=0.5)),
    ("零等待・同意每 0.4 秒補送", dict(gap=0.0, deny=True, join_every=0.4)),
    ("零等待・不先拒絕", dict(gap=0.0, deny=False, join_every=0.4)),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("leader", type=int)
    ap.add_argument("partner", type=int)
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--only", type=int, default=-1, help="只跑第幾個設定（0 起算）")
    ap.add_argument("--set-channel", type=int, default=0,
                    help="先把 partner 換到這個分流（1 起算）")
    ap.add_argument("--new-flow", action="store_true",
                    help="只驗現行產品流程（拒絕＋退組→等清空→邀請→同意）")
    args = ap.parse_args()

    a, b = Side(args.leader), Side(args.partner)
    print(f"隊長 {a.name}（{a.acct}）{a.ch}　分身 {b.name}（{b.acct}）{b.ch}")
    if not (a.mover and a.mover.active and b.mover and b.mover.active):
        print("⛔ 跳板沒裝起來")
        return 1
    if args.set_channel and str(args.set_channel) not in (b.ch or ""):
        print(f"→ 把 {b.name} 換到 雅典娜-{args.set_channel} …", flush=True)
        channel.switch(b.mover, args.set_channel, channel.FALLBACK_MAX)
        # 等標題真的變成新分流（換分流＝換連線，要重連）
        t0 = time.time()
        while time.time() - t0 < 60.0:
            cur = channel.current(b.hwnd)
            if cur == args.set_channel:
                break
            time.sleep(1.0)
        b.close()                      # ⚠ 先還跳板再重建，不然 owner 會殘留
        time.sleep(3.0)
        b = Side(args.partner)
        print(f"   現在：{b.name} {b.ch}（等 {time.time() - t0:.0f} 秒）")
    if a.ch and b.ch and a.ch != b.ch:
        print(f"⛔ 跨分流（{a.ch} vs {b.ch}）—— 組不了隊，先換到同一個分流")
        a.close()
        b.close()
        return 1

    if args.new_flow:
        times, fails = [], 0
        for n in range(args.trials):
            ok, secs, clear_s, jn = new_flow(a, b, True)
            print(f"  新流程 第 {n + 1} 趟：{'成' if ok else '✘失敗'} {secs:.2f}s"
                  f"（等清空 {clear_s:.2f}s、同意 {jn} 次）", flush=True)
            if ok:
                times.append(secs)
            else:
                fails += 1
            time.sleep(0.5)
        if times:
            print(f"→ 新流程：成功 {len(times)}／失敗 {fails}　"
                  f"中位 {statistics.median(times):.2f}s　最慢 {max(times):.2f}s")
        a.close()
        b.close()
        return 1 if fails else 0

    rows = []
    for i, (label, cfg) in enumerate(CONFIGS):
        if args.only >= 0 and i != args.only:
            continue
        times, fails, joins = [], 0, []
        for n in range(args.trials):
            if not reset(a, b):
                print(f"  ⚠ {label}：第 {n + 1} 趟開始前退不乾淨，跳過")
                fails += 1
                continue
            ok, secs, jn = trial(a, b, cfg)
            print(f"  {label} 第 {n + 1} 趟：{'成' if ok else '失敗'} {secs:.1f}s"
                  f"（同意送 {jn} 次）", flush=True)
            if ok:
                times.append(secs)
                joins.append(jn)
            else:
                fails += 1
        med = statistics.median(times) if times else float("nan")
        rows.append((label, len(times), fails, med,
                     min(times) if times else float("nan"),
                     max(times) if times else float("nan"),
                     statistics.median(joins) if joins else 0))
    reset(a, b)
    print()
    print("設定".ljust(26), "成功/失敗", "中位數", "最快", "最慢", "同意次數")
    for label, ok_n, fails, med, lo, hi, jn in rows:
        print(f"{label.ljust(26)} {ok_n}/{fails}　　{med:5.1f}s {lo:5.1f}s "
              f"{hi:5.1f}s {jn:.0f}")
    a.close()
    b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
