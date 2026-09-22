"""「空的技能鍵＝普攻」離線測試 —— 驗 `KeyWorker` 與 `quickbar.look` 的規格。

使用者 2026-09-20 原話：「如果技能鍵選擇空的技能就是用普攻的意思」、
「一樣可以跟近戰一樣走到那個範圍，然後讓官方自己處理後面」。

驗的規格：
    ① `quickbar.look()` 分得出**空格**與**物品格**（舊的 `skills()` 兩種都只是
       「不在結果裡」，拿它當空格會把放藥水的鍵也變成普攻）
    ② 勾到的空格**進循環**；放物品的格照舊不進（⛔ 不准誤按把藥吃掉）
    ③ 輪到空格 → 叫 `attack.basic`（官方左鍵點怪 TryAct(eid,1)），
       ⛔ 不是 `quickbar.use`、也不是自送施放封包
    ④ 距離照近戰那把尺：> BASIC_REACH 不送；交棒那一輪（client_walk）不擋
    ⑤ 走位：`min_range` 把空格算成近戰 1 格（不然只勾空格時會站在 12 格外發呆）；
       `in_range_of_any` 在 2 格內回 True
    ⑥ 只勾空格也算「有得打」（`has_attack`）—— 寫目標那條才不會去寫血量
    ⑦ 技能鍵與空格混勾：技能照原路（quickbar.use），空格走普攻

用法：py tools\\basic_attack_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import quickbar                       # noqa: E402
from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []
F1 = quickbar.VK_F1
F2 = F1 + 1
F3 = F1 + 2
SID = 743                              # 幻影刺殺Ⅳ（射程 12 → 走封包那條）
MELEE_SID = 0                          # 用不到：③ 只勾空格


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# ── ① quickbar.look 分得出空格／物品格 ───────────────────────────────
print("① quickbar.look()：空格 vs 物品格")


class FakeSc:
    def _read_bytes(self, addr, n):      # quickbar.Reader 找 Lua 全域節點會先讀記憶體；假的一律讀不到
        return None


CELLS = [None] * quickbar.SLOTS
CELLS[0] = quickbar.QuickSlot(kind=quickbar.KIND_SKILL, value=SID, value2=0)
CELLS[2] = quickbar.QuickSlot(kind=quickbar.KIND_ITEM, value=1905, value2=0)
# F2 留 None ＝ 空格

quickbar.read_page = lambda sc, page: CELLS
rd = quickbar.Reader(FakeSc())
rd.page = lambda: 0
rd.page_or_none = lambda: 0        # look() 出手前問的是這個（讀不到 → None）
got = rd.look([F1, F2, F3])
check("讀得到", got is not None)
skl, blank = got
check("F1 是技能", skl.get(F1) == SID, str(skl))
check("★ F2 是空格", blank == {F2}, str(blank))
check("★ F3 放物品 → 不是空格、也不是技能",
      F3 not in blank and F3 not in skl, f"{skl} {blank}")
check("skills() 舊介面不受影響", rd.skills([F1, F2, F3]) == {F1: SID})

# ── 出手執行緒 ──────────────────────────────────────────────────────
CALLS: list[tuple] = []


def make_worker(vks, skills_map, empties, dist=1.0, client_walk=False):
    """一個只用來跑 step() 的 KeyWorker（不啟動執行緒、不碰記憶體）。"""
    kw = farm_tab.KeyWorker(0, FakeSc())
    kw.vks = list(vks)
    kw.skills = dict(skills_map)
    kw.empties = set(empties)
    kw.qb_ok = True
    kw.mover = object()
    kw.eid = 0x1234
    kw.ent_addr = 0x50000
    kw.pos = kw.pos_f = (10.0, 10.0)
    kw.player = 0x40000
    kw.client_walk = client_walk
    kw.set_on(True)
    kw._sel = kw.eid                       # 選定已送過（別在測裡再送一次）
    kw._next_qb = float("inf")             # ⚠ 不要在 step() 裡重讀快捷欄
    kw._next_round = 0.0
    kw._dist = dist
    return kw


def run(kw) -> None:
    CALLS.clear()
    farm_tab.quickbar.self_entity_ok = lambda sc: True
    farm_tab.attack.select = lambda mover, eid: True
    farm_tab.move.pathfinder_this = lambda sc: 0x30000
    # 距離：目標在 (10,10)，把玩家放到「剛好差 _dist 格」的位置
    farm_tab.entity.read_pos = lambda sc, ent: (10.0 - kw._dist, 10.0)
    farm_tab.attack.basic = lambda mover, sc, ent: (
        CALLS.append(("basic", ent)) or True)
    farm_tab.quickbar.use = lambda mover, sc, slot, page: (
        CALLS.append(("quickkey", slot)) or True)
    farm_tab.attack.cast_skill = lambda mover, pf, sid, ent=0, *a: (
        CALLS.append(("cast", sid)) or True)
    farm_tab._send_scan = lambda hwnd, vk: CALLS.append(("sendkey", vk))
    kw.step()


print("② 空格進循環、物品格不進")
kw = make_worker([F1, F2, F3], {F1: SID}, {F2})
run(kw)
kinds = [c[0] for c in CALLS]
check("★ 空格 F2 打了普攻", ("basic", 0x50000) in CALLS, str(CALLS))
check("★ 放物品的 F3 一下都沒動",
      kinds.count("basic") == 1 and ("quickkey", 2) not in CALLS, str(CALLS))
check("⛔ 沒有退回送鍵", "sendkey" not in kinds, str(CALLS))

print("③ 只勾空格 → 整輪都是普攻（⛔ 不是快捷鍵、不是施放封包）")
kw = make_worker([F2], {}, {F2})
run(kw)
check("★ 叫了 attack.basic", CALLS == [("basic", 0x50000)], str(CALLS))
check("★ 帶的是目標**實體位址**（eid 由 click_object 當場重讀）",
      CALLS and CALLS[0][1] == 0x50000, str(CALLS))

print("④ 距離：照近戰那把尺（BASIC_REACH）")
kw = make_worker([F2], {}, {F2}, dist=farm_tab.BASIC_REACH + 0.5)
run(kw)
check("★ 超出近戰距離就不送", CALLS == [], str(CALLS))
kw = make_worker([F2], {}, {F2}, dist=farm_tab.BASIC_REACH - 0.1)
run(kw)
check("進到距離內就送", ("basic", 0x50000) in CALLS, str(CALLS))
kw = make_worker([F2], {}, {F2}, dist=9.0, client_walk=True)
run(kw)
check("★ 交棒那一輪不擋（官方自己走過去）",
      ("basic", 0x50000) in CALLS, str(CALLS))

print("⑤ 走位：空格算成近戰 1 格")
kw = make_worker([F2], {}, {F2})
check("★ min_range = BASIC_RANGE", kw.min_range == farm_tab.BASIC_RANGE,
      str(kw.min_range))
check("　2 格內算打得到", kw.in_range_of_any(farm_tab.BASIC_REACH))
check("★ 3 格外算打不到（要走近）",
      not kw.in_range_of_any(farm_tab.BASIC_REACH + 1.0))
kw2 = make_worker([F1, F2], {F1: SID}, {F2})
check("混勾時 min_range 取最短（普攻 1 格）",
      kw2.min_range == farm_tab.BASIC_RANGE, str(kw2.min_range))
check("　射程 12 的那招在 10 格照樣算打得到", kw2.in_range_of_any(10.0))

print("⑥ 只勾空格也算「有得打」（寫目標那條才不會去寫血量）")
kw = make_worker([F2], {}, {F2})
check("★ has_attack", kw.has_attack is True)
check("　沒技能也沒空格 → False",
      make_worker([F3], {}, set()).has_attack is False)

print("⑦ 技能鍵與空格混勾：各走各的路")
kw = make_worker([F1, F2], {F1: SID}, {F2}, dist=1.0)
run(kw)
check("★ 技能走原本那條（射程 12 → 施放函式）", ("cast", SID) in CALLS,
      str(CALLS))
check("★ 空格走普攻", ("basic", 0x50000) in CALLS, str(CALLS))

print()
if FAILS:
    print(f"FAIL {len(FAILS)} 項：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
