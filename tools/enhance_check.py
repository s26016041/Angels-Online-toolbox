"""強化裝備：「讀不到」不准說成「裝備被打掉了」—— 離線測試。

    py tools\\enhance_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

2026-08-28 /_audit 抓到的兩個洞，都是同一個復發六次的老坑
（[[bag-false-empty-guards]]：讀不到回的空清單跟真的沒有長得一模一樣）：

① `gear.read()` 有**六種**理由回 `None`，其中四種是**暫時讀不到**
   （背包表頭讀失敗、格號界外、物件那一塊沒讀到、掃描沒掃完），
   而 `enhance.Run._check()` 把六種一律當成「強化失敗消失」。
   ⇒ 換個地圖、或背包剛好在搬，畫面就跳出「XXX 強化失敗消失」——
     對著使用者宣告他的裝備沒了，其實東西好好的。
   （旁邊的 `gear.in_bag()` 一直有回 `complete` 旗標正是為了這件事，
     偏偏最要命的 `read()` 沒有。）

② 錘子數量用 `bag.scan()` 沒掃完的結果去比。數量是拿來分辨
   「沒送出去（可以補送）」與「錘子扣掉了（不准再送）」的 ——
   沒掃到害它判成「沒送出去」，就會**再花一支錘子**打一件狀態未知的裝備。

修法：`gear.read_state()` 回三態（ok／unreadable／absent），
`enhance.hammers()` 回 (格號, 總數, **掃完了嗎**)；
讀不到就等 `UNREADABLE_SECS`，等完還是讀不到就**大聲說驗不了**，
絕不猜結果（CLAUDE.md：失效只允許大聲停用或安全退化）。

⚠ 純離線：假的 scanner／背包／時鐘，不碰遊戲、不送封包。
  **只換 I/O（讀記憶體那一層），判斷邏輯跑真的。**
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import bag, enhance, gear                    # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, got: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {name}" + (f"　—— {got}" if not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 假的 I/O 層
# ---------------------------------------------------------------------------
SLOT = 22
SERIAL = 0xABCD1234


class FakeGear:
    """只要有 serial / enhance 兩個欄位就夠 —— 決策邏輯只看這兩個。"""

    def __init__(self, serial: int, level: int) -> None:
        self.serial = serial
        self.enhance = level


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


CLOCK = Clock()
enhance.time = CLOCK                       # 只換時鐘，狀態機跑真的


class Run(enhance.Run):
    """真的 `enhance.Run`，只是不真的送封包（記下送了幾發）。"""

    def __init__(self, **kw) -> None:
        super().__init__(scanner=None, mover=None, gear_slot=SLOT,
                         serial=SERIAL, **kw)
        self.sent = 0


enhance.strike = lambda mover, h, g: True  # 送包那一層換掉，其他照跑
_REAL_STRIKE_COUNT = [0]


def _strike(mover, hslot, gslot):
    _REAL_STRIKE_COUNT[0] += 1
    return True


enhance.strike = _strike


def set_world(state: str, level: int = 5, hammer: int = 10,
              complete: bool = True, serial: int = SERIAL) -> None:
    """決定這一拍 `gear.read_state` 與 `enhance.hammers` 會回什麼。"""
    if state == gear.READ_OK:
        gear.read_state = lambda sc, slot: (FakeGear(serial, level), gear.READ_OK)
    else:
        gear.read_state = lambda sc, slot: (None, state)
    enhance.gear = gear
    enhance.hammers = lambda sc: ((3, hammer, complete) if hammer
                                  else (None, 0, complete))


def fresh(target: int = 10) -> Run:
    _REAL_STRIKE_COUNT[0] = 0
    return Run(target=target, name="測試裝備")


def kinds(evs) -> list[str]:
    return [e.kind for e in evs]


# ---------------------------------------------------------------------------
print(__doc__.splitlines()[0])
print()
print("① gear.read_state 分得出「讀不到」與「真的不在」")

REAL_READ_STATE = gear.read_state


class FakeScanner:
    def __init__(self, reads: dict) -> None:
        self.reads = reads

    def _read_bytes(self, addr: int, n: int):
        return self.reads.get((addr, n))


_real_head, _real_scan = bag.head, bag.scan
BEGIN = 0x10000000

# (a) 表頭讀不到 ＝ 換地圖／還沒進場 → unreadable，不是 absent
bag.head = lambda sc: None
_g, st = REAL_READ_STATE(FakeScanner({}), SLOT)
check("表頭讀不到 → unreadable（不是「裝備不見了」）",
      st == gear.READ_UNREADABLE, f"實得 {st}")

# (b) 指標讀得到、是 0 ＝ 那格真的空著 → absent
bag.head = lambda sc: (BEGIN, 200)
_g, st = REAL_READ_STATE(
    FakeScanner({(BEGIN + SLOT * 4, 4): b"\x00\x00\x00\x00"}), SLOT)
check("指標讀到 0 → absent（這格真的空著）", st == gear.READ_ABSENT,
      f"實得 {st}")

# (c) 指標非 0，但物件那一塊讀不到 → unreadable
_g, st = REAL_READ_STATE(
    FakeScanner({(BEGIN + SLOT * 4, 4): b"\x00\x00\x00\x20"}), SLOT)
check("指標有值但物件讀不到 → unreadable", st == gear.READ_UNREADABLE,
      f"實得 {st}")

# (d) 物件讀到了，但 bag.scan 沒掃完 → unreadable（不能說這格沒東西）
bag.scan = lambda sc, a=None, b=None: ([], False)
_g, st = REAL_READ_STATE(
    FakeScanner({(BEGIN + SLOT * 4, 4): b"\x00\x00\x00\x20",
                 (0x20000000, bag.ITEM_SPAN): b"\x00" * bag.ITEM_SPAN}), SLOT)
check("掃描沒掃完 → unreadable（不是「這格沒東西」）",
      st == gear.READ_UNREADABLE, f"實得 {st}")

# (e) 物件讀到了、掃描也掃完了、就是沒東西 → absent
bag.scan = lambda sc, a=None, b=None: ([], True)
_g, st = REAL_READ_STATE(
    FakeScanner({(BEGIN + SLOT * 4, 4): b"\x00\x00\x00\x20",
                 (0x20000000, bag.ITEM_SPAN): b"\x00" * bag.ITEM_SPAN}), SLOT)
check("掃完了確實沒東西 → absent", st == gear.READ_ABSENT, f"實得 {st}")

bag.head, bag.scan = _real_head, _real_scan

print()
print("② 送出後讀不到 —— 不准說「強化失敗消失」")

r = fresh()
set_world(gear.READ_OK, level=5)
r.tick()                                        # 送出一發
check("有送出去", _REAL_STRIKE_COUNT[0] == 1, f"送了 {_REAL_STRIKE_COUNT[0]} 發")

set_world(gear.READ_UNREADABLE)
CLOCK.t += 0.2
evs = r.tick()
check("讀不到的第一拍：不下結論、也不停手",
      evs == [] and not r.done, f"evs={kinds(evs)} done={r.done}")

CLOCK.t += enhance.UNREADABLE_SECS + 0.1
evs = r.tick()
check("寬限期到了才停手", r.done, "還沒停")
check("停手的理由是「驗不了」不是「消失」",
      kinds(evs) == [enhance.UNKNOWN], f"實得 {kinds(evs)}")
check("訊息不准說裝備消失",
      "消失" not in evs[0].text, f"訊息：{evs[0].text}")

print()
print("③ 真的消失（確定讀到那格空了）還是要說消失")

r = fresh()
set_world(gear.READ_OK, level=5)
r.tick()
set_world(gear.READ_ABSENT)
CLOCK.t += 0.2
evs = r.tick()
check("absent → GONE「強化失敗消失」",
      kinds(evs) == [enhance.GONE] and "消失" in evs[0].text,
      f"實得 {kinds(evs)}")

print()
print("④ 讀不到又恢復 —— 要接著跑，不是永久停在那裡")

r = fresh()
set_world(gear.READ_OK, level=5)
r.tick()
set_world(gear.READ_UNREADABLE)
CLOCK.t += 1.0
r.tick()
set_world(gear.READ_OK, level=6)                # 恢復了，而且成功了
CLOCK.t += 0.2
evs = r.tick()
check("恢復後判成功", kinds(evs) == [enhance.SUCCESS], f"實得 {kinds(evs)}")
check("沒被上一輪的讀不到卡住", not r.done, "被停掉了")

# 寬限期的計時要歸零，否則第二次讀不到會立刻放棄
set_world(gear.READ_UNREADABLE)
CLOCK.t += 0.2
r.tick()                                        # 這一拍會送第二發前先讀
check("寬限期有歸零（不是沿用上一次的起點）", not r.done,
      "第二次讀不到就馬上停了")

print()
print("⑤ 背包沒掃完時，不准拿錘子數量當證據")

r = fresh()
set_world(gear.READ_OK, level=5, hammer=10)
r.tick()
check("送出第一發", _REAL_STRIKE_COUNT[0] == 1)

# 逾時了、等級沒變，而且背包沒掃完 → 不准補送（補送＝再花一支錘子）
set_world(gear.READ_OK, level=5, hammer=0, complete=False)
CLOCK.t += enhance.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("沒掃完不補送", _REAL_STRIKE_COUNT[0] == 1,
      f"送了 {_REAL_STRIKE_COUNT[0]} 發")
check("沒掃完也不下結論（等）", evs == [] and not r.done,
      f"evs={kinds(evs)} done={r.done}")

CLOCK.t += enhance.UNREADABLE_SECS + 0.1
evs = r.tick()
check("等完還是沒掃完 → 停手說驗不了",
      r.done and kinds(evs) == [enhance.UNKNOWN], f"實得 {kinds(evs)}")

print()
print("⑥ 掃得完整、錘子沒少 ＝ 真的沒送出去 → 才准補送")

r = fresh()
set_world(gear.READ_OK, level=5, hammer=10)
r.tick()
set_world(gear.READ_OK, level=5, hammer=10, complete=True)
CLOCK.t += enhance.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("錘子沒少 → 補送", kinds(evs) == [enhance.UNKNOWN]
      and "補送" in evs[0].text and not r.done, f"實得 {kinds(evs)}")

r = fresh()
set_world(gear.READ_OK, level=5, hammer=10)
r.tick()
set_world(gear.READ_OK, level=5, hammer=9, complete=True)
CLOCK.t += enhance.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("錘子少了卻沒變化 → 停手，不再打",
      r.done and kinds(evs) == [enhance.UNKNOWN], f"實得 {kinds(evs)}")

print()
print("⑦ 開工前讀不到 —— 一發都還沒送，更不該說消失")

r = fresh()
set_world(gear.READ_UNREADABLE)
evs = r.tick()
check("第一拍不下結論", evs == [] and not r.done, f"evs={kinds(evs)}")
check("完全沒送出去", _REAL_STRIKE_COUNT[0] == 0)
CLOCK.t += enhance.UNREADABLE_SECS + 0.1
evs = r.tick()
check("等完停手，理由是驗不了、且說明沒有動手",
      r.done and kinds(evs) == [enhance.UNKNOWN]
      and "沒有動手" in evs[0].text, f"實得 {evs[0].text if evs else evs}")

print()
print("⑧ 那一格換成別件（serial 不同）→ 停，但不算「被打掉」")

r = fresh()
set_world(gear.READ_OK, level=5)
r.tick()
set_world(gear.READ_OK, level=5, serial=SERIAL + 1)
CLOCK.t += 0.2
evs = r.tick()
check("serial 變了就停", r.done, "沒停")
check("不說消失、說驗不了",
      kinds(evs) == [enhance.UNKNOWN] and "消失" not in evs[0].text,
      f"實得 {kinds(evs)}：{evs[0].text if evs else ''}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print(f"OK：全部通過（{8} 組）")
