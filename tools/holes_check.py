"""自動打孔：挑錘子／挑寶石的規則＋狀態機每一發都驗結果 —— 離線測試。

    py tools\\holes_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

規則出處見 `app/game/holes.py` 檔頭。這裡把 I/O（讀記憶體、送封包、時鐘）換成
假的，**判斷邏輯跑真的**（[[test-via-button]]：替身只換 I/O，不換邏輯）。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import bag, enhance, gear, holes                 # noqa: E402

# 真的那三支先留一份 —— 下面狀態機測試會把模組層的換成假的
REAL_HAMMERS, REAL_GEMS, REAL_JEWEL = holes.hammers, holes.gems, holes.jewel_levels

FAILS: list[str] = []


def check(name: str, ok: bool, got: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {name}" + (f"　—— {got}" if not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 假的 I/O 層
# ---------------------------------------------------------------------------
GEAR_SLOT = 50
SERIAL = 0xABCD1234

NAMES = {
    14636: "13星打孔錘", 14634: "11星打孔錘", 1202: "1星打孔錘",
    12230: "13星祝福打孔錘", 6905: "1星祝福打孔錘", 7295: "完美威猛打孔禮盒",
    3092: "瑕疵的紅寶石", 3105: "完美的黃寶石", 2862: "60級寶石扭蛋",
    9999: "5星打孔錘",
}


class FakeNames:
    @staticmethod
    def of(type_id: int) -> str:
        return NAMES.get(int(type_id), "")


holes.itemname = FakeNames
bag.itemname = FakeNames


def item(slot: int, type_id: int, kind: int, level: int = 0, param1: int = 0,
         count: int = 1) -> bag.Item:
    return bag.Item(slot=slot, serial=slot * 7, stamp=0, type_id=type_id,
                    count=count, dura=0, kind=kind, price=0, grade=0,
                    dura_max=0, level=level, param1=param1)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


CLOCK = Clock()
holes.time = CLOCK

STRIKES: list[tuple[int, int]] = []


def _strike(mover, item_slot: int, gear_slot: int) -> bool:
    STRIKES.append((item_slot, gear_slot))
    return True


enhance.strike = _strike


class FakeGear:
    def __init__(self, holes_: int, gems: tuple, level: int | None = 120,
                 serial: int = SERIAL) -> None:
        self.serial = serial
        self.holes = holes_
        self.gems = tuple(gems) + (0,) * (5 - len(gems))
        self.base = {} if level is None else {"level": level}


WORLD: dict = {}


def set_world(state: str = gear.READ_OK, holes_: int = 2,
              gems: tuple = (3115, 3100), level: int | None = 120,
              serial: int = SERIAL, hammers=None, gems_in_bag=None,
              complete: bool = True, counts: dict | None = None) -> None:
    """決定這一拍 gear.read_state／holes.hammers／holes.gems／holes.count_of 回什麼。"""
    if hammers is None:
        hammers = [holes.Hammer(52, 14636, "13星打孔錘", 13, 3)]
    if gems_in_bag is None:
        gems_in_bag = [holes.Gem(21, 3092, "瑕疵的紅寶石", 2, 20, 450, 5)]
    if counts is None:
        counts = {14636: 3, 3092: 5}
    WORLD.update(state=state, gear=FakeGear(holes_, gems, level, serial),
                 hammers=hammers, gems=gems_in_bag, complete=complete,
                 counts=counts)


def _read_state(sc, slot):
    if WORLD["state"] == gear.READ_OK:
        return WORLD["gear"], gear.READ_OK
    return None, WORLD["state"]


gear.read_state = _read_state
holes.hammers = lambda sc, items=None, complete=True: (WORLD["hammers"], WORLD["complete"])
holes.gems = lambda sc, items=None, complete=True: (WORLD["gems"], WORLD["complete"])
holes.count_of = lambda sc, tid: (WORLD["counts"].get(tid, 0), WORLD["complete"])


def fresh(target: int = 4, cap: int = 40) -> holes.Run:
    STRIKES.clear()
    return holes.Run(None, None, GEAR_SLOT, SERIAL, target, cap, "測試戰靴")


def kinds(evs) -> list[str]:
    return [e.kind for e in evs]


# ---------------------------------------------------------------------------
print(__doc__.splitlines()[0])
print()
print("① 挑錘子：只認一般 N星打孔錘，星級＝物品等級//10，祝福／對不上的都不用")

real_hammers, real_gems, real_jewel_levels = REAL_HAMMERS, REAL_GEMS, REAL_JEWEL

items = [
    item(52, 14636, holes.KIND_HAMMER, level=130),          # 13星
    item(53, 14634, holes.KIND_HAMMER, level=110, count=2),  # 11星
    item(54, 1202, holes.KIND_HAMMER, level=11),            # 1星
    item(55, 12230, holes.KIND_HAMMER, level=131, param1=1),  # 13星祝福（動態資料1=1）
    item(56, 6905, holes.KIND_HAMMER, level=11),            # 1星祝福（只靠名字擋）
    item(57, 7295, 40, level=0),                            # 禮盒：分類不對
    item(58, 9999, holes.KIND_HAMMER, level=130),           # 名字 5星、等級 13星 → 不猜
]
hs, complete = real_hammers(None, items, True)
check("三支一般錘都認得、星級由低到高",
      [(h.star, h.type_id) for h in hs] == [(1, 1202), (11, 14634), (13, 14636)],
      f"實得 {[(h.star, h.type_id) for h in hs]}")
check("祝福錘（動態資料1=1）不用", all(h.type_id != 12230 for h in hs))
check("祝福錘（只有名字看得出）不用", all(h.type_id != 6905 for h in hs))
check("名字星數跟物品等級對不上 → 不用", all(h.type_id != 9999 for h in hs))
check("數量帶出來", next(h.count for h in hs if h.type_id == 14634) == 2)

h = holes.pick_hammer(hs, 120)
check("120 級裝備 → 要 12 星以上 → 挑 13星（11星不夠）", h and h.star == 13,
      f"實得 {h}")
h = holes.pick_hammer(hs, 119)
check("119 級 → 11星就夠（挑最低的夠用星級）", h and h.star == 11, f"實得 {h}")
h = holes.pick_hammer(hs, 139)
check("139 級 → 13星剛好夠", h and h.star == 13, f"實得 {h}")
h = holes.pick_hammer(hs, 140)
check("140 級 → 沒有夠的錘子 → None", h is None, f"實得 {h}")
h = holes.pick_hammer(hs, 0)
check("0 級 → 最低星級那支", h and h.star == 1, f"實得 {h}")

print()
print("② 寶石效果表：+0x6C 最低等級、+0x70 等級上限，讀不到／不合理不用")

TABLE, ROW = 0x31000000, 0x03B40000


class FakeScanner:
    def __init__(self, reads: dict) -> None:
        self.reads = reads

    def _read_bytes(self, addr: int, n: int):
        return self.reads.get((addr, n))


def scanner_for(lo: int, hi: int, effect: int = 15) -> FakeScanner:
    return FakeScanner({
        (gear.JEWEL_TABLE_PTR, 4): struct.pack("<I", TABLE),
        (TABLE + effect * 4, 4): struct.pack("<I", ROW),
        (ROW + holes.JEWEL_MIN_LEVEL, 8): struct.pack("<ii", lo, hi),
    })


check("row[15] = (80, 450)", real_jewel_levels(scanner_for(80, 450), 15) == (80, 450))
check("垃圾值（5000, 1）→ None", real_jewel_levels(scanner_for(5000, 1), 15) is None)
check("表讀不到 → None", real_jewel_levels(FakeScanner({}), 15) is None)
check("效果編號 0 → None", real_jewel_levels(scanner_for(80, 450), 0) is None)

gitems = [
    item(21, 3105, holes.KIND_GEM, param1=15),              # 完美的黃寶石 → 80
    item(22, 3092, holes.KIND_GEM, param1=2, count=5),      # 瑕疵的紅寶石 → 20
    item(74, 2862, 33, level=60, param1=29672),             # 扭蛋：分類不對
    item(23, 3105, holes.KIND_GEM, param1=0),               # 沒效果編號
]
sc = FakeScanner({
    (gear.JEWEL_TABLE_PTR, 4): struct.pack("<I", TABLE),
    (TABLE + 15 * 4, 4): struct.pack("<I", ROW),
    (ROW + holes.JEWEL_MIN_LEVEL, 8): struct.pack("<ii", 80, 450),
    (TABLE + 2 * 4, 4): struct.pack("<I", ROW + 0x100),
    (ROW + 0x100 + holes.JEWEL_MIN_LEVEL, 8): struct.pack("<ii", 20, 450),
})
gs, _ = real_gems(sc, gitems, True)
check("兩顆寶石認得、等限由低到高",
      [(g.min_level, g.type_id) for g in gs] == [(20, 3092), (80, 3105)],
      f"實得 {[(g.min_level, g.type_id) for g in gs]}")
check("扭蛋（分類 33）不算寶石", all(g.slot != 74 for g in gs))
check("沒效果編號的不用", all(g.slot != 23 for g in gs))

print("②b 寶石加什麼（gem_effects）：範本 +0x108 → 效果列三組 × 分類欄 → 寶石範本那一欄")
ITABLE, TMPL = 0x32000000, 0x04C00000
GRP = gear.GEM_GROUPS


def effects_scanner(armor_codes=(14,) * 7, groups2=()) -> FakeScanner:
    reads = {
        (gear.ITEM_TABLE_PTR, 4): struct.pack("<I", ITABLE),
        (ITABLE + 3105 * 4, 4): struct.pack("<I", TMPL),
        (TMPL + bag.TMPL_PARAM1, 4): struct.pack("<I", 15),
        (gear.JEWEL_TABLE_PTR, 4): struct.pack("<I", TABLE),
        (TABLE + 15 * 4, 4): struct.pack("<I", ROW),
        # 完美的黃寶石：武器 雷電攻擊(18)、防具 靈敏(14)、盾 雷電防禦(22)；第二、三組 0
        (ROW + GRP[0] + 0x00, 4): struct.pack("<I", 18),
        (ROW + GRP[0] + 0x20, 4): struct.pack("<I", 22),
        (TMPL + 0x88, 4): struct.pack("<i", 24),          # 雷電攻擊
        (TMPL + 0x84, 4): struct.pack("<i", 10),          # 靈敏
        (TMPL + 0x8C, 4): struct.pack("<i", 5),           # 雷電防禦
        (TMPL + 0x74, 4): struct.pack("<i", 3),           # 防禦（給「七欄不一樣」那題）
    }
    for off, code in zip((0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C), armor_codes):
        reads[(ROW + GRP[0] + off, 4)] = struct.pack("<I", code)
    for grp_i, off, code in groups2:
        reads[(ROW + GRP[grp_i] + off, 4)] = struct.pack("<I", code)
    return FakeScanner(reads)


eff = holes.gem_effects(effects_scanner(), 3105)
check("★ 完美的黃寶石 ＝ 武器：雷電攻擊 +24／防具：靈敏 +10／盾牌：雷電防禦 +5（跟資源包說明一樣）",
      eff == [("武器", "雷電攻擊", 24), ("防具", "靈敏", 10), ("盾牌", "雷電防禦", 5)], str(eff))
eff = holes.gem_effects(effects_scanner(armor_codes=(14, 10, 14, 14, 14, 14, 14)), 3105)
check("　防具七欄不一樣 → 逐欄列（衣服：防禦力 +3），不合寫",
      ("衣服", "防禦力", 3) in eff and ("頭飾", "靈敏", 10) in eff
      and not any(x[0] == "防具" for x in eff), str(eff))
eff = holes.gem_effects(effects_scanner(groups2=((1, 0x00, 11),)), 3105)
check("　第二組效果也列（武器再加魔攻，範本沒那欄＝0）",
      ("武器", "魔攻", 0) in eff and eff[0] == ("武器", "雷電攻擊", 24), str(eff))
check("　範本讀不到 → 空清單（介面不印、不猜）", holes.gem_effects(FakeScanner({}), 3105) == [])

print("②c 說明原文表（itemdesc，資源包文字3）")
from app.game import itemdesc                                       # noqa: E402
check("表載得到（> 3 萬筆）", itemdesc.count() > 30000, str(itemdesc.count()))
d = itemdesc.lines(3105)
check("完美的黃寶石的原文＝可鑲嵌…／武器：雷電攻擊 +24／防具：靈敏 +10／盾牌：雷電防禦 +5／裝備等限：80級",
      d == ["可鑲嵌於已打孔之裝備上，以加強能力。", "武器：雷電攻擊 +24", "防具：靈敏 +10",
            "盾牌：雷電防禦 +5", "裝備等限：80級"], str(d))
check("記憶體算的三行每一項都在原文裡（表跟記憶體對得上）",
      all(f"{a} {v:+d}" in "\n".join(d) for _l, a, v in holes.gem_effects(effects_scanner(), 3105)))
check("查不到 → 空", itemdesc.of(99999999) == "" and itemdesc.lines(99999999) == [])

g = holes.pick_gem(gs, 120, 40)
check("等限 ≤ 40、裝備 120 級 → 挑瑕疵（20）", g and g.type_id == 3092, f"實得 {g}")
g = holes.pick_gem(gs, 120, 10)
check("等限上限 10 → 沒有一顆合格 → None", g is None, f"實得 {g}")
g = holes.pick_gem(gs, 10, 100)
check("裝備 10 級 < 寶石最低 20 → 鑲不進去 → None", g is None, f"實得 {g}")
g = holes.pick_gem(gs, 500, 100)
check("裝備 500 級 > 等級上限 450 → None", g is None, f"實得 {g}")
g = holes.pick_gem(gs, 120, 100)
check("等限 100 時仍挑最便宜的（20 不是 80）", g and g.min_level == 20, f"實得 {g}")
g = holes.pick_gem(gs, 120, 10, gem_type=3105)
check("指定寶石 3105 → 不看等限上限、只用它", g and g.type_id == 3105, f"實得 {g}")
g = holes.pick_gem(gs, 50, 100, gem_type=3105)
check("指定寶石但裝備 50 級 < 它的最低 80 → None", g is None, f"實得 {g}")
g = holes.pick_gem(gs, 120, 100, gem_type=4242)
check("指定的寶石不在背包 → None（不拿別種頂替）", g is None, f"實得 {g}")
check("寶石帶圖示編號", all(hasattr(x, "icon_id") for x in gs))

print()
print("③ 快樂路徑：2 孔鑲滿 → 打孔 → 鑲 → 打孔 → 到 4 孔停，最後一孔留空")

r = fresh(target=4, cap=40)
set_world(holes_=2, gems=(3115, 3100))
evs = r.tick()
check("沒空孔 → 第一發是打孔（錘子格 52 → 裝備格 50）",
      STRIKES == [(52, 50)] and evs == [], f"STRIKES={STRIKES} evs={kinds(evs)}")
set_world(holes_=3, gems=(3115, 3100))
CLOCK.t += 0.3
evs = r.tick()
check("孔數 2→3 → 打孔成功", kinds(evs) == [enhance.SUCCESS] and "打孔成功" in evs[0].text,
      f"實得 {kinds(evs)}")
evs = r.tick()
check("第 3 孔空著 → 下一發是鑲嵌（寶石格 21）",
      STRIKES[-1] == (21, 50) and evs == [], f"STRIKES={STRIKES}")
set_world(holes_=3, gems=(3115, 3100, 3092))
CLOCK.t += 0.3
evs = r.tick()
check("第 3 孔有東西了 → 鑲嵌成功", kinds(evs) == [enhance.SUCCESS] and "第 3 孔" in evs[0].text,
      f"實得 {[e.text for e in evs]}")
evs = r.tick()
check("鑲滿 → 再打孔", STRIKES[-1] == (52, 50) and len(STRIKES) == 3, f"STRIKES={STRIKES}")
set_world(holes_=4, gems=(3115, 3100, 3092))
CLOCK.t += 0.3
evs = r.tick()
check("到 4 孔 → SUCCESS + DONE，停",
      kinds(evs) == [enhance.SUCCESS, enhance.DONE] and r.done, f"實得 {kinds(evs)}")
check("最後一孔沒有再鑲（總共只送 3 發）", len(STRIKES) == 3, f"送了 {len(STRIKES)} 發")
check("DONE 訊息說最後一孔留空", "留空" in evs[1].text, evs[1].text)

print()
print("④ 有空孔就先鑲，不打孔")

r = fresh(target=3)
set_world(holes_=1, gems=(0,))
r.tick()
check("第一發是鑲嵌不是打孔", STRIKES == [(21, 50)], f"STRIKES={STRIKES}")

print()
print("④b 指定寶石：只鑲那一種；沒了就停，不拿別種頂替")

r = holes.Run(None, None, GEAR_SLOT, SERIAL, 3, 40, "測試戰靴", gem_type=3105)
STRIKES.clear()
set_world(holes_=1, gems=(0,), gems_in_bag=[
    holes.Gem(21, 3092, "瑕疵的紅寶石", 2, 20, 450, 5),
    holes.Gem(30, 3105, "完美的黃寶石", 15, 80, 450, 1)])
r.tick()
check("指定 3105 → 鑲的是格 30 那顆（不是便宜的格 21）", STRIKES == [(30, 50)],
      f"STRIKES={STRIKES}")

r = holes.Run(None, None, GEAR_SLOT, SERIAL, 3, 40, "測試戰靴", gem_type=3105)
STRIKES.clear()
set_world(holes_=1, gems=(0,), gems_in_bag=[
    holes.Gem(21, 3092, "瑕疵的紅寶石", 2, 20, 450, 5)])
evs = r.tick()
check("指定的寶石用完了 → BLOCKED、訊息點名它、一發不送",
      r.done and kinds(evs) == [enhance.BLOCKED] and "完美的黃寶石" in evs[0].text
      and STRIKES == [], f"實得 {[e.text for e in evs]} STRIKES={STRIKES}")

print()
print("⑤ 打孔後那格空了（確定讀到）→ 裝備毀損，停")

r = fresh()
set_world(holes_=2, gems=(3115, 3100))
r.tick()
set_world(state=gear.READ_ABSENT)
CLOCK.t += 0.3
evs = r.tick()
check("absent → GONE「毀損」", kinds(evs) == [enhance.GONE] and "毀損" in evs[0].text and r.done,
      f"實得 {[e.text for e in evs]}")

print()
print("⑥ 送出後讀不到 —— 不准說毀損；等完說驗不了")

r = fresh()
set_world(holes_=2, gems=(3115, 3100))
r.tick()
set_world(state=gear.READ_UNREADABLE)
CLOCK.t += 0.3
evs = r.tick()
check("讀不到第一拍不下結論", evs == [] and not r.done, f"evs={kinds(evs)}")
CLOCK.t += holes.UNREADABLE_SECS + 0.1
evs = r.tick()
check("寬限期到 → UNKNOWN，不說毀損",
      r.done and kinds(evs) == [enhance.UNKNOWN] and "毀損" not in evs[0].text,
      f"實得 {[e.text for e in evs]}")

print()
print("⑦ 逾時沒變：錘子沒少 → 補送（上限）；錘子少了 → 停")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), counts={14636: 3})
r.tick()
for i in range(holes.MAX_RESEND):
    CLOCK.t += holes.WAIT_MS / 1000 + 0.1
    evs = r.tick()
    check(f"第 {i + 1} 次：錘子沒少 → 補送", kinds(evs) == [enhance.UNKNOWN]
          and "補送" in evs[0].text and not r.done, f"實得 {[e.text for e in evs]}")
    r.tick()                                            # 真的再送一發
check("補送真的有送", len(STRIKES) == holes.MAX_RESEND + 1, f"送了 {len(STRIKES)} 發")
CLOCK.t += holes.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("超過上限 → 停", r.done and kinds(evs) == [enhance.UNKNOWN], f"實得 {kinds(evs)}")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), counts={14636: 3})
r.tick()
set_world(holes_=2, gems=(3115, 3100), counts={14636: 2})
CLOCK.t += holes.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("錘子少了卻沒變化 → 停，不再打",
      r.done and kinds(evs) == [enhance.UNKNOWN] and "扣掉" in evs[0].text,
      f"實得 {[e.text for e in evs]}")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), counts={14636: 3})
r.tick()
set_world(holes_=2, gems=(3115, 3100), counts={14636: 2}, complete=False)
CLOCK.t += holes.WAIT_MS / 1000 + 0.1
evs = r.tick()
check("背包沒掃完 → 不拿數量當證據（等）", evs == [] and not r.done and len(STRIKES) == 1,
      f"evs={kinds(evs)} 送了 {len(STRIKES)}")
CLOCK.t += holes.UNREADABLE_SECS + 0.1
evs = r.tick()
check("等完還沒掃完 → 停說驗不了", r.done and kinds(evs) == [enhance.UNKNOWN],
      f"實得 {kinds(evs)}")

print()
print("⑧ 沒有夠星的錘子／沒有合格寶石 → 大聲擋下，一發不送")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), level=140)          # 140 級要 14 星
evs = r.tick()
check("錘子星級不夠 → BLOCKED，訊息說幾星", r.done and kinds(evs) == [enhance.BLOCKED]
      and "14 星" in evs[0].text and STRIKES == [], f"實得 {[e.text for e in evs]}")

r = fresh(cap=10)
set_world(holes_=2, gems=(3115, 0))
evs = r.tick()
check("寶石等限上限 10 → 沒合格寶石 → BLOCKED，訊息提等限",
      r.done and kinds(evs) == [enhance.BLOCKED] and "等限" in evs[0].text and STRIKES == [],
      f"實得 {[e.text for e in evs]}")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), hammers=[], complete=False)
evs = r.tick()
check("沒掃完就沒錘子 → 先等，不下結論", evs == [] and not r.done)
CLOCK.t += holes.UNREADABLE_SECS + 0.1
evs = r.tick()
check("等完還是沒掃完 → BLOCKED 說掃不完整", r.done and "掃不完整" in evs[0].text,
      f"實得 {[e.text for e in evs]}")

print()
print("⑨ 範本讀不到（不知道幾級）→ 不挑錘子、不送")

r = fresh()
set_world(holes_=2, gems=(3115, 3100), level=None)
evs = r.tick()
check("第一拍等", evs == [] and STRIKES == [])
CLOCK.t += holes.UNREADABLE_SECS + 0.1
evs = r.tick()
check("等完停：驗不了、沒動手", r.done and kinds(evs) == [enhance.UNKNOWN]
      and "沒有動手" in evs[0].text and STRIKES == [], f"實得 {[e.text for e in evs]}")

print()
print("⑩ 目標已達／上限／換人")

r = fresh(target=2)
set_world(holes_=2, gems=(3115, 3100))
evs = r.tick()
check("已經 2 孔、目標 2 → DONE 不送", kinds(evs) == [enhance.DONE] and STRIKES == [])

r = fresh(target=5)
set_world(holes_=5, gems=(1, 1, 1, 1, 1))
evs = r.tick()
check("5 孔 → 目標最多 5 → DONE", kinds(evs) == [enhance.DONE] and STRIKES == [],
      f"實得 {kinds(evs)}")
check("目標會被夾在 1~5", holes.Run(None, None, 1, 1, 9, 0).target == 5
      and holes.Run(None, None, 1, 1, 0, 0).target == 1)

r = fresh()
set_world(holes_=2, gems=(3115, 3100))
r.tick()
set_world(holes_=2, gems=(3115, 3100), serial=SERIAL + 1)
CLOCK.t += 0.3
evs = r.tick()
check("serial 變了 → 停、不說毀損",
      r.done and kinds(evs) == [enhance.UNKNOWN] and "毀損" not in evs[0].text,
      f"實得 {[e.text for e in evs]}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過（10 組）")
