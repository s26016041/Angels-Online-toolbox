"""自動分身補放確認離線測試 —— 重演 2026-08-20 實機回報的「無法分身、
一直重複補發」（不碰遊戲、不碰 Qt）。

事故：8/19 版（326e142）把「等不到施放廣播」當「被拒收」→ 每 8 秒重放。
分身這種自我 buff 會不會廣播從沒驗過，等不到就重放＝無限補發迴圈
（同類技能重放會被伺服器拒收，白繞）。

驗修法（buff.py）：
    ① 廣播看得到 → 照樣 100% 確認「伺服器已受理」
    ② 廣播等不到 → 判「驗不了」：當作已補、這招之後跳過確認（_cw_skip）
      —— **絕不再重放**，20 分鐘後的下一次補放也不再白等
    ③ 沒有監聽 → 維持舊行為（送出就當成功）
    ④ 換技能（adopt）→ 廣播驗不驗得到重新試

用法：py tools\\buff_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import buff                     # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# --- 假遊戲層（patch 進 buff 模組的命名空間）---------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class FakeHook:
    def __init__(self):
        self.active = True
        self.hit = False           # fired() 的答案（有沒有看到自己的廣播）
        self.n = 0

    def write_count(self):
        return self.n

    def fired(self, since, srv, skill):
        return self.hit


class FakeMover:
    def __init__(self):
        self.active = True
        self.sends = 0

    def call(self, *a):
        self.sends += 1
        return True


CLOCK = Clock()
buff.time = CLOCK
buff.skills = types.SimpleNamespace(
    of=lambda sid: (types.SimpleNamespace(id=int(sid), secs=1200.0)
                    if sid else None))
buff.castwatch = types.SimpleNamespace(own_server_id=lambda sc, ent: 42)
buff.bag = types.SimpleNamespace(player_entity=lambda sc: 0x1000)

SC = object()
NOKEY = lambda hwnd, vk: None                                  # noqa: E731


def new_buff():
    b = buff.AutoBuff(0x7B, 5424)
    b.arm()
    return b


def step(b, mv, hook):
    return b.step(SC, mv, 0, 1, 1, 0, NOKEY, cast_hook=hook)


print("① 廣播看得到 → 100% 確認受理")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
step(b, mv, hook)
check("送了第一包、進入等待", mv.sends == 1 and b._confirming)
hook.hit = True
note = step(b, mv, hook)
check("確認受理", "伺服器已受理" in note, f"實得 {note}")
check("倒數開始", b.left() > 1000)
CLOCK.t += 60
step(b, mv, hook)
check("時間還夠就不再送", mv.sends == 1)

print("② 廣播等不到（分身實機情境）→ 當作已補、絕不重放")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
step(b, mv, hook)
check("送了第一包", mv.sends == 1 and b._confirming)
CLOCK.t += buff.CAST_WAIT + 0.5
note = step(b, mv, hook)
check("判「驗不了」當作已補", "沒看到施放廣播" in note, f"實得 {note}")
check("不再等廣播（_cw_skip）", b._cw_skip is True)
check("倒數照走", b.left() > 1000)
for _ in range(50):                     # 舊版 8 秒就重放：走 100 秒驗迴圈已死
    CLOCK.t += 2.0
    step(b, mv, hook)
check("100 秒內零重放（無限補發迴圈已死）", mv.sends == 1,
      f"實得 {mv.sends} 次")
check("狀態列沒有「重放」字樣", "重放" not in b.note, f"實得 {b.note}")
CLOCK.t += 1200.0                       # 到期 → 下一次正常補放
note = step(b, mv, hook)
check("到期照補", mv.sends == 2)
check("這招直接跳過確認（不再白等 4 秒）",
      not b._confirming and "已用封包補分身" in note, f"實得 {note}")

print("③ 沒有監聽 → 舊行為（送出就當成功）")
b, mv = new_buff(), FakeMover()
note = step(b, mv, None)
check("一步到位", mv.sends == 1 and not b._confirming
      and "已用封包補分身" in note, f"實得 {note}")

print("④ 換技能 → 廣播驗不驗得到重新試")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
step(b, mv, hook)
CLOCK.t += buff.CAST_WAIT + 0.5
step(b, mv, hook)
check("這招已標驗不了", b._cw_skip is True)
b.adopt(9999)
check("adopt 後重置", b._cw_skip is False)

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
