r"""回城那一步的回歸測試 —— **趴趴GO 優先、天使之翼當退路**（2026-09-18）。

    py tools\flyhome_check.py

測的是 `supply._fly_home()` 與 `run_full_supply` 第 2 步的分支，全部用替身，
**不碰遊戲、不碰 config**。為什麼要有它：補給主流程出過「整趟放棄 → 停機六小時」
（`1ee3e00`），改這一段一定要有東西擋住回歸。

涵蓋：
  ① 一發就到 → 沒燒翼
  ② 送不出去／沒落地 → 重送，滿 HOME_TRIES 次才退回燒翼
  ③ 傳送表裡沒有那座城的落點 → 直接退回燒翼
  ④ 落到**別座城**不算到（只認設定的那座；分流編號算同一張）
  ⑤ 趴趴GO 沒到、背包也沒翼 → 失敗訊息要說得出兩件事都沒成
  ⑥ 背包**讀不到** ≠ 沒有 → 等人站穩再問一次（bag-false-empty-guards）
  ⑦ 落點挑「離第一個要辦事的 NPC 最近」那個（傳給 jumpmap.nearest 的座標）
  ⑧ 人已經站在**任何**補給城裡 → 一發都不送（使用者 2026-09-18 定）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import farmsettings, supply                    # noqa: E402

FAILED = 0


def check(name, cond, extra=""):
    global FAILED
    print(("  ✔ " if cond else "  ✘ ") + name + ("　" + str(extra) if extra else ""))
    if not cond:
        FAILED += 1


class FakeJump:
    """傳送表替身。`land` = 每次 teleport 之後場景要變成什麼（None＝不動）。"""

    def __init__(self, entry=True, sends=(), land=None):
        self.entry = entry
        self.sends = list(sends)          # 每次 teleport 回 (ok, msg)
        self.land = land
        self.calls = []                   # 每次 nearest 收到的 (scene, x, y)
        self.sent = []

    def nearest(self, sid, x=None, y=None, scanner=None):
        self.calls.append((sid, x, y))
        if not self.entry:
            return None
        return type("E", (), {"jump_id": 99, "name": "假落點"})()

    def teleport(self, mover, scanner, jump_id):
        self.sent.append(jump_id)
        return self.sends.pop(0) if self.sends else (True, "送出")


class FakeScene:
    """場景替身：`seq` 是每次 current_id() 依序回的值（用完維持最後一個）。"""

    def __init__(self, seq):
        self.seq = list(seq)

    def current_id(self, scanner=None):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

    @staticmethod
    def scene_name(sid):
        return f"城{sid}"

    @staticmethod
    def same_map(a, b):
        return a is not None and b is not None and (a & 0xFFFF) == (b & 0xFFFF)


def run(city=88, jump=None, scene_seq=(7,), wing=(5, True), use_ok=True,
        wing_land=88, ready_scene=None):
    """跑一次 _fly_home，回 (結果, 說明, 用到的替身)。"""
    jp = jump if jump is not None else FakeJump()
    sc = FakeScene(list(scene_seq))
    used = {"wing": 0}

    def fake_use(mover, slot):
        used["wing"] += 1
        return use_ok

    def fake_ready(scanner, *a, **k):
        if ready_scene is not None:
            sc.seq = [ready_scene]
        return True

    old = (supply.jumpmap, supply.scene, supply._wing_slot, supply._wing_count,
           supply.recall.use_item, supply._nap, supply._wait_map_change,
           supply._wait_ready, supply.HOME_WAIT)
    supply.jumpmap, supply.scene = jp, sc
    supply._wing_slot = lambda s: (wing[0], wing[1])
    supply._wing_count = lambda s: 5
    supply.recall.use_item = fake_use
    supply._nap = lambda *a, **k: None
    supply._wait_map_change = lambda s, frm, t: wing_land
    supply._wait_ready = fake_ready
    supply.HOME_WAIT = 0.01                # 測試不要真的等
    try:
        got, msg = supply._fly_home(object(), object(), 7, city, lambda m: None)
    finally:
        (supply.jumpmap, supply.scene, supply._wing_slot, supply._wing_count,
         supply.recall.use_item, supply._nap, supply._wait_map_change,
         supply._wait_ready, supply.HOME_WAIT) = old
    return got, msg, jp, used


print("① 一發就到 → 沒燒翼")
got, msg, jp, used = run(scene_seq=[7, 88])
check("到了設定的那座城", got == 88, got)
check("只送一發", len(jp.sent) == 1, jp.sent)
check("一張翼都沒燒", used["wing"] == 0)
check("說明說得出沒燒翼", "沒燒翼" in msg, msg)

print("② 一直沒落地 → 重送滿 HOME_TRIES 次才退回燒翼")
got, msg, jp, used = run(scene_seq=[7])
check(f"送滿 {supply.HOME_TRIES} 次", len(jp.sent) == supply.HOME_TRIES, jp.sent)
check("退回燒了一張翼", used["wing"] == 1)
check("翼那條路成功就算成功", got == 88, got)
check("說明說得出燒了翼", "翼" in msg, msg)

print("③ 送不出去（指令槽忙／連線是 0）也要重送")
jp = FakeJump(sends=[(False, "還沒連上線"), (False, "排不進去"), (True, "送出")])
got, msg, jp, used = run(scene_seq=[7, 7, 7, 88], jump=jp)
check("三次都有試", len(jp.sent) == 3, jp.sent)
check("最後到了", got == 88, got)

print("④ 傳送表裡沒有那座城的落點 → 直接退回燒翼，一發都不送")
got, msg, jp, used = run(jump=FakeJump(entry=False), scene_seq=[7])
check("一發都沒送", not jp.sent)
check("燒了翼", used["wing"] == 1)

print("⑤ 落到別座城不算到（只認設定的那座）")
got, msg, jp, used = run(city=88, scene_seq=[7, 3])     # 掉到聖光城
check("不當成到了 → 退回燒翼", used["wing"] == 1, msg)
print("   分流編號算同一張圖")
got, msg, jp, used = run(city=88, scene_seq=[7, (1 << 16) | 88])
check("分流算到了", got == (1 << 16) | 88, got)
check("沒燒翼", used["wing"] == 0)

print("⑥ 趴趴GO 沒到、背包也**確定**沒翼 → 說得出兩件事都沒成")
got, msg, jp, used = run(scene_seq=[7], wing=(None, True))
check("回失敗", got is None)
check("訊息講了趴趴GO 沒到", "趴趴GO" in msg, msg)
check("訊息講了沒有回程道具", "沒有" in msg, msg)

print("⑦ 背包**讀不到** ≠ 沒有 → 等人站穩再問一次")
got, msg, jp, used = run(scene_seq=[7], wing=(None, False))
check("不是直接判沒有（有走等人站穩那條）", got is None)
check("訊息要說是「讀不到」不是「沒有」", "讀不到" in msg, msg)
print("   等的期間被送到城裡了 → 就用那座城，不燒翼")
got, msg, jp, used = run(scene_seq=[7], wing=(None, False), ready_scene=3)
check("直接收下那座城", got == 3, got)
check("沒燒翼", used["wing"] == 0)

print("⑧ 落點挑「離第一個要辦事的 NPC 最近」那個")
got, msg, jp, used = run(city=88, scene_seq=[7, 88])
want = supply.NPC_TABLE.get(88, {}).get("bank")
check("nearest 收到的是那座城", jp.calls and jp.calls[0][0] == 88, jp.calls)
check("而且帶了銀行的座標", want is not None
      and jp.calls[0][1:] == (want[1], want[2]), (jp.calls, want))

print("⑨ 設定值本身的防呆")
check("清單只有 5 座城", len(farmsettings.SUPPLY_CITIES) == 5,
      farmsettings.SUPPLY_CITIES)
check("預設是棕櫚基地 88", farmsettings.CITY_DEFAULT == 88)
check("五座城全都有藥水商人", all(
    "buy" in (supply.NPC_TABLE.get(s) or {}) for s in farmsettings.SUPPLY_CITIES))
check("亂填 → 退回預設", farmsettings.clamp_city(9999) == 88)
check("填字串 → 退回預設", farmsettings.clamp_city("abc") == 88)
check("None → 退回預設", farmsettings.clamp_city(None) == 88)
check("填清單裡的照收", farmsettings.clamp_city(26) == 26)

print("\n" + ("OK：全部通過" if not FAILED else f"⛔ 有 {FAILED} 項沒過"))
raise SystemExit(1 if FAILED else 0)
