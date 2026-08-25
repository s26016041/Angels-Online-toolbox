"""結構偏移總驗：每一個寫死的欄位偏移，都拿真遊戲的不變量驗一次。

    py tools\\verify_offsets.py            # 開著遊戲跑（純讀）
    py tools\\verify_offsets.py --strict   # 「無法驗證」也算失敗（發布前用）

為什麼要有這支
--------------
`tools/verify_sigs.py` 驗的是**位址**（AOB 找不找得到）。但 2026-08-11 那次
改版真正把掛機弄壞的是**結構偏移搬家**：目標欄位從 +0x2D8 移到 +0x270、
組隊隊員陣列 +0x31A0 → +0x3140、晶化那組 +0xB8 → +0x54。

這種壞法**沒有任何錯誤訊息**：舊偏移照樣讀得到（讀到的是別的成員）、
照樣寫得進去（安靜地改壞遊戲的堆積），功能只是「什麼都不做」。
所以每個偏移都要有一條「值長得對不對」的不變量，改版後一跑就看得出來。

判定方式
--------
✔  這條不變量成立（欄位還在原位）
✘  不成立 —— **偏移八成搬家了**，去 reports/offset_verify.txt 看細節
–  這台當下驗不了（沒進遊戲／沒隊友／背包空），不算通過也不算失敗

⚠ 純讀取：不寫記憶體、不呼叫遊戲函式、不送封包。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import charname, preload                      # noqa: E402
from app.core.memory import MemoryScanner                   # noqa: E402
from app.game import bag, energy, entity, inventory         # noqa: E402
from app.game import locate, login, monsters, move, player  # noqa: E402
from app.game import quickbar, scene, skillcost, skills     # noqa: E402
from app.game import scenery, team, terrain                 # noqa: E402

OUT = ROOT / "reports" / "offset_verify.txt"
NA = None                       # 「這台驗不了」

# ★ 這支到底驗到了哪些偏移 —— 給 tools/coverage_audit.py 算覆蓋率用。
#   (模組, 名字或前綴)；前綴以底線結尾表示「開頭是這個的都算」。
#   ⚠ **加新檢查時要一起加這裡**，不然覆蓋率報告會把它算成「沒保護」。
#   ⚠ 反過來更糟：這裡寫了、下面卻沒真的驗，等於自己騙自己 —— 只准照實寫。
COVERS: tuple[tuple[str, str], ...] = (
    ("entity", "OFF_TARGET"), ("entity", "OFF_TARGET_HP_GAP"),
    ("entity", "OFF_POS_"), ("entity", "OFF_ID"), ("entity", "OFF_STATE"),
    ("entity", "OFF_WEIGHT"), ("entity", "OFF_WEIGHT_MAX"),
    ("team", "MEMBERS_OFF"), ("team", "MEMBER_STRIDE"),
    ("energy", "OFF_"),
    ("quickbar", "TABLE_OFF"),
    ("robot", "ROBOT_READY_OFF"),
    # 場景管理器那條：查表(bag 那串偏移) vs vtable 掃描，兩條獨立的路要對上
    ("bag", "OFF_MY_ID"), ("bag", "OFF_ENT_TABLE"), ("bag", "OFF_ENT_CAP"),
    ("bag", "OFF_ENT_ID"),
    # OFF_SCENE_MGR 沒有自己的檢查，但背包那條讀不到就會紅 —— 間接涵蓋
    ("bag", "OFF_SCENE_MGR"), ("bag", "ITEM_"), ("bag", "OFF_CONTAINER"),
    ("monsters", "OFF_"),
    ("player", "OFF_"),
    ("skillcost", "OFF_"),
    ("scene", "OFF_SCENE_ID"),
    ("terrain", "OFF_W"), ("terrain", "OFF_H"), ("terrain", "OFF_ROWS"),
    ("scenery", "OFF_MODEL"),
    # 伺服器清單那條：servers() 走 SRV_BEGIN/END/STRIDE 撈整張、每筆驗
    # port 範圍＋兩格分流數互驗＋名稱可讀 —— SRV_ 開頭的整組都吃這條。
    ("login", "SRV_"),
)


def u32(sc, a):
    r = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(r[:4]))[0] if r and len(r) >= 4 else None


def i32(sc, a):
    r = sc._read_bytes(a, 4)
    return struct.unpack("<i", bytes(r[:4]))[0] if r and len(r) >= 4 else None


def checks(sc, log):
    """回傳 {項目: (True/False/None, 說明)}。每一條都是純讀。"""
    out: dict[str, tuple[bool | None, str]] = {}

    def put(name, ok, detail=""):
        out[name] = (ok, detail)

    locate.warm(sc)
    state, pl, ents, _r, _x = entity.snapshot(sc)
    stats = player.locate_fast(sc)

    # --- 狀態物件 ---------------------------------------------------------
    put("狀態物件 vtable", state is not None, state and hex(state) or "掃不到")
    if state is None:
        return out

    # entity.OFF_TARGET：沒目標時是 0；有目標時必須是**現場某個實體的 ID**，
    # 而且血量欄（+4）落在 0~100。這是「寫錯欄位」最直接的照妖鏡。
    ok, tid, thp = entity.read_target_checked(sc, state)
    ids = {e.eid for e in ents}
    if not ok:
        put("目標欄位 entity.OFF_TARGET", NA, "狀態物件驗不過")
    elif tid == 0:
        put("目標欄位 entity.OFF_TARGET", NA, "目前沒有選定目標")
    else:
        put("目標欄位 entity.OFF_TARGET", tid in ids and 0 <= thp <= 100,
            f"ID {tid:#x}{'（在實體清單裡）' if tid in ids else '（不在清單裡）'}"
            f" 血量欄 {thp}")

    # team.MEMBERS_OFF：沒隊友時全 0（驗不了）；有隊友時 ID 必須是實體 ID
    # 或至少落在合理範圍，而且名字要是可讀字串。
    raw = sc._read_bytes(state + team.MEMBERS_OFF,
                         team.MEMBER_STRIDE * team.MEMBER_MAX)
    if not raw:
        put("隊員陣列 team.MEMBERS_OFF", NA, "讀不到")
    else:
        b = bytes(raw)
        alive = []
        for i in range(team.MEMBER_MAX):
            off = i * team.MEMBER_STRIDE
            mid = struct.unpack_from("<I", b, off + team.M_ID)[0]
            if not mid:
                continue
            nm = b[off + team.M_NAME:off + team.M_NAME + team.NAME_MAX]
            nm = nm.split(b"\0")[0]
            alive.append((mid, nm))
        if not alive:
            put("隊員陣列 team.MEMBERS_OFF", NA, "現在沒有隊友")
        else:
            good = all(0 < m < 0xFFFFFFFF and n and _printable(n)
                       for m, n in alive)
            put("隊員陣列 team.MEMBERS_OFF", good,
                "；".join(f"{m:#x} {n.decode('utf-8', 'replace')}"
                          for m, n in alive))

    # energy 四欄：read() 自己就會逐欄驗版面，回 None 代表驗不過。
    # ⚠⚠ **四欄全 0 不算通過。** 晶化資料要「開過晶能視窗（送 0x3F）」才會同步
    #   下來；沒同步時整片是 0，而 0 通過每一道逐欄驗證（能量 0、抽到 −1、
    #   每次 0、點數全 0）→ read() 回一個合法物件 → 這一項就印綠燈。
    #   2026-08-25 實際踩到：OFF_ENERGY 已經搬家（0x54 → 0x34），這項還是 ✔，
    #   直到使用者自己開了晶能視窗、錯位置讀到垃圾才顯現。
    #   （memory `lazy-sync-request-first`：全 0 先懷疑沒同步。）
    #   ⚠ 這支是純讀工具，不會替你送 0x3F —— 判成「驗不了」並請人開一次視窗。
    got = energy.read(sc, state)
    synced = got is not None and (got.energy or got.result is not None
                                  or got.per_roll or any(got.points))
    if got is not None and not synced:
        put("晶化欄位 energy.OFF_*", NA,
            "四欄全 0 ＝ 這台還沒開過晶能視窗同步 —— 驗不了（開一次晶能視窗再跑）")
    else:
        put("晶化欄位 energy.OFF_*", got is not None,
            "" if got is None else
            f"能量 {got.energy}／抽到 {got.result}／每次 {got.per_roll}"
            f"／點數 {sum(got.points)}")

    # quickbar.TABLE_OFF：讀得到頁面且每一格型別合法（read_page 內建驗證）
    page = quickbar.read_page(sc, 0)
    put("快捷欄 quickbar.TABLE_OFF", page is not None,
        f"第 0 頁 {sum(1 for s in (page or []) if s)} 格有東西")

    # robot.ROBOT_READY_OFF：那一格是布林，只准 0/1
    from app.game import robot
    v = u32(sc, state + robot.ROBOT_READY_OFF)
    put("精靈就緒 robot.ROBOT_READY_OFF", v in (0, 1), f"值 {v}")

    # --- 場景管理器 / 玩家物件 -------------------------------------------
    # bag.OFF_MY_ID + OFF_ENT_TABLE + OFF_ENT_CAP + OFF_ENT_ID：
    #   兩條**互相獨立**的路都要指向同一個玩家物件（查表 vs vtable 掃描）。
    pf = move.pathfinder_this(sc)
    put("場景管理器 bag.OFF_MY_ID／表／上限",
        NA if (pf is None or pl is None) else (pf + 8 == pl),
        f"查表得 {pf and hex(pf)}／掃描得 {pl and hex(pl)}（應差 8）")

    # entity.OFF_POS_X/Y：玩家座標要落在這張地圖的範圍內
    grid, _why = terrain.load(sc)
    pos = entity.read_pos(sc, pl) if pl else None
    if pos is None:
        put("座標 entity.OFF_POS_X/Y", NA, "讀不到玩家座標")
    elif grid is None:
        put("座標 entity.OFF_POS_X/Y", 0 <= pos[0] < 4096 and 0 <= pos[1] < 4096,
            f"{pos[0]:.1f},{pos[1]:.1f}（沒有地形圖可比對）")
    else:
        inside = 0 <= pos[0] < grid.w and 0 <= pos[1] < grid.h
        put("座標 entity.OFF_POS_X/Y 對地形圖", inside,
            f"{pos[0]:.1f},{pos[1]:.1f} vs 地圖 {grid.w}x{grid.h}")

    # entity.OFF_WEIGHT / OFF_WEIGHT_MAX：負重要是「0 < 現值 <= 上限」，
    # 而且上限說得通（實測五台 11308~34860）。版面搬家的話這兩格會變成
    # 別的欄位 —— 幾乎不可能同時滿足這三個條件，所以會紅。
    wt = entity.weight(sc, pl) if pl else None
    if wt is None:
        put("負重 entity.OFF_WEIGHT", NA, "讀不到（沒進場／版面變了）")
    else:
        cur, cap = wt
        put("負重 entity.OFF_WEIGHT",
            0 < cap < 1 << 22 and 0 <= cur <= cap,
            f"{cur} / {cap}（{cur * 100.0 / cap:.0f}%）")

    # entity.OFF_ID：每隻唯一 + 玩家自己的 ID 要跟場景管理器記的一致
    if ents:
        uniq = len({e.eid for e in ents}) == len(ents)
        put("實體 ID entity.OFF_ID 唯一", uniq, f"{len(ents)} 個實體")
    else:
        put("實體 ID entity.OFF_ID 唯一", NA, "沒掃到實體")

    # entity.OFF_KIND / OFF_TYPE：怪的種類 ID 要查得到範本
    idx = monsters.index_base(sc)
    mons = [e for e in ents if e.is_monster]
    if mons and idx:
        info = monsters.info(sc, mons[0].type_id, idx)
        put("怪物範本 monsters.OFF_*",
            bool(info) and 0 < getattr(info, "level", 0) <= 500,
            f"{getattr(info, 'name', '?')} Lv{getattr(info, 'level', '?')}"
            f" HP{getattr(info, 'max_hp', '?')}")
    else:
        put("怪物範本 monsters.OFF_*", NA, "附近沒有怪")

    # entity.OFF_STATE：動畫狀態要是可讀的短字串
    if mons:
        st = entity.read_state(sc, mons[0].addr)
        put("動畫狀態 entity.OFF_STATE",
            bool(st) and st.isascii() and 2 <= len(st) <= 12, f"'{st}'")
    else:
        put("動畫狀態 entity.OFF_STATE", NA, "附近沒有怪")

    # --- 角色屬性 ---------------------------------------------------------
    st = player.read(sc, stats) if stats else None
    if st is None:
        put("角色屬性 player.OFF_*", NA, "定位不到角色屬性")
    else:
        sane = (0 < st.level <= 500 and 0 <= st.hp <= st.max_hp
                and 0 <= st.mp <= st.max_mp and st.exp_lo <= st.exp <= st.exp_hi)
        put("角色屬性 player.OFF_*", sane,
            f"Lv{st.level} HP{st.hp}/{st.max_hp} MP{st.mp}/{st.max_mp}")

    # skillcost.OFF_ATTR(+MP/+SP)：子物件裡的 MP 要等於角色屬性那份 MP
    if st and pl and st.mp > 0:
        sp = skillcost.sp_now(sc, pl, st.mp)
        put("數值子物件 skillcost.OFF_ATTR/MP/SP", sp is not None,
            f"對帳到 MP={st.mp}，SP={sp}")
    else:
        put("數值子物件 skillcost.OFF_ATTR/MP/SP", NA, "MP 是 0 或讀不到")

    # 技能範本 +0x50/+0x54/+0x58/+0x5C：拿資源包那張表逐筆對帳
    hit = tot = 0
    for sid in range(1, 4000):
        want = skills.range_of(sid)
        if want is None:
            continue
        got2 = skillcost.cost(sc, sid)
        tot += 1
        hit += got2 is not None
        if tot >= 400:
            break
    put("技能範本 skillcost.OFF_SHOOT_RANGE/COST_*",
        tot > 0 and hit == tot, f"{hit}/{tot} 筆與 magic.xml 對得上")

    # --- 場景 / 地形 ------------------------------------------------------
    here = scene.current(sc)
    put("場景編號 scene.OFF_SCENE_ID", here is not None, str(here))
    if grid is not None:
        put("地形圖 terrain.OFF_W/H/ROWS",
            8 <= grid.w <= 4096 and 8 <= grid.h <= 4096,
            f"{grid.w}x{grid.h}")
    else:
        put("地形圖 terrain.OFF_W/H/ROWS", NA, "讀不到地形圖")

    # --- 背包 / 物品 ------------------------------------------------------
    items = None
    try:
        items = bag.items(sc)
    except Exception as exc:                       # noqa: BLE001
        log.append(f"bag.items 例外：{exc!r}")
    if not items:
        put("背包欄位 bag.ITEM_*", NA, "背包讀不到／是空的")
    else:
        # 物品自己記的格號要落在合法範圍、耐久不可超過範本上限、
        # 種類 ID 要查得到名字（三個欄位各自獨立，一起錯的機率極低）
        slot_ok = sum(1 for it in items if 0 <= it.slot < bag.MAX_SLOTS)
        dura_ok = all(it.dura <= it.dura_max for it in items if it.dura_max)
        named = sum(1 for it in items
                    if not it.name.startswith("物品 "))
        put("背包欄位 bag.ITEM_*",
            slot_ok == len(items) and dura_ok and named > 0,
            f"{len(items)} 件、格號合法 {slot_ok}、查得到名字 {named}")

    # scenery：可以點的**製作站台**（vtable 0x7D87B4／kind0／選定id在 +0x1D0）。
    # 不變量：讀得到一些、每一筆的選定 id 世代碼與索引都非 0（⚠不是固定 0x13——
    # 那是每張圖的世代碼，見 scenery.py）＋外觀非 0、座標落在地形圖裡面。
    me = entity.player_pos(sc, (move.pathfinder_this(sc) or 0) + 8)
    props = scenery.nearby(sc, me, 25.0) if me else None
    if not props:
        put("製作站台 scenery.nearby", NA,
            "附近沒有製作站台／讀不到（換到有檯子的圖再驗）")
    else:
        grid, _msg = terrain.load(sc)
        inside = all(0 <= p.x < (grid.w if grid else 4096)
                     and 0 <= p.y < (grid.h if grid else 4096) for p in props)
        ids_ok = all((p.oid >> 16) and (p.oid & 0xFFFF) and p.model
                     for p in props)
        put("製作站台 scenery.nearby", inside and ids_ok,
            f"{len(props)} 個、最近 {props[0].dist(me):.1f} 格"
            f"、外觀 {props[0].model}、選定id {props[0].oid:#x}")

    # login.SRV_*：伺服器清單版面。servers() 每筆已做結構性驗證（port 範圍、
    # 兩格分流數互驗、名稱可讀）——版面搬家時整張清單會讀成空。
    # 分流數改讀這條是 2026-08-11 換 channel.count() 時五台實測過的路。
    app = u32(sc, login.APP_PTR)
    if not app or not 0x10000 < app < 0x7FFF0000:
        put("伺服器清單 login.SRV_*", NA, "APP 物件還沒好（還在載入？）")
    else:
        srv = login.servers(sc)
        good = bool(srv) and all(
            1 <= n <= login.MAX_SUBSET and nm for nm, n in srv)
        put("伺服器清單 login.SRV_*", good,
            "、".join(f"{nm}×{n}" for nm, n in srv) if srv
            else "APP 物件在、清單卻讀不到 —— SRV_* 版面八成搬家了")
    return out


def _printable(raw: bytes) -> bool:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(s) and all(ch.isprintable() for ch in s)


def main() -> int:
    strict = "--strict" in sys.argv
    wins = preload.windows()
    if not wins:
        print("找不到遊戲視窗 —— 先把遊戲開起來、進到遊戲裡再跑。")
        return 1
    log: list[str] = []
    per_client: list[tuple[str, dict]] = []
    for w in wins:
        acc = charname.account_from_title(w.title) or ""
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            nm = preload.name_of(w.pid, sc, acc)
            per_client.append((nm or acc, checks(sc, log)))
        except Exception as exc:                   # noqa: BLE001
            per_client.append((acc, {"讀取記憶體": (False, repr(exc))}))
        finally:
            sc.close()

    names = []
    for _nm, res in per_client:
        for k in res:
            if k not in names:
                names.append(k)

    lines = ["=== 結構偏移總驗（純讀）==="]
    bad, unknown = [], []
    for key in names:
        got = [res.get(key) for _n, res in per_client]
        got = [g for g in got if g is not None]
        oks = [g[0] for g in got]
        # ⚠⚠ 2026-08-25 改掉了舊規則「要**每一台**都失敗才算壞」。
        #   舊規則的本意是避免誤報（有分身停在登入頁），但那種情況本來就會回
        #   NA（驗不了），不是 False —— 所以「有人 True 就算過」等於**讓通過的
        #   分身把失敗的蓋掉**。晶化搬家那次 5 台有 2 台因為讀到的垃圾剛好像
        #   合理值而通過，整項就印了綠燈。
        #   新規則：**只要有任何一台「驗得了而且驗不過」，就是壞。**
        #   驗不了（NA）不參與判定，只有全部都 NA 時才報「驗不了」。
        real = [o for o in oks if o is not None]     # 排除「這台驗不了」
        if any(o is False for o in real):
            mark, bucket = "✘", bad
        elif real:
            mark, bucket = "✔", None
        else:
            mark, bucket = "–", unknown
        if bucket is not None:
            bucket.append(key)
        lines.append(f"{mark} {key}")
        for (nm, _r), g in zip(per_client, [r.get(key) for _n, r in per_client]):
            if g:
                lines.append(f"      {nm}: {'✔' if g[0] else ('–' if g[0] is None else '✘')} {g[1]}")
        print(f"{mark} {key}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines + [""] + log), encoding="utf-8")
    print(f"\n通過 {len(names) - len(bad) - len(unknown)}／壞 {len(bad)}"
          f"／驗不了 {len(unknown)}　→ {OUT}")
    if bad:
        print("\n⛔ 這幾項有分身驗不過 —— 偏移八成搬家了（哪一台看報告）：")
        for k in bad:
            print(f"   · {k}")
        print("   重新定位的方法見 memory 的 patch-2026-08-11（找遊戲自己用"
              "那個偏移的程式碼當錨，tools/find_off.py）。")
    if unknown:
        print("\n– 這幾項這次驗不了（不代表壞）：" + "、".join(unknown))
    return 1 if (bad or (strict and unknown)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
