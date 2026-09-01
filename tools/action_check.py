"""⑤ 動作層體檢：真的打一隻怪、真的走一步 —— 靜態工具驗不到的那一刀。

    py tools\\action_check.py --yes            # 打怪＋走位，每一台都驗
    py tools\\action_check.py --yes --walk     # 只驗走位（副作用最小）
    py tools\\action_check.py --yes --attack   # 只驗打怪

⚠⚠ **這支有副作用**：會對怪出手、會讓角色走幾格（走完會走回原點）。
   所以一定要加 `--yes`，而且動使用者的分身前要先取得授權。

## 為什麼非要這一層不可

①位址 ②偏移 ③讀取 ④查表 **全綠還是可能整個功能是廢的**。2026-08-11 實錄：
位址全綠、selfcheck 13/13、資料表全對，掛機站在怪旁邊完全不出手 —— 真兇是
`usequickkey` 第三個參數從「0 可以」變成「0 一律失敗」。參數個數沒變、函式頭
沒變、特徵完美命中，**只有真的打一隻怪才驗得出來**。

## ⚠⚠ 假紅燈跟假綠燈一樣糟

這支的結論會被拿來判「改版是不是把動作層弄壞了」，所以**「驗不了」一定要跟
「壞了」分開**。2026-08-28 /_audit 就踩到：工具箱自己的掛機正在跑那幾台，
每 150ms 把目標換成它要打的怪，於是「寫進去又讀回別的」三台全中，收尾印
「⛔ 呼叫慣例語意改掉了」—— 其實 `OFF_TARGET` 完全正常（見下面坑 5）。

## ⚠⚠ 寫這種探針最容易踩的坑（2026-08-25 踩過 1~4、08-28 踩到 5）

1. **一定要用 `entity.set_target()`（目標 ID ＋ 血量欄兩個一起寫）**，
   不是 `set_target_id()`。只寫 ID 的話血量欄停在 0，遊戲會認定目標已死
   而**直接跳過攻擊** —— 症狀是「叫了 8 下、指令槽 8 次全成功、怪血一動也
   不動」，看起來就像改版把掛機弄壞了。（見 entity.set_target 的說明。）
2. **不能拿快捷欄第一格就用**：要先過 `skillcost.sp_enough` 與 MP，
   再比對 `skills.range_of()` 與距離，否則會挑到 SP 不足、或射程差十幾格的招。
3. **`player.read()` 要餵角色屬性物件**（`player.locate_fast()`），
   不是 `entity.snapshot()` 回的玩家實體 —— 餵錯回 None，MP 那條證據就沒了。
4. **走位一定要排在打怪之前，而且先把目標清掉**。打完怪角色還在交戰狀態，
   遊戲自己的「接近怪」狀態機會把我們的 `walk_exact` 整個蓋掉 —— 症狀是
   座標**一格都沒動**，看起來就像走位壞了。5 台裡有 3 台這樣假失敗過。

5. **目標欄被搶著寫 ≠ 目標欄搬家**。掛機在跑的分身每 150ms 就重選一次目標，
   我們寫進去 0.3 秒後當然讀回別的。分辨法：寫完**立刻**再讀一次 ——
   0ms 讀回我們寫的值就代表寫入有落地；之後被換成**另一隻活著的實體**，
   那是別人在正常選怪，判「驗不了」不判「壞了」。
   （真的搬家長什麼樣：0ms 當下就不是我們寫的值。）

## 判定

✔ 打得到 —— 目標血量% 真的下降，或 MP 真的照技能的消耗扣了
✘ 打不到 —— 叫得出去（進得了指令槽）但兩個證據都沒有 ★ 這就是改版壞掉的樣子
– 沒驗到 —— 附近沒活怪／沒有負擔得起又射程夠的招／狀態物件中途失效／
           **這台正被掛機等功能佔著**（目標欄被搶、動畫狀態是 'Cast'）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import charname, injector, preload                 # noqa: E402
from app.core.memory import MemoryScanner                        # noqa: E402
from app.game import entity, locate, move, player                # noqa: E402
from app.game import quickbar, skillcost, skills, terrain        # noqa: E402

OUT = ROOT / "reports" / "action_check.txt"
OWNER = object()
TRIES = 12          # 最多叫幾下快捷鍵
GAP = 0.8           # 每下之間等多久（遊戲自己有冷卻，叫太密只是白叫）
WALK_TILES = 3      # 走幾格
# 這些動畫狀態代表「角色現在不可能動」——判驗不了，不是判失敗。
# 'Cast' ＝ 自動練技／掛機正在放招（2026-08-25 實錄）。
# 'Dead' ＝ **角色是死的**：走位不會動、招也放不出去，兩項都會假失敗
#   （2026-08-28 /_audit：黑狐死在那裡，走位跟打怪同時被判 ✘，
#    收尾還印「呼叫慣例語意改掉了」）。屍體驗不了動作層，不是動作層壞了。
BUSY_STATES = ("Cast", "Dead")


def cannot_act(sc, me) -> str:
    """角色現在有沒有「根本不可能動」的理由？有就回那個動畫狀態，沒有回 ""。

    ⛔ 打怪／走位**兩邊都要先問這一句**：只有走位問過，害死掉的角色在打怪那邊
      照樣被判成「叫得出去卻沒反應」＝改版壞掉的樣子（假紅燈）。
    """
    st = entity.read_state(sc, me)
    return st if st in BUSY_STATES else ""


def usable_skills(sc, ent_addr, mp_now):
    """快捷欄第 1 頁裡「真的打得出去」的攻擊招：非 buff、SP 夠、MP 夠。

    回 [(格號, 技能ID, (MP, SP), 射程), ...]，射程大的排前面。
    """
    page = quickbar.read_page(sc, 0) or []
    out = []
    for i, s in enumerate(page):
        if not s or not s.is_skill or not s.value:
            continue
        if skills.of(s.value):                 # 有持續時間 = buff，不是攻擊招
            continue
        cost = skillcost.cost(sc, s.value)
        if cost is None:                       # 範本讀不到 = 不知道，別冒險
            continue
        if skillcost.sp_enough(sc, ent_addr, mp_now, s.value) is False:
            continue
        if cost[0] > (mp_now or 0):
            continue
        out.append((i, s.value, cost, skills.range_of(s.value) or 0))
    out.sort(key=lambda t: -t[3])
    return out


def pick_pair(sc, mobs, pos, ent_addr, mp_now):
    """配出一組「射程真的夠得到」的（怪, 技能）—— 由近而遠試。

    ⚠⚠ **絕對不要把射程外的怪拿來當測試對象。** 打不到本來就不會掉血、
      不會扣 MP，判成 ✘ 就是誤報 —— 而這支工具存在的意義就是「✘ 一定要
      代表真的壞了」。配不出來寧可回 None（＝沒驗到）。
      （2026-08-25 實際踩到：怪 12.2 格、招射程 12，被判成打不到。）
    """
    skl = usable_skills(sc, ent_addr, mp_now)
    if not skl:
        return None
    mx, my = pos
    for t in mobs:
        dist = ((t.x - mx) ** 2 + (t.y - my) ** 2) ** 0.5
        for slot, sid, cost, rng in skl:
            if rng >= dist:
                return t, dist, slot, sid, cost, rng
    return None


def check_attack(sc, mv, log, state, me, base):
    """設目標 → 叫快捷鍵 → 看怪血% 掉了沒／MP 扣了沒。回 True/False/None。"""
    _st, _me, ents, _h, _e = entity.snapshot(sc)
    pos = entity.player_pos(sc, me)
    stats = player.read(sc, base) if base else None
    if not pos or not stats:
        log.append("    – 讀不到座標或角色屬性")
        return None
    busy = cannot_act(sc, me)
    if busy:
        log.append(f"    – 角色動畫狀態是 {busy!r}"
                   f"（死了／正被別的功能佔著）→ 這台驗不了")
        return None
    mobs = [e for e in ents if e.is_monster and not e.dead]
    if not mobs:
        log.append("    – 附近沒有活著的怪")
        return None
    mx, my = pos
    mobs.sort(key=lambda e: (e.x - mx) ** 2 + (e.y - my) ** 2)
    got = pick_pair(sc, mobs, pos, me, stats.mp)
    if not got:
        near = ((mobs[0].x - mx) ** 2 + (mobs[0].y - my) ** 2) ** 0.5
        best = max((r for *_x, r in usable_skills(sc, me, stats.mp)), default=0)
        log.append(f"    – 配不出「射程夠得到」的組合：最近的怪 {near:.1f} 格、"
                   f"最大可用射程 {best}（MP {stats.mp}）")
        return None
    t, dist, slot, sid, cost, rng = got
    log.append(f"    目標 {t.name} eid={t.eid} 距離 {dist:.1f} 格")
    log.append(f"    用 F{slot + 1} 技能 {sid}（{skills.name_of(sid) or '?'}）"
               f"射程 {rng} MP={cost[0]} SP={cost[1]}｜MP 現有 {stats.mp}")

    # ★★ 兩欄一起寫（見檔頭坑 1）。順便補驗 verify_offsets 常常驗不到的
    #    entity.OFF_TARGET —— 它要有選定目標才驗得到。
    entity.set_target(sc, state, t.eid)
    # ⚠⚠ 坑 5（2026-08-28 /_audit）：讀回別的值**不一定是欄位搬家** ——
    #   工具箱自己的掛機正在跑這一台時，它每 150ms 就會把目標換成它要打的怪，
    #   於是「寫進去又讀回別的」每次都成立 → 三台被判 ✘、收尾還印
    #   「⛔ 呼叫慣例語意改掉了」，那是**假的紅燈**（跟假綠燈一樣糟）。
    #   實測：寫完 0ms 讀回來一定是我們寫的值（寫入有落地），50ms 後才被換成
    #   **另一隻活著的同種怪** —— 那是別人在正常選怪，不是我們寫壞了。
    #   ⇒ 讀回來的值如果對得上一隻活著的實體，就是有人在搶，判「驗不了」。
    ok0, tid0, _hp = entity.read_target_checked(sc, state)
    landed = tid0 == t.eid
    time.sleep(0.3)
    ok, tid, hp0 = entity.read_target_checked(sc, state)
    mark = "✔" if tid == t.eid else "✘"
    log.append(f"    目標欄位 +{entity.OFF_TARGET:#x}：寫 {t.eid} → 讀回 {tid} "
               f"{mark}｜血量% 欄 = {hp0}")
    if tid != t.eid:
        other = next((e for e in ents if e.eid == tid and not e.dead), None)
        if landed and other is not None:
            log.append(f"    – 寫入有落地（當下讀回 {tid0} ＝我們寫的），"
                       f"0.3 秒後被換成活怪「{other.name}」"
                       f"＝這台正被別的功能佔著（掛機在跑）→ 驗不了")
            return None
        if landed:
            log.append(f"    – 寫入有落地（當下讀回 {tid0}），"
                       f"0.3 秒後變成 {tid}（不是已知的活怪）→ 驗不了")
            return None
        log.append("    ✘ 寫進去當下就不是我們寫的值 —— 欄位可能真的搬家了")
        return False

    mp_before = stats.mp
    sp_before = skillcost.sp_now(sc, me, mp_before)
    sent = 0
    hp = hp0
    for k in range(TRIES):
        if quickbar.use(mv, sc, slot, 0):
            sent += 1
        time.sleep(GAP)
        ok, tid, hp = entity.read_target_checked(sc, state)
        now = player.read(sc, base)
        mp_now = now.mp if now else None
        # ⚠ sp_now 要拿**同一拍**的 MP 當基準（見 skillcost.sp_now 的說明）
        sp_now_ = skillcost.sp_now(sc, me, mp_now) if mp_now is not None else None
        if not ok:
            log.append("    – 狀態物件中途失效（換圖／重生）—— 這一輪不算數")
            return None
        if tid == t.eid and hp < hp0:
            log.append(f"    ✔ 第 {k + 1} 下：目標血量 {hp0}% → {hp}%"
                       f"（MP {mp_before} → {mp_now}）")
            return True
        # MP 真的照這一招的消耗扣掉，也算硬證據（怪可能被別人搶著打死）
        if cost[0] > 0 and mp_now is not None and mp_now <= mp_before - cost[0]:
            log.append(f"    ✔ 第 {k + 1} 下：MP {mp_before} → {mp_now}"
                       f"（扣了 {mp_before - mp_now}，招確實放出去了）")
            return True
        # ⚠⚠ **SP 招也要認**（2026-09-01 實錄的假紅燈）：嵐狐挑到的 F1 是
        #   946 冰凍狙擊Ⅳ，MP=0／SP=1000 —— 怪血明明 100% → 48%，但因為
        #   `tid` 被遊戲換成別隻、MP 又一毛都沒扣，整項判成 ✘「叫得出去卻
        #   沒反應」，還印出「呼叫慣例語意改掉了」的收尾。只看 MP 等於對
        #   **半數的招視而不見**。SP 照消耗扣掉跟 MP 扣掉是一樣硬的證據。
        if (cost[1] > 0 and sp_before is not None and sp_now_ is not None
                and sp_now_ <= sp_before - cost[1]):
            log.append(f"    ✔ 第 {k + 1} 下：SP {sp_before} → {sp_now_}"
                       f"（扣了 {sp_before - sp_now_}，招確實放出去了）")
            return True
    now = player.read(sc, base)
    sp_end = skillcost.sp_now(sc, me, now.mp) if now else None
    log.append(f"    ✘ 叫了 {TRIES} 下（進指令槽 {sent} 次）：血量% {hp0} → {hp}、"
               f"MP {mp_before} → {now.mp if now else None}、"
               f"SP {sp_before} → {sp_end}")
    return False


def check_walk(sc, mv, log, me, state):
    """走幾格再走回來；硬證據＝座標真的變。回 True/False/None。

    ⚠ 先把目標清掉（見檔頭坑 4）：只要還鎖著怪，遊戲自己的「接近怪」狀態機
      會蓋掉我們送的目的地，角色一格都不會動 —— 那是假失敗，不是走位壞了。
    """
    if state:
        entity.set_target(sc, state, 0, 0)
        time.sleep(0.6)
    p0 = entity.player_pos(sc, me)
    grid, why = terrain.load(sc)
    if not p0:
        log.append("    – 讀不到座標")
        return None
    if not grid:
        log.append(f"    – 讀不到地形圖（{why}）")
        return None
    # ⚠⚠ `walk_exact` **不尋路** —— 它只是把目的地交給遊戲。所以終點光是
    #   「那一格可走」不夠，中間有牆照樣一格都不會動（千夜魔宮實際踩到：
    #   座標完全沒變 → 被判成走位壞掉，其實是我挑錯終點）。
    #   用 grid.clear_line() 要求**直線全程可走**才算數。
    here = (int(p0[0]), int(p0[1]))
    dest = None
    for n in (WALK_TILES, 2):
        for dx, dy in ((n, 0), (-n, 0), (0, n), (0, -n),
                       (n, n), (-n, -n), (n, -n), (-n, n)):
            cx, cy = here[0] + dx, here[1] + dy
            if grid.walkable(cx, cy) and grid.clear_line(here, (cx, cy)):
                dest = (cx + 0.5, cy + 0.5)
                break
        if dest:
            break
    if not dest:
        log.append("    – 附近找不到「直線走得過去」的格子（牆邊／死角）")
        return None

    # ⚠⚠ 「被別的功能佔著」要**整趟一路採樣**，不能只在最後看一眼。
    #   2026-09-01 實錄：白狐開著自動練技，是**斷斷續續**在施法 —— 走到一半
    #   被robot打斷（只挪了 0.3 格就停），等我們回頭讀狀態時它已經回到 'Wait'
    #   → 判成 ✘「叫得出去卻沒反應」＝假紅燈。
    #   跟 tab_check 的 sp_now 同一個老坑：**量測工具自己把數據弄假**。
    seen: set[str] = set()

    def go(tx, ty, label):
        mv.walk_exact(sc, me, tx, ty)
        end = p0
        for _ in range(25):
            time.sleep(0.25)
            st = entity.read_state(sc, me)
            if st in BUSY_STATES:
                seen.add(st)
            end = entity.player_pos(sc, me) or end
            if abs(end[0] - tx) < 0.6 and abs(end[1] - ty) < 0.6:
                break
        log.append(f"    {label} → 目標 ({tx:.1f}, {ty:.1f})　"
                   f"實到 ({end[0]:.1f}, {end[1]:.1f})")
        return end

    log.append(f"    起點 ({p0[0]:.1f}, {p0[1]:.1f})")
    a = go(dest[0], dest[1], "去程")
    moved = abs(a[0] - p0[0]) > 0.5 or abs(a[1] - p0[1]) > 0.5
    if not moved:
        # ⚠⚠ 沒動不一定是走位壞了 —— **這台可能正被別的功能佔著**。
        #   自動練技就是典型：角色一直在原地施法（動畫狀態 'Cast'），
        #   我們送的目的地當然沒人理。判成 ✘ 就是誤報。
        #   （2026-08-25 使用者當場指出：白狐在練技，所以不能動。）
        # 現在是不是忙 ＋ **這一趟有沒有忙過**（斷斷續續施法的抓不到當下）
        busy = cannot_act(sc, me) or (sorted(seen)[0] if seen else "")
        if busy:
            log.append(f"    – 沒動，但這一趟出現過動畫狀態 {busy!r}"
                       f" ＝ 角色死了／正被別的功能佔著（自動練技／掛機）→ 驗不了")
            return None
        log.append(f"    ✘ 座標一格都沒動"
                   f"（動畫狀態 {entity.read_state(sc, me)!r}，"
                   f"整趟沒出現過 {BUSY_STATES}）")
        return False
    b = go(p0[0], p0[1], "回程")               # 收拾乾淨：走回原本站的地方
    back = abs(b[0] - p0[0]) < 0.9 and abs(b[1] - p0[1]) < 0.9
    log.append(f"    ✔ 座標真的變了；{'✔' if back else '⚠'} 回到原點")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true",
                    help="確認要動使用者的角色（有副作用，必填）")
    ap.add_argument("--attack", action="store_true", help="只驗打怪")
    ap.add_argument("--walk", action="store_true", help="只驗走位")
    # 掛機在跑的分身會搶目標欄（見檔頭坑 5），驗不了。要驗打怪就得先空一台出來，
    # 所以要能只挑那一台跑 —— 不然每次都得把五台全打擾一遍。
    ap.add_argument("--acct", default="",
                    help="只驗這個帳號（比對視窗標題，可給片段）")
    a = ap.parse_args()
    if not a.yes:
        print("⚠ 這支會動到角色（出手、走位）。確定要跑就加 --yes。")
        return 2
    do_atk = a.attack or not (a.attack or a.walk)
    do_walk = a.walk or not (a.attack or a.walk)

    wins = preload.windows()
    if a.acct:
        wins = [w for w in wins if a.acct in w.title]
    if not wins:
        print("找不到遊戲視窗 —— 先把遊戲開起來、進到遊戲裡再跑。"
              if not a.acct else f"沒有標題含「{a.acct}」的遊戲視窗。")
        return 1

    log = ["=== ⑤ 動作層體檢（真的打一隻怪／走一步）==="]
    res = {}
    for w in wins:
        acc = charname.account_from_title(w.title) or str(w.pid)
        log.append(f"\n── {acc}（pid {w.pid}）")
        sc = MemoryScanner()
        atk = walk = None
        try:
            sc.open(w.pid)
            locate.warm(sc)                     # ⚠ 碰記憶體前的第一件事
            state, me, _e, _h, _x = entity.snapshot(sc)
            if not state or not me:
                log.append("    ✘ 狀態／玩家物件讀不到")
                res[acc] = (False, False)
                continue
            base = player.locate_fast(sc) or player.locate(sc)
            mv = move.acquire(w.pid, injector.process_path(w.pid), OWNER)
            try:
                # ★ 順序不能反（見檔頭坑 4）：先走位再打怪。
                #   打完怪角色還在交戰狀態，走位會被遊戲蓋掉而假失敗。
                if do_walk:
                    log.append("  【走位】")
                    walk = check_walk(sc, mv, log, me, state)
                if do_atk:
                    log.append("  【打怪】")
                    atk = check_attack(sc, mv, log, state, me, base)
            finally:
                move.release(w.pid, OWNER)
        except Exception as exc:                # noqa: BLE001
            log.append(f"    ✘ 例外 {type(exc).__name__}: {exc}")
            atk = walk = False
        finally:
            sc.close()
        res[acc] = (atk, walk)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(log) + "\n", encoding="utf-8")

    sym = {True: "✔", False: "✘", None: "–"}
    bad = 0
    print("=== ⑤ 動作層 ===")
    for acc, (atk, walk) in res.items():
        bits = []
        if do_atk:
            bits.append(f"打怪 {sym[atk]}")
        if do_walk:
            bits.append(f"走位 {sym[walk]}")
        bad += (atk is False) + (walk is False)
        print(f"  {acc:<14} " + "　".join(bits))
    n_ok = sum(1 for v in res.values() if True in v)
    print(f"\n{len(res)} 台；驗到 {n_ok} 台有動作、{bad} 項失敗。報告：{OUT}")
    if bad:
        print("⛔ 有分身『叫得出去卻沒反應』—— 這正是 2026-08-11 那種"
              "呼叫慣例語意改掉的樣子，回頭查 use_quickkey / OFF_TARGET。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
