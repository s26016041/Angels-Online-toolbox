# -*- coding: utf-8 -*-
r"""擊殺歸屬的離線回歸：`py tools\kills_check.py`

① castwatch.kills_since：只認 op=0x0a、@10==7 的死亡廣播，回 (怪 eid, 殺手)。
② farm_tab._poll_kills：殺手==我 或 ==我的召喚物才 +1；別人殺的不算；
   監聽沒裝→標「停數」；換一份 hook→從它現在的 write_count 起算；
   殺的是王→舉打王旗（種類 ID 從清單或 _recent_tid 查）。
版面出處：memory `kill-credit-packet`（2026-09-23 實錄）。
"""
from __future__ import annotations

import os
import struct
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.game import castwatch                                  # noqa: E402

fails = 0


def check(name, ok, note=""):
    global fails
    print(("  ✅ " if ok else "  ❌ ") + name + (f"　{note}" if note and not ok else ""))
    if not ok:
        fails += 1


ME = 0x1FCD01CA
PET = 0x5FFB00DC
OTHER = 0x5FFA00DB


def kill_pkt(victim, killer, tag=7):
    return struct.pack("<HIII", 0x0A, victim, killer, tag) + b"\0\0"


def cast_pkt():
    return struct.pack("<HIHI", 0x1D, ME, 0x0301, 946) + b"\0" * 4


print("① kills_since 解包")


class FakeHook(castwatch.CastHook):
    def __init__(self, slots):
        super().__init__(1)
        self._active = True
        self.slots = slots

    def _slots_since(self, since):
        return self.slots[since:]


h = FakeHook([kill_pkt(0x4D65014B, ME), cast_pkt(), kill_pkt(0x4D66014C, OTHER),
              kill_pkt(0x4D670001, ME, tag=3), kill_pkt(0, ME), b"\x0a\x00\x01"])
got = h.kills_since(0)
check("兩包死亡廣播都解到、施放包/tag≠7/victim=0/太短都濾掉",
      got == [(0x4D65014B, ME), (0x4D66014C, OTHER)], str(got))
check("since 起算", h.kills_since(2) == [(0x4D66014C, OTHER)])

print("② _poll_kills")
from app.tabs import farm_tab                                   # noqa: E402


class Ring:
    def __init__(self):
        self.pk: list[bytes] = []
        self.active = True
        self.inst = True                 # 遊戲裡那 7 bytes 還指著我（installed()）

    def installed(self):
        return self.inst

    def mark_lost(self):
        self.active = False

    def write_count(self):
        return len(self.pk)

    def kills_since(self, since):
        return FakeHook(self.pk).kills_since(since)

    def read_since(self, since):
        return since, len(self.pk), [(len(d), d) for d in self.pk[since:]]


class Lbl:
    def __init__(self):
        self.t = ""

    def text(self):
        return self.t

    def setText(self, t):
        self.t = t


class Page:
    _poll_kills = farm_tab.CharFarmPage._poll_kills
    _bump_kills = farm_tab.CharFarmPage._bump_kills
    _show_kills = farm_tab.CharFarmPage._show_kills
    _note_boss_kill = farm_tab.CharFarmPage._note_boss_kill
    _feed_loot = farm_tab.CharFarmPage._feed_loot

    def __init__(self):
        self._castwatch = None
        self._kill_cw = None
        self._kill_wc = 0
        self._kill_poll_t = -9.0
        self._loot = farm_tab.loot.Loot()
        self._loot_t = 0.0
        self._loot_poll_t = 0.0
        self._home = None
        self.char_name = self.account = "t"
        self._recent_tid = {}
        self._pet_eids = {}
        self._kills = 0
        self.kills_lbl = Lbl()
        self.mons = []
        self.sc = None
        self.rot_boss_cb = types.SimpleNamespace(isChecked=lambda: True)
        self._rot_seq = []
        self._rot_settle = 0.0
        self._rot_boss_hit = False
        self.dbg = []

    def _dbg(self, s):
        self.dbg.append(s)

    def _my_id(self):
        return ME

    def poll(self):
        self._kill_poll_t = -9.0          # 跳過節流
        self._poll_kills()


pet_now = [0]
farm_tab.bag.scan = lambda sc, *a, **k: ([], False)
farm_tab.player.pet_eid = lambda sc: pet_now[0]
boss_ids = {777}
farm_tab.monsters.is_boss = lambda sc, tid: (True if tid in boss_ids else False)

p = Page()
p.poll()
check("監聽沒裝 → 標停數", "停數" in p.kills_lbl.text(), p.kills_lbl.text())

ring = Ring()
ring.pk = [kill_pkt(0x1111, ME)]        # 裝 hook 前環槽裡的舊包
p._castwatch = ring
p.poll()
check("剛換 hook：舊包不算、標籤恢復", p._kills == 0 and p.kills_lbl.text() == "已擊殺 0 隻",
      f"{p._kills} {p.kills_lbl.text()}")

ring.pk += [kill_pkt(0x2222, ME), kill_pkt(0x3333, OTHER)]
p.poll()
check("我殺的 +1、別人殺的不算", p._kills == 1 and p.kills_lbl.text() == "已擊殺 1 隻",
      f"{p._kills} {p.dbg[-2:]}")
check("別人殺的有 debug 說明", any("不是我" in s for s in p.dbg))

pet_now[0] = PET
p.poll()                                 # 先看到召喚物
ring.pk += [kill_pkt(0x4444, PET)]
p.poll()
check("我的召喚物殺的 +1", p._kills == 2, str(p._kills))

ring.pk += [kill_pkt(0x5555, 0x12345678)]
p.poll()
check("別人的召喚物不算", p._kills == 2)

p.mons = [types.SimpleNamespace(eid=0x6666, type_id=777)]
ring.pk += [kill_pkt(0x6666, ME)]
p.poll()
check("殺的是王（清單裡查到）→ 舉打王旗", p._rot_boss_hit and p._kills == 3)

p._rot_boss_hit = False
p.mons = []
p._recent_tid[0x7777] = (777, time.monotonic())
ring.pk += [kill_pkt(0x7777, ME)]
p.poll()
check("清單沒牠、_recent_tid 有 → 一樣舉旗", p._rot_boss_hit)

p._rot_boss_hit = False
ring.pk += [kill_pkt(0x8888, ME)]
p.poll()
check("種類查不到 → 算擊殺但不舉旗", p._kills == 5 and not p._rot_boss_hit)

ring.pk += [kill_pkt(0x9999, ME)] * 3
p.poll()
p.poll()
check("同一批不重複數", p._kills == 8, str(p._kills))

ring.active = False
p.poll()
check("hook 死了 → 標停數、不當機", "停數" in p.kills_lbl.text())

ring2 = Ring()
ring2.pk = [kill_pkt(0xAAAA, ME)]
p._castwatch = ring2
p.poll()
ring2.pk += [kill_pkt(0xBBBB, ME)]
p.poll()
n_before = p._kills
ring2.inst = False                        # 監聽被別的行程拆了：旗標還舉著、環槽沒人寫
ring2.pk += [kill_pkt(0xCCCC, ME)]        # 這包其實不會再進來，模擬舊槽殘留
p.poll()
check("hook 被拆（installed 為 False）→ 放下旗標、標停數、不再數",
      not ring2.active and "停數" in p.kills_lbl.text() and p._kills == n_before,
      f"{ring2.active} {p.kills_lbl.text()} {p._kills}")
check("被拆有 debug 說明", any("被拆" in x for x in p.dbg))
ring3 = Ring()
ring3.pk = [kill_pkt(0xDDDD, ME)]         # 重裝前的舊包
p._castwatch = ring3
p.poll()
ring3.pk += [kill_pkt(0xEEEE, ME)]
p.poll()
check("重裝後從新 hook 起算、繼續數", p._kills == n_before + 1 and "停數" not in p.kills_lbl.text(),
      f"{p._kills} {p.kills_lbl.text()}")

print("失敗 %d 項" % fails)
raise SystemExit(1 if fails else 0)
