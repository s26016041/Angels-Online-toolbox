"""逐分頁體檢：每個分頁**實際會讀的東西**，一項一項讀出來看對不對。

    py tools\\tab_check.py            # 要進遊戲；有幾台就驗幾台

## 為什麼要這支

`selfcheck.py` 那 13 項是**共用底層**（狀態物件／玩家／座標／地圖／背包…），
分頁自己額外讀的東西它一項都沒驗到 —— 快捷欄、技能射程、地形圖、隊伍、
精靈設定、兌換清單、伺服器清單、能量欄位、裝備品質…
2026-08-11 改版就是這樣：selfcheck 13/13 全綠，掛機還是廢的。

所以這支的對照表是「**分頁 → 它會呼叫哪些讀取函式**」，用 AST 掃
`app/tabs/*_tab.py` 的相依關係列出來的，不是憑印象寫的。

## 判定原則

每一項都要有**能分辨對錯的證據**，不是「有回值就算過」：
* 數值要落在合理範圍（等級 1~200、座標在地圖內、血量百分比 0~100…）
* 交叉比對兩個來源（快捷欄的技能 ID 要在技能範本表查得到、
  場上怪的種類 ID 要在怪物範本表查得到、背包物品的種類 ID 要有名字…）
* 讀不到 ≠ 壞掉：本來就可能沒有的東西（沒組隊、沒買賣視窗）標成「—」不算失敗

⚠ 純讀取：不寫記憶體、不呼叫遊戲函式、不送封包。動作層（真的打怪／真的賣）
  不在這支的範圍 —— 那要另外一支，而且會有副作用。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                     # noqa: E402
from app.core.memory import MemoryScanner              # noqa: E402
from app.game import (aob, bag, channel, energy, entity, inventory,  # noqa: E402
                      itemname, items, jumpmap, locate, login, monsters,
                      player, quickbar, robot, scene, skillcost, skills,
                      tablestamp, team, terrain)

REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "reports", "tab_check.txt")

OK, BAD, NA = "✔", "✘", "—"


class Ctx:
    """一台分身的共用讀取結果（各分頁的檢查都吃這個，不重複掃）。"""

    def __init__(self, sc, hwnd, pid, title):
        self.sc, self.hwnd, self.pid, self.title = sc, hwnd, pid, title
        self.state, self.me, self.ents, _r, _e = entity.snapshot(sc)
        self.base = player.locate_fast(sc) or player.locate(sc)
        self.stats = player.read(sc, self.base) if self.base else None
        self.scene = scene.current(sc)
        self.inv = None
        try:
            hits = aob.scan(sc, aob.SKILL_EXP_BALL, limit=4096)
            self.inv = inventory.locate(
                sc, {a - inventory.ITEM_BALL_OFF for a in hits})
        except Exception:                                  # noqa: BLE001
            pass
        self.bag_items = bag.items(sc)
        self.pages = [quickbar.read_page(sc, p) for p in range(quickbar.PAGES)]


# ---------------------------------------------------------------------------
# 各分頁的檢查（回 (狀態, 說明)）
# ---------------------------------------------------------------------------
def c_stats(c):
    s = c.stats
    if not s:
        return BAD, "讀不到角色屬性"
    ok = (1 <= s.level <= 200 and 0 < s.max_hp < 10_000_000
          and 0 <= s.hp <= s.max_hp and 0 <= s.mp <= s.max_mp
          and s.exp_lo <= s.exp <= s.exp_hi and 0 <= s.gold < 10 ** 11)
    return (OK if ok else BAD), (
        f"Lv{s.level} HP{s.hp}/{s.max_hp} MP{s.mp}/{s.max_mp} "
        f"經驗{s.exp - s.exp_lo}/{s.exp_hi - s.exp_lo} 金{s.gold}")


def c_ball(c):
    """經驗球分類表（收益監控靠它認球）。背包裡沒有球就標 —。"""
    balls = [i for i in (c.bag_items or []) if items.is_ball(i.type_id)]
    if not balls:
        return NA, "背包沒有技能經驗球"
    bt = items.ball_type(balls[0].type_id)
    return (OK if bt else BAD), f"{len(balls)} 顆，第一顆 {itemname.of(balls[0].type_id)}"


def c_pos(c):
    if not c.me:
        return BAD, "讀不到玩家物件"
    p = entity.player_pos(c.sc, c.me)
    if not p:
        return BAD, "讀不到座標"
    ok = 0 <= p[0] < 4096 and 0 <= p[1] < 4096
    return (OK if ok else BAD), f"({p[0]:.1f}, {p[1]:.1f}) 走路中={entity.is_walking(c.sc, c.me)}"


def c_scene(c):
    s = c.scene
    if not s:
        return BAD, "讀不到場景"
    ok = 0 < s.id < 10000
    return (OK if ok else BAD), f"{s}　同圖代號={s.key}"


def c_terrain(c):
    grid, why = terrain.load(c.sc)
    if grid is None:
        return BAD, why
    if not c.me:
        return OK, f"{grid.w}x{grid.h}"
    p = entity.player_pos(c.sc, c.me)
    inside = p and 0 <= p[0] < grid.w and 0 <= p[1] < grid.h
    walk = grid.walkable(int(p[0]), int(p[1])) if inside else False
    return ((OK if inside and walk else BAD),
            f"{grid.w}x{grid.h}，自己站的格子可走={walk}")


def c_mobs(c):
    """場上怪 + 怪物範本表交叉比對（種類 ID 要查得到範本）。"""
    idx = monsters.index_base(c.sc)
    if idx is None:
        return BAD, "讀不到怪物範本表"
    mobs = [e for e in (c.ents or []) if e.addr != c.me and e.type_id]
    if not mobs:
        return NA, f"範本表在（{idx:#x}），但附近沒怪 —— 換張有怪的圖再驗一次"
    got = [(e, monsters.info(c.sc, e.type_id, idx)) for e in mobs]
    bad = [e.name for e, i in got if i is None]
    sample = "、".join(f"{e.name}={i}" for e, i in got[:3] if i)
    return ((BAD if bad else OK),
            f"{len(mobs)} 隻；{sample}" + (f"　查不到範本：{bad}" if bad else ""))


def c_target(c):
    if not c.state:
        return BAD, "讀不到狀態物件"
    ok, tid, pct = entity.read_target_checked(c.sc, c.state)
    if not ok:
        return BAD, "狀態物件 vtable 驗不過"
    good = 0 <= pct <= 100
    return ((OK if good else BAD),
            f"目標欄位 +{entity.OFF_TARGET:#x} 讀得到（目標={tid} 血量%={pct}）")


def c_quickbar(c):
    n = sum(1 for pg in c.pages if pg is not None)
    if n != quickbar.PAGES:
        return BAD, f"只有 {n}/{quickbar.PAGES} 頁讀得到（表偏移 {quickbar.TABLE_OFF:#x} 對嗎？）"
    filled = [s for pg in c.pages if pg for s in pg if s]
    if not filled:
        return BAD, "4 頁全空 —— 表偏移多半錯了"
    kinds = {s.kind for s in filled}
    return OK, f"4/4 頁、{len(filled)} 格有東西（型別 {sorted(kinds)}）"


def c_skill_tmpl(c):
    """快捷欄上的技能 → 技能範本表（射程／消耗）+ 射程表交叉比對。"""
    ids = [s.value for pg in c.pages if pg for s in pg if s and s.is_skill]
    if not ids:
        return NA, "快捷欄沒有技能"
    rows = []
    bad = []
    for sid in ids[:12]:
        cost = skillcost.cost(c.sc, sid)
        rng = skills.range_of(sid)
        if cost is None:
            bad.append(sid)
        else:
            rows.append(f"{skills.name_of(sid)}(射程{rng} MP{cost[0]} SP{cost[1]})")
    return ((BAD if bad else OK),
            "、".join(rows[:4]) + (f"　⚠ 查不到範本：{bad}" if bad else ""))


def c_sp(c):
    """⚠ MP 一定要**當場重讀**：`sp_now` 是拿「子物件的 +0x294 等不等於角色屬性
    那份 MP」來認基準的，用 Ctx 裡幾秒前的快照會因為 MP 自然回復而對不上 ——
    量測工具自己把數據弄假（實際踩到，五台裡有一台假失敗）。"""
    if not c.me:
        return BAD, "缺玩家物件"
    st = player.read(c.sc, c.base) if c.base else None
    if not st:
        return BAD, "讀不到角色屬性"
    sp = skillcost.sp_now(c.sc, c.me, st.mp)
    if sp is None:
        return BAD, "讀不到 SP（子物件基準對不上 MP）"
    return OK, f"SP={sp}（對帳 MP={st.mp}）"


def c_bag(c):
    if c.bag_items is None:
        return BAD, "讀不到背包"
    gold = bag.gold(c.sc)
    named = sum(1 for i in c.bag_items if not itemname.of(i.type_id).isdigit())
    ok = len(c.bag_items) > 0 and gold is not None
    return ((OK if ok else BAD),
            f"{len(c.bag_items)} 件、金幣 {gold}、查得到名字 {named}/{len(c.bag_items)}")


def c_gear(c):
    broken = bag.worn_broken(c.sc)
    if broken is None:
        return BAD, "讀不到身上裝備（耐久欄位對嗎？）"
    return OK, (f"身上壞掉 {len(broken)} 件" if broken else "身上沒有壞裝")


def c_robot(c):
    try:
        run = robot.is_run(c.sc)
        auto = robot.get_int(None, c.sc, 1000)
    except Exception as exc:                               # noqa: BLE001
        return BAD, f"{type(exc).__name__}: {exc}"
    return ((OK if auto is not None else BAD),
            f"精靈執行中={run}、設定樹讀得到（自動戰鬥={auto}）")


def c_supply(c):
    if c.inv is None:
        return NA, "找不到物品陣列表頭（背包沒有經驗球時本來就找不到）"
    try:
        dry = robot.potions_out(None, c.sc, c.inv, c.pid)
        recall = robot.has_recall_item(c.sc, c.inv)
    except Exception as exc:                               # noqa: BLE001
        return BAD, f"{type(exc).__name__}: {exc}"
    return OK, (f"見底的藥水 {len(dry)} 種、回程道具="
                f"{'有' if recall else '沒有'}")


def c_channel(c):
    cur = channel.current(c.hwnd)
    srv = channel.server_name(c.hwnd)
    ok = cur is not None and srv
    return (OK if ok else BAD), f"{srv} 分流 {cur}"


def c_servers(c):
    lst = login.servers(c.sc)
    ok = bool(lst) and all(n and 1 <= n_sub <= 8 for n, n_sub in lst)
    return (OK if ok else BAD), "、".join(f"{n}({s})" for n, s in lst) or "讀不到"


def c_chars(c):
    """角色清單（自動登入拿它判斷「第幾格有角色」）。進遊戲後仍讀得到。"""
    names = [login.character(c.sc, i) for i in range(4)]
    got = [n for n in names if n]
    return ((OK if got else NA),
            "、".join(got) if got else "進遊戲後角色清單可能已釋放（不算壞）")


def c_team(c):
    m = team.members(c.sc)
    if m is None:
        return BAD, "讀不到隊伍陣列"
    return OK, (f"{len(m)} 人：" + "、".join(x.name for x in m)) if m else "沒有組隊"


def c_jumpmap(c):
    ent = jumpmap.entries()
    cls = jumpmap.classes()
    if not ent or not cls:
        return BAD, "趴趴GO 表讀不到"
    here = jumpmap.nearest(c.scene.id) if c.scene else None
    return OK, (f"{len(ent)} 個傳送點、{len(cls)} 個分類；"
                f"這張圖最近的={here.name if here else '（無）'}")


def c_energy(c):
    if not c.state:
        return BAD, "沒有狀態物件"
    st = energy.read(c.sc, c.state)
    names = energy.attr_names(c.sc)
    if st is None:
        return BAD, "讀不到晶能欄位（要先開過晶能視窗同步，或版面變了）"
    return ((OK if names else BAD),
            f"能量 {st.energy}、屬性名稱 {len(names)} 個")


def c_decomp(c):
    try:
        lst = energy.decomposable(c.sc)
        note = energy.blocked_note(c.sc)
    except Exception as exc:                               # noqa: BLE001
        return BAD, f"{type(exc).__name__}: {exc}"
    return OK, f"可分解 {len(lst)} 件" + (f"（{note}）" if note else "")


def c_sell(c):
    """販賣裝備：要看得出「哪些是裝備」與品質／耐久。"""
    its = c.bag_items or []
    eq = [i for i in its if getattr(i, "is_equip", False)]
    if not its:
        return BAD, "讀不到背包"
    if not eq:
        return NA, f"背包 {len(its)} 件，沒有裝備可賣"
    g = [f"{itemname.of(i.type_id)}(品質{getattr(i, 'grade', '?')}"
         f"/耐久{getattr(i, 'dura', '?')})" for i in eq[:3]]
    return OK, f"裝備 {len(eq)} 件：" + "、".join(g)


def c_stamp(c):
    msg = tablestamp.check()
    return (BAD if msg else OK), (msg or "資料表已對這一版核對過")


def c_locate(c):
    fail = locate.failed()
    moved = locate.moved()
    return ((BAD if fail else OK),
            f"{len(locate.SIGS)} 段特徵，失敗 {len(fail)}、位移 {len(moved)}"
            + (f"：{fail[:4]}" if fail else ""))


TABS: dict[str, list] = {
    "（共用）": [("AOB 定位", c_locate), ("資料表戳記", c_stamp)],
    "收益監控": [("角色屬性", c_stats), ("經驗球分類", c_ball)],
    "自動掛機": [("玩家座標", c_pos), ("目前場景", c_scene),
                 ("地形圖", c_terrain), ("周圍怪＋範本表", c_mobs),
                 ("目標欄位", c_target), ("快捷欄 4 頁", c_quickbar),
                 ("技能範本＋射程表", c_skill_tmpl), ("SP 讀取", c_sp),
                 ("背包／金幣", c_bag), ("身上裝備耐久", c_gear),
                 ("精靈設定樹", c_robot), ("補給判斷", c_supply),
                 ("分流／伺服器名", c_channel)],
    "自動登入": [("伺服器清單", c_servers), ("角色清單", c_chars)],
    "分身總控": [("隊伍成員", c_team), ("趴趴GO 表", c_jumpmap)],
    "能量晶化": [("晶能欄位", c_energy), ("可分解清單", c_decomp)],
    "販賣裝備": [("裝備品質／耐久", c_sell)],
}


def main() -> int:
    wins = [w for w in win.enumerate_windows(title_contains="Angels Online")
            if "_MIDAGEONL_" in w.class_name and " - " in w.title]
    if not wins:
        print("⛔ 找不到**已經進遊戲**的分身 —— 分頁讀的東西要進遊戲才有。")
        return 2
    lines: list[str] = []
    bad_total = 0
    for w in wins:
        sc = MemoryScanner()
        sc.open(w.pid)
        try:
            locate.warm(sc)
            c = Ctx(sc, w.hwnd, w.pid, w.title)
            mine = [f"=== {w.title} (pid {w.pid}) ===", ""]
            nbad = 0
            for tab, checks in TABS.items():
                mine.append(f"  【{tab}】")
                for name, fn in checks:
                    try:
                        st, detail = fn(c)
                    except Exception as exc:               # noqa: BLE001
                        st, detail = BAD, f"{type(exc).__name__}: {exc}"
                    nbad += st == BAD
                    mine.append(f"    {st} {name:<16} {detail}")
                mine.append("")
            lines += mine
            bad_total += nbad
            print(f"{'✘' if nbad else '✔'} {w.title}　"
                  f"{'失敗 %d 項' % nbad if nbad else '全部通過'}")
            for ln in mine:                    # 只印這一台壞掉的那幾行
                if ln.strip().startswith(BAD):
                    print("   " + ln.strip())
        finally:
            sc.close()
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"\n{len(wins)} 台，共 {bad_total} 項失敗。報告：{REPORT}")
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
