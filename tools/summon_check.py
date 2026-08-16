# -*- coding: utf-8 -*-
"""自動召喚離線回歸：認養「比範本編號不比名字」（吸血鬼Ⅲ bug）。

    py tools\\summon_check.py

背景（2026-08-16 朋友實掛回報）：勾自動召喚＋F11「召喚吸血鬼Ⅲ」(150)，
明明召喚物還活著卻每 6 秒重召一次。根因＝認養拿「技能名去掉『召喚』」
（＝「吸血鬼Ⅲ」）跟實體名**全等**比對，但這招召出來的怪叫「**死亡吸血鬼Ⅲ**」
（magic.xml 動態參數1=222 → str_monster 1200000222）→ 永遠認不到 →
槽驗證也過不了（同樣卡名字）→ 每 RETRY=6 秒白放、把活的那隻不停換掉。

修法＝skills.summon_of()（skill_range.tsv.gz 第 5 欄）給出召喚物 type_id，
認養／槽驗證改比 type_id，名字退居退路且改「包含」。

全部離線：patch 掉 attack.cast_at / entity.player_pos / player.pet_eid /
summon.time，不碰遊戲。照 test-via-button memory：假物件 patch 進**用到的
模組命名空間**（summon.py 是 `from app.game import attack, ...` 拿模組物件，
patch 模組屬性即可）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.game import attack, entity, player, quickbar, skills, summon  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("✅" if ok else "⛔"), name, ("— " + detail) if detail else "")
    if not ok:
        FAILS.append(name)


# ── 假遊戲層 ────────────────────────────────────────────────────────────
class Clock:
    t = 1000.0

    @classmethod
    def monotonic(cls) -> float:
        return cls.t


class World:
    """一次測試情境的可控狀態。"""

    def __init__(self) -> None:
        self.slot = 0            # player.pet_eid 回的值
        self.pos = (100.4, 200.6)
        self.casts: list[tuple] = []
        self.key_uses: list[tuple] = []


W = World()
summon.time = Clock                                    # 控制時間
attack.cast_at = lambda mv, sid, eid, x, y: (W.casts.append((sid, x, y)), True)[1]
quickbar.use = lambda mv, sc, slot, page: (W.key_uses.append((slot, page)), True)[1]
entity.player_pos = lambda sc, obj: W.pos
player.pet_eid = lambda sc: W.slot


class FakeMover:
    active = True


MV = FakeMover()
SC = object()          # scanner 佔位（假層都不看它）


def pet(eid: int, tid: int, name: str, x: float, y: float) -> entity.Entity:
    return entity.Entity(0x1000 + eid, eid, tid, name, x, y, kind=4)


def run_until_cast(s: summon.AutoSummon, pets, secs: float,
                   tick: float = 0.5) -> int:
    """往前走 secs 秒、每 tick 呼叫一次 step，回傳期間新增的施放數。"""
    before = len(W.casts)
    end = Clock.t + secs
    while Clock.t < end:
        Clock.t += tick
        s.step(SC, MV, 0xDEAD, pets)
    return len(W.casts) - before


# ── 情境 1：吸血鬼Ⅲ —— bug 本尊 ────────────────────────────────────────
check("資料表：summon_of(150)=222（召喚吸血鬼Ⅲ→死亡吸血鬼Ⅲ）",
      skills.summon_of(150) == 222, f"got {skills.summon_of(150)}")
check("資料表：summon_of(781)=5014（噬魂怪實機對照）",
      skills.summon_of(781) == 5014, f"got {skills.summon_of(781)}")
check("資料表：非召喚技能查不到（743 幻影刺殺）",
      skills.summon_of(743) is None)

s = summon.AutoSummon()
s.adopt(150, page=0)
s.arm()
check("adopt(150)：expect_tid=222、expect=吸血鬼Ⅲ",
      s.expect_tid == 222 and s.expect == "吸血鬼Ⅲ",
      f"tid={s.expect_tid} name={s.expect!r}")

Clock.t += 0.5
s.step(SC, MV, 0xDEAD, [])                     # ④ 施放
check("第一拍就施放（帶自己腳下座標）",
      W.casts == [(150, 100, 200)], f"casts={W.casts}")

# 1.5 秒後召喚物出現：名字「死亡吸血鬼Ⅲ」≠「吸血鬼Ⅲ」但 type_id=222
vamp = pet(9001, 222, "死亡吸血鬼Ⅲ", 100.5, 200.5)
Clock.t += 1.5
s.step(SC, MV, 0xDEAD, [vamp])
check("★名字對不上也認得（比 type_id）", s.alive and len(W.casts) == 1,
      f"alive={s.alive} casts={len(W.casts)}")

# 槽跟上 → 驗證通過；之後 60 秒 eid 連環重建也不准重召
W.slot = 9001
n = run_until_cast(s, [vamp], 1.0)
check("槽值=認養 eid → _slot_ok", s._slot_ok and n == 0)
W.slot = 9002                                   # 伺服器重建：新 eid
vamp2 = pet(9002, 222, "死亡吸血鬼Ⅲ", 103.0, 201.0)
n = run_until_cast(s, [vamp2], 60.0)
check("★活著 60 秒（含 eid 重建）零重召——朋友的症狀不再出現", n == 0,
      f"多放了 {n} 次")

# 槽歸零 8 秒 → 才判定沒了 → 重召
W.slot = 0
n = run_until_cast(s, [], summon.LOST_GRACE + 2.0)
check("槽歸零撐過 LOST_GRACE 才重召（跳圖暫態不白放）", n == 1,
      f"重召 {n} 次")

# ── 情境 2：噬魂怪Ⅰ —— 原本就好的不能弄壞 ──────────────────────────────
W2 = World()
W.__dict__.update(W2.__dict__)                  # 重置世界
s = summon.AutoSummon()
s.adopt(781, page=0)
s.arm()
Clock.t += 0.5
s.step(SC, MV, 0xDEAD, [])
soul = pet(7001, 5014, "噬魂怪Ⅰ", 100.5, 200.5)
Clock.t += 1.5
s.step(SC, MV, 0xDEAD, [soul])
check("噬魂怪Ⅰ照常認養（tid=5014）", s.alive and len(W.casts) == 1)

# ── 情境 3：表查不到（舊資料檔／改版新技能）→ 名字「包含」退路 ─────────
W.__dict__.update(World().__dict__)
s = summon.AutoSummon()
s.adopt(150, page=0)
s.expect_tid = None                             # 模擬 summon_of 查不到
s.arm()
Clock.t += 0.5
s.step(SC, MV, 0xDEAD, [])
vamp = pet(9101, 222, "死亡吸血鬼Ⅲ", 100.5, 200.5)
Clock.t += 1.5
s.step(SC, MV, 0xDEAD, [vamp])
check("表缺了退路也認得（『死亡吸血鬼Ⅲ』⊇『吸血鬼Ⅲ』）",
      s.alive and len(W.casts) == 1)

# ── 情境 4：別人的召喚物（種類不對）不准認 ─────────────────────────────
W.__dict__.update(World().__dict__)
s = summon.AutoSummon()
s.adopt(150, page=0)
s.arm()
Clock.t += 0.5
s.step(SC, MV, 0xDEAD, [])
stranger = pet(9201, 999, "火焰魔像", 100.5, 200.5)   # 站我施放格也不行
Clock.t += 1.5
s.step(SC, MV, 0xDEAD, [stranger])
check("種類不對不認（tid=999 名字也不含）", not s.alive)
n = run_until_cast(s, [stranger], summon.RETRY + 1.0)
check("認不到照 RETRY 節奏重試", n == 1, f"重試 {n} 次")

# ── 情境 5：同拍舊有召喚物在場 → 槽當場驗證（不等認養） ────────────────
W.__dict__.update(World().__dict__)
s = summon.AutoSummon()
s.adopt(150, page=0)
s.arm()
W.slot = 9301
old = pet(9301, 222, "死亡吸血鬼Ⅲ", 98.0, 199.0)
Clock.t += 0.5
s.step(SC, MV, 0xDEAD, [old])
check("場上舊召喚物＋槽對上 → 立刻 _slot_ok", s._slot_ok)

print()
if FAILS:
    print(f"⛔ {len(FAILS)} 項失敗：", "、".join(FAILS))
    sys.exit(1)
print("✅ 全部通過")
