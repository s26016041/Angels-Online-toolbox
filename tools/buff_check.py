"""自動分身補放離線測試 —— 重演兩次實機翻案（不碰遊戲、不碰 Qt）。

2026-08-20 實機定案（嵐狐 A/B/C 三路對照，reports/lanfox_ab_probe.txt）：
    ⛔ 裸 CAST_FN 施放包對雙體分身Ⅰ(5471) 無效——有廣播、MP 不扣、分身不出現
    ✅ 補放走 quickbar.use（usequickkey，跟真按 F12 同路）
    ⛔「等不到廣播＝被拒收→8s重放」也翻案（無限補發迴圈）→ 等不到＝驗不了＝當已補

驗的規格（buff.py）：
    ① 走快捷鍵路徑補放；廣播看得到 → 100% 確認「伺服器已受理」
    ② 廣播等不到 → 判「驗不了」：當作已補、之後跳過確認 —— 絕不重放
    ③ 沒有監聽 → 按出就當成功
    ④ 換技能（adopt）→ 廣播驗不驗得到重新試
    ⑤ 目前頁那一格不是學到的技能（翻頁）→ 這輪不放，等 adopt 收斂
    ⑥ 快捷欄整個讀不到（改版位移）→ 退回真送鍵保底

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
    active = True


class FakeQuickbar:
    """假快捷欄：F12＝格 11 放著 cell_skill；use() 記次數。"""

    VK_F1 = 0x70
    SLOTS = 12

    def __init__(self):
        self.cell_skill = 5424     # 目前頁 F12 格放的技能（None＝讀不到整頁）
        self.uses = 0

    class Reader:
        def __init__(self, sc):
            self._sc = sc

        def page(self):
            return 0

    def read_page(self, sc, page):
        if self.cell_skill == "unreadable":
            return None
        cells = [None] * 12
        if self.cell_skill:
            cells[11] = types.SimpleNamespace(
                kind=1, value=self.cell_skill, is_skill=True, is_item=False)
        return cells

    def use(self, mover, sc, slot, page):
        self.uses += 1
        return True


CLOCK = Clock()
QB = FakeQuickbar()
KEYS = {"n": 0}
buff.time = CLOCK
buff.skills = types.SimpleNamespace(
    of=lambda sid: (types.SimpleNamespace(id=int(sid), secs=1200.0)
                    if sid else None))
buff.castwatch = types.SimpleNamespace(own_server_id=lambda sc, ent: 42)
buff.bag = types.SimpleNamespace(player_entity=lambda sc: 0x1000)
# quickbar 介面掛在實例上（Reader 是巢狀類別，補一層轉接）
buff.quickbar = types.SimpleNamespace(
    VK_F1=0x70, Reader=lambda sc: FakeQuickbar.Reader(sc),
    read_page=QB.read_page, use=QB.use)

# 假的「身上 buff 樹」（app/game/buffs.py 的 I/O）：
#   TREE["left"] = None → 讀不到（① ~ ⑥ 全部走這條＝退化路徑）
#                = 0.0  → 身上沒有這招
#                > 0    → 身上還剩幾秒
TREE = {"left": None}
buff.buffs = types.SimpleNamespace(left_of=lambda sc, sid: TREE["left"])

SC = object()


def send_key(hwnd, vk):
    KEYS["n"] += 1


def new_buff():
    b = buff.AutoBuff(0x7B, 5424)
    b.arm()
    return b


def step(b, mv, hook):
    return b.step(SC, mv, 0, 1, 1, 0, send_key, cast_hook=hook)


print("① 快捷鍵路徑＋廣播確認")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
step(b, mv, hook)
check("用 quickbar.use 按出（不是封包）", QB.uses == 1 and b._confirming)
hook.hit = True
note = step(b, mv, hook)
check("確認受理", "伺服器已受理" in note, f"實得 {note}")
check("倒數開始", b.left() > 1000)
CLOCK.t += 60
step(b, mv, hook)
check("時間還夠就不再按", QB.uses == 1)

print("② 廣播等不到 → 當作已補、絕不重放")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
u0 = QB.uses
step(b, mv, hook)
check("按了第一下", QB.uses == u0 + 1 and b._confirming)
CLOCK.t += buff.CAST_WAIT + 0.5
note = step(b, mv, hook)
check("判「驗不了」當作已補", "沒接到施放廣播" in note, f"實得 {note}")
check("不再等廣播（_cw_skip）", b._cw_skip is True)
for _ in range(50):                     # 舊版 8 秒就重放：走 100 秒驗迴圈已死
    CLOCK.t += 2.0
    step(b, mv, hook)
check("100 秒內零重放", QB.uses == u0 + 1, f"實得 {QB.uses - u0} 次")
CLOCK.t += 1200.0
note = step(b, mv, hook)
check("到期照補、跳過確認", QB.uses == u0 + 2 and not b._confirming
      and "快捷鍵路徑" in note, f"實得 {note}")

print("③ 沒有監聽 → 按出就當成功")
b, mv = new_buff(), FakeMover()
u0 = QB.uses
note = step(b, mv, None)
check("一步到位", QB.uses == u0 + 1 and not b._confirming
      and "快捷鍵路徑" in note, f"實得 {note}")

print("④ 換技能 → 重新試廣播")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
step(b, mv, hook)
CLOCK.t += buff.CAST_WAIT + 0.5
step(b, mv, hook)
check("這招已標驗不了", b._cw_skip is True)
b.adopt(9999)
check("adopt 後重置", b._cw_skip is False)

print("⑤ F12 換了另一招（設定檔舊編號的實機根因）→ 當場收下、照放")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
check("起點是設定檔帶進來的舊編號", b.skill == 5424)
QB.cell_skill = 5471                    # 實機：設定 5424、F12 其實是 5471
u0 = QB.uses
step(b, mv, hook)
check("改放 F12 現在那一招", b.skill == 5471, f"實得 {b.skill}")
check("有按出去（不是卡住）", QB.uses == u0 + 1)

print("⑤b F12 是空格／物品 → 不亂按")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
QB.cell_skill = None                    # 空格
u0 = QB.uses
note = step(b, mv, hook)
check("不按、說清楚", QB.uses == u0 and "沒有技能" in note, f"實得 {note}")
QB.cell_skill = 5424

print("⑥ 快捷欄讀不到 → 退回真送鍵保底")
b, mv, hook = new_buff(), FakeMover(), FakeHook()
QB.cell_skill = "unreadable"
u0, k0 = QB.uses, KEYS["n"]
step(b, mv, hook)
check("改送真按鍵", KEYS["n"] == k0 + 1 and QB.uses == u0)
QB.cell_skill = 5424

print("⑦ 看身上真正的剩餘時間（使用者 2026-08-22：剩 < 20 秒或沒有才放）")
TREE["left"] = 25.0                     # 剩 25 秒 > LEAD(20) → 不該放
b, mv, hook = new_buff(), FakeMover(), FakeHook()
u0 = QB.uses
step(b, mv, hook)
check("剩 25 秒（> 20）→ 不放", QB.uses == u0, f"實得按了 {QB.uses - u0} 次")
check("開始時也不無腦放一次了", not b._confirming)
TREE["left"] = 15.0                     # 剩 15 秒 < LEAD → 要放
b._forget_live()
step(b, mv, hook)
check("剩 15 秒（< 20）→ 放", QB.uses == u0 + 1)
TREE["left"] = 0.0                      # 身上沒有 → 要放
b, mv, hook = new_buff(), FakeMover(), FakeHook()
u0 = QB.uses
step(b, mv, hook)
check("身上沒有 → 放", QB.uses == u0 + 1)

print("⑧ 送出後身上真的多了那個 buff → 確認成功（比廣播硬）")
TREE["left"] = 0.0
b, mv, hook = new_buff(), FakeMover(), FakeHook()   # hook.hit 一直是 False
step(b, mv, hook)
check("先進確認中", b._confirming)
TREE["left"] = 1200.0                   # 伺服器生效了
b._forget_live()
note = step(b, mv, hook)
check("身上看到就算成功（不必等廣播）", "身上確認" in note, f"實得 {note}")
u0 = QB.uses
CLOCK.t += 60
step(b, mv, hook)
check("之後時間還夠就不再按", QB.uses == u0)

print("⑨ 送出後身上一直沒有 → 重試；連 3 次 → 退回自記時間，不無限重放")
TREE["left"] = 0.0                      # 放了也不會出現（鍵對不上的假想改版）
b, mv, hook = new_buff(), FakeMover(), FakeHook()
u0 = QB.uses
for _ in range(3):
    step(b, mv, hook)                   # 送出
    CLOCK.t += buff.CAST_WAIT + 0.5     # 等確認超時
    note = step(b, mv, hook)
    CLOCK.t += buff.RETRY + 0.5         # 等重試冷卻
check("三次都送出去了", QB.uses == u0 + 3, f"實得 {QB.uses - u0} 次")
check("第三次退回自記時間", b._tree_skip is True and "改用自己記時間" in note,
      f"實得 {note}")
sent = QB.uses
for _ in range(60):                     # 走 120 秒（持續 1200 秒還沒到期）
    CLOCK.t += 2.0
    step(b, mv, hook)
check("之後零重放（無限補發迴圈沒回來）", QB.uses == sent,
      f"實得又送了 {QB.uses - sent} 次")
TREE["left"] = None                     # 收乾淨，別影響之後的測試

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
