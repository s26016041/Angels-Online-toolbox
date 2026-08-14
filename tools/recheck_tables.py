"""查表體檢：寫死資料表 vs 遊戲記憶體，順便回答「setting 這次要不要重新解包」。

    py tools\\recheck_tables.py        # 要進遊戲（資料表進遊戲才載得進來）

## 為什麼

`assets/` 底下那幾張表是從遊戲資源包（GAMEDATA/setting）解包抄出來的。官方改版換掉
UPDATE.PAK 時它們**不會報錯，只會安靜過期** —— 射程錯 → 走位停太遠 → 零傷害；
趴趴GO 地圖編號錯 → 傳到別的地方。以前唯一的辦法是請使用者用 RPGViewer 重新
解包（GUI，自動化不了）。

★ 但**遊戲自己把 41 張資料表都載進記憶體了**，所以能直接拿記憶體當真相對帳。
  使用者的實務經驗「通常只有大更新新增內容才要重新解包」因此變成**可驗證的事實**。

## 41 張表怎麼找到的（不必寫死任何位址）

那 41 支「依 ID 查表」的函式共用同一句錯誤訊息
`Get %s Data Error, ID:%d >= MAX:%d`，`%s` 帶進去的常數字串就是**表名**。
所以：找那句格式字串的每一個參照 → 往前 40 bytes 找 `push <表名字串>`
→ 再往前 260 bytes 找 `A1 <表位址> / 8B 04 B0`（= `mov eax,[表]; mov eax,[eax+id*4]`）。
**字串位址會隨改版位移，內容不會** —— 這條路改版自動跟上。

⚠ 純讀取：不寫記憶體、不呼叫遊戲函式、不送封包。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                    # noqa: E402
from app.core.memory import MemoryScanner             # noqa: E402
from app.game import (bag, dailygift, energy, entity, itemname,   # noqa: E402
                      locate, monsters, skillcost, skills)
from app.paths import resource                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "reports", "table_recheck.txt")

FMT = b"Get %s Data Error, ID:%d >= MAX:%d"

# JumpMap 範本的欄位偏移。2026-08-11 拿 120 筆寫死表去掃「哪個偏移對全部都成立」
# 定出來的，三個欄位**各自只有一個偏移 120/120 全中**，不是猜的。
JM_OFF_SCENE, JM_OFF_X, JM_OFF_Y = 0x04, 0x10, 0x14


# ---------------------------------------------------------------------------
def find_tables(img: bytes, base: int) -> dict[str, int]:
    """{表名: 表指標位址}。做法見檔頭；41 張一次全拿。"""
    out: dict[str, int] = {}
    fpos = img.find(FMT)
    while fpos >= 0:
        fptr = (base + fpos).to_bytes(4, "little")
        i = img.find(fptr)
        while i >= 0:
            # 往前 40 bytes 找 push <可讀字串>
            name = None
            for j in range(max(0, i - 40), i):
                if img[j] != 0x68:
                    continue
                p = int.from_bytes(img[j + 1:j + 5], "little")
                if not base < p < base + len(img):
                    continue
                s = img[p - base:p - base + 32].split(b"\x00")[0]
                if 1 <= len(s) <= 24 and all(32 < c < 127 for c in s):
                    name = s.decode()
                    break
            if name:
                tab = None
                for k in range(max(0, i - 260), i):
                    if img[k] == 0xA1 and img[k + 5:k + 8] == b"\x8b\x04\xb0":
                        tab = int.from_bytes(img[k + 1:k + 5], "little")
                if tab and base < tab < base + len(img):
                    out.setdefault(name, tab)
            i = img.find(fptr, i + 1)
        fpos = img.find(FMT, fpos + 1)
    return out


def _u32(sc, addr):
    raw = sc._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def _sane(p) -> bool:
    return bool(p) and 0x10000 < p < 0x7FFF0000


# ---------------------------------------------------------------------------
# 各項對帳（回 (訊息, 過了嗎)）
# ---------------------------------------------------------------------------
def check_skill_range(sc, tabs, lines):
    """assets/skill_range.tsv.gz 的射程 vs 記憶體 Magic 範本（+0x50/+0x54）。"""
    skills._load_ranges()
    table = skills._ranges or {}
    ptr = tabs.get("Magic")
    tab = _u32(sc, ptr) if ptr else None
    if not _sane(tab):
        return "讀不到 Magic 表", False
    ptrs = sc._read_bytes(tab + 4, skillcost.MAX_SKILL_ID * 4)
    if not ptrs:
        return "讀不到 Magic 表內容", False
    ptrs = bytes(ptrs)
    same = diff = miss = 0
    for sid, want in sorted(table.items()):
        if not 1 <= sid <= skillcost.MAX_SKILL_ID:
            continue
        p = struct.unpack_from("<I", ptrs, (sid - 1) * 4)[0]
        if not _sane(p):
            miss += 1
            continue
        raw = sc._read_bytes(p + skillcost.OFF_SHOOT_RANGE, 8)
        if not raw or len(raw) < 8:
            miss += 1
            continue
        shoot, area = struct.unpack("<ii", bytes(raw))
        if want in (shoot, area):
            same += 1
        else:
            diff += 1
            if diff <= 40:
                lines.append(f"    技能 {sid}（{skills.name_of(sid)}）"
                             f"表={want} 記憶體 射程={shoot} 範圍={area}")
    return (f"{same} 對上、{diff} 對不上、{miss} 查不到（共 {len(table)}）",
            diff == 0 and same > 0)


def check_skill_secs(sc, tabs, lines):
    """assets/skills.tsv.gz 的 buff 持續時間（秒）vs 記憶體 Magic 範本（+0x100）。

    偏移出處：2026-08-14 拿全部 10516 筆寫死持續時間掃「哪個偏移全中」——
    只有 +0x100 10516/10516（skillcost.OFF_DURATION_SECS 的註解有全程記錄）。
    """
    table = skills._load()
    if not table:
        return "讀不到 assets/skills.tsv.gz", False
    ptr = tabs.get("Magic")
    tab = _u32(sc, ptr) if ptr else None
    if not _sane(tab):
        return "讀不到 Magic 表", False
    ptrs = sc._read_bytes(tab + 4, skillcost.MAX_SKILL_ID * 4)
    if not ptrs:
        return "讀不到 Magic 表內容", False
    ptrs = bytes(ptrs)
    same = diff = miss = 0
    for sid, sk in sorted(table.items()):
        if not 1 <= sid <= skillcost.MAX_SKILL_ID:
            continue
        p = struct.unpack_from("<I", ptrs, (sid - 1) * 4)[0]
        if not _sane(p):
            miss += 1
            continue
        raw = sc._read_bytes(p + skillcost.OFF_DURATION_SECS, 4)
        if not raw or len(raw) < 4:
            miss += 1
            continue
        got = struct.unpack("<i", bytes(raw))[0]
        if got == sk.secs:
            same += 1
        else:
            diff += 1
            if diff <= 40:
                lines.append(f"    技能 {sid}（{skills.name_of(sid)}）"
                             f"表={sk.secs}s 記憶體={got}s")
    return (f"{same} 對上、{diff} 對不上、{miss} 查不到（共 {len(table)}）",
            diff == 0 and same > 0)


def check_onlinegift(sc, tabs, lines):
    """dailygift.REWARD_IDS（安全退路）vs 記憶體 OnlineGift 表。

    正路已改成 dailygift.reward_ids() 現場讀；這裡驗退路那份沒過期＋
    表本身讀得到（兩條路輸出一致才放行，跟 channel.count() 那次同一套）。
    """
    got = dailygift.reward_ids(sc)
    raw = sc._read_bytes(dailygift.GIFT_TAB, 4)
    tab = struct.unpack("<I", bytes(raw))[0] if raw else 0
    if not _sane(tab):
        return "讀不到 OnlineGift 表（GIFT_TAB 搬家？）", False
    if got != dailygift.REWARD_IDS:
        lines.append(f"    記憶體={list(got)} 寫死退路={list(dailygift.REWARD_IDS)}")
        return ("獎勵格數變了 —— 正路會自動跟上，但 REWARD_IDS 退路要更新", False)
    return f"{len(got)} 格，記憶體與退路一致", True


def check_jumpmap(sc, tabs, lines):
    """assets/jumpmap.tsv 的 場景/座標 vs 記憶體 JumpMap 範本。"""
    rows = []
    try:
        with open(resource("assets/jumpmap.tsv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 4:
                    rows.append((int(p[0]), int(p[1]), int(p[2]), int(p[3])))
    except Exception as exc:                               # noqa: BLE001
        return f"讀不到 assets/jumpmap.tsv（{exc}）", False
    ptr = tabs.get("JumpMap")
    tab = _u32(sc, ptr) if ptr else None
    if not _sane(tab):
        return "讀不到 JumpMap 表", False
    same = diff = miss = 0
    for jid, sid, x, y in rows:
        p = _u32(sc, tab + jid * 4)
        if not _sane(p):
            miss += 1
            continue
        b = sc._read_bytes(p, JM_OFF_Y + 4)
        if not b or len(b) < JM_OFF_Y + 4:
            miss += 1
            continue
        b = bytes(b)
        got = (struct.unpack_from("<i", b, JM_OFF_SCENE)[0],
               struct.unpack_from("<i", b, JM_OFF_X)[0],
               struct.unpack_from("<i", b, JM_OFF_Y)[0])
        if got == (sid, x, y):
            same += 1
        else:
            diff += 1
            if diff <= 40:
                lines.append(f"    跳點 {jid}：表=(場景{sid}, {x}, {y}) "
                             f"記憶體=(場景{got[0]}, {got[1]}, {got[2]})")
    return (f"{same} 對上、{diff} 對不上、{miss} 查不到（共 {len(rows)}）",
            diff == 0 and same > 0)


def check_item_table(sc, tabs, lines):
    """背包每一件物品的種類 ID，都要在記憶體的 Item 表查得到範本。

    這同時驗三件事：Item 表指標、物品結構的種類 ID 欄位、還有「背包讀到的
    是不是真的物品」。查不到就是有一邊錯了。
    """
    ptr = tabs.get("Item")
    tab = _u32(sc, ptr) if ptr else None
    if not _sane(tab):
        return "讀不到 Item 表", False
    its = bag.items(sc)
    if not its:
        return "背包讀不到東西（不算表壞，但也驗不到）", False
    ok = bad = 0
    for i in its:
        p = _u32(sc, tab + i.type_id * 4)
        if _sane(p):
            ok += 1
        else:
            bad += 1
            if bad <= 20:
                lines.append(f"    背包第 {i.slot} 格 種類 {i.type_id}"
                             f"（{itemname.of(i.type_id)}）在 Item 表查不到")
    return f"{ok} 件查得到範本、{bad} 件查不到", bad == 0 and ok > 0


def check_decomp_whitelist(sc, tabs, lines):
    """energy.DECOMP_ITEMS（自動分解白名單）vs 記憶體 Item 範本。

    ⚠ 白名單本身是**使用者明令**的（「只能分解這兩個」，87381 刻意排除）——
    不能也不該改成自動認欄位。真正的風險是改版把編號**回收給別的東西**：
    送分解前執行時已驗「紙娃娃＋分解值>0＋沒時限」，這裡再對帳「範本的
    分類與分解值跟白名單記的一致」，編號被挪用第一時間就亮紅。
    """
    ptr = tabs.get("Item")
    tab = _u32(sc, ptr) if ptr else None
    if not _sane(tab):
        return "讀不到 Item 表", False
    bad = []
    for tid, val in sorted(energy.DECOMP_ITEMS.items()):
        p = _u32(sc, tab + tid * 4)
        if not _sane(p):
            bad.append(f"{tid}（{itemname.label(tid)}）查不到範本")
            continue
        kind = _u32(sc, p + bag.TMPL_KIND)
        dv = _u32(sc, p + bag.TMPL_PARAM2)
        if kind != bag.KIND_DOLL or dv != val:
            bad.append(f"{tid}（{itemname.label(tid)}）分類={kind} "
                       f"分解值={dv}，白名單記 紙娃娃/{val}")
    if bad:
        lines += [f"    {b}" for b in bad]
        return "白名單編號的範本對不上 —— 編號可能被改版挪用，先別自動分解", False
    names = "、".join(itemname.label(t) for t in sorted(energy.DECOMP_ITEMS))
    return f"{len(energy.DECOMP_ITEMS)} 個編號範本仍是紙娃娃＋分解值一致（{names}）", True


def check_npc_table(sc, tabs, lines):
    """怪物範本表：場上的怪要查得到，抽樣的等級／血量要落在合理範圍。"""
    idx = monsters.index_base(sc)
    if idx is None:
        return "讀不到怪物範本表", False
    lv, hp, n = [], [], 0
    for tid in range(1, 2000):
        info = monsters.info(sc, tid, idx)
        if info:
            n += 1
            lv.append(info.level)
            hp.append(info.max_hp)
    if n < 100:
        return f"只查得到 {n} 種（表壞了？）", False
    sane = max(lv) <= 5000 and max(hp) <= 2_000_000_000
    state, me, ents, _r, _e = entity.snapshot(sc)
    mobs = [e for e in ents if e.addr != me and e.type_id]
    missing = [e.name for e in mobs if monsters.info(sc, e.type_id, idx) is None]
    if missing:
        lines.append(f"    場上這幾隻查不到範本：{missing}")
    return (f"抽樣 1~1999 查得到 {n} 種（等級 ≤{max(lv)}、滿血 ≤{max(hp)}）；"
            f"場上 {len(mobs)} 隻、查不到 {len(missing)} 隻",
            sane and not missing)


def check_item_names(sc, tabs, lines):
    """物品名稱表（只驗**涵蓋率**：名稱在字串資源檔，記憶體裡沒有真相可比）。"""
    its = bag.items(sc)
    if not its:
        return "背包讀不到東西", False
    unnamed = [i.type_id for i in its if itemname.of(i.type_id).strip().isdigit()]
    if unnamed:
        lines.append(f"    這些種類查不到名字（會顯示成編號）：{sorted(set(unnamed))[:20]}")
    return (f"背包 {len(its)} 件，查不到名字 {len(set(unnamed))} 種"
            + ("　← 新道具，重新解包才會有" if unnamed else ""), True)


CHECKS = (
    ("skill_range.tsv.gz（技能射程）", check_skill_range,
     "⛔ 過期後果：走位停太遠 → 零傷害，完全不報錯", True),
    ("skills.tsv.gz（buff 持續時間）", check_skill_secs,
     "⛔ 過期後果：補 buff 的時間點算錯（太早浪費 MP／太晚裸奔）", True),
    ("在線獎勵格（OnlineGift）", check_onlinegift,
     "正路現場讀表自動跟上；這裡驗退路 REWARD_IDS 沒過期", True),
    ("自動分解白名單（DECOMP_ITEMS）", check_decomp_whitelist,
     "⛔ 過期後果：編號被改版挪用 → 自動分解拆錯東西（執行時另有三道驗證）", True),
    ("jumpmap.tsv（趴趴GO 傳送點）", check_jumpmap,
     "⛔ 過期後果：傳到錯的地方", True),
    ("Item 表 ↔ 背包物品", check_item_table,
     "驗 Item 表指標＋物品結構的種類 ID 欄位", True),
    ("Npc 表（怪物範本）", check_npc_table,
     "驗王／等級／滿血血量的來源", True),
    ("item_names（物品名稱）", check_item_names,
     "只影響顯示：查不到就顯示成編號，不會做錯事", False),
)


def main() -> int:
    pid = None
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" in w.class_name and " - " in w.title:
            pid = w.pid
            break
    if pid is None:
        print("⛔ 找不到**已經進遊戲**的分身 —— 資料表要進遊戲才載得進來。")
        return 2
    sc = MemoryScanner()
    sc.open(pid)
    try:
        locate.warm(sc)
        base = sc.module_base(locate.GAME_MODULE)
        info = next(m for m in sc.list_modules()
                    if m.name.lower() == locate.GAME_MODULE)
        img = bytes(sc._read_bytes(base, info.size) or b"")
        tabs = find_tables(img, base)
        lines = [f"pid={pid}　遊戲載進記憶體的資料表：{len(tabs)} 張", ""]
        print(f"遊戲載進記憶體的資料表：{len(tabs)} 張")

        hard_bad = False
        for name, fn, why, hard in CHECKS:
            lines.append(f"=== {name} ===")
            lines.append(f"  {why}")
            try:
                msg, ok = fn(sc, tabs, lines)
            except Exception as exc:                       # noqa: BLE001
                msg, ok = f"{type(exc).__name__}: {exc}", False
            lines += [f"  {'✔' if ok else '✘'} {msg}", ""]
            print(f"{'✔' if ok else '✘'} {name}：{msg}")
            if hard and not ok:
                hard_bad = True

        lines += ["=== 遊戲載進記憶體的資料表（要寫死之前先看這裡）===",
                  "  " + "、".join(f"{k}={v:#x}" for k, v in sorted(tabs.items())),
                  "", "⚠ 上面的位址每次改版都會位移 —— 要用哪張就照本檔的",
                  "  find_tables() 現場找，或照 skillcost.TABLE_PTR 進 locate.SIGS。"]
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")

        print()
        if hard_bad:
            print("⛔ setting 這次**要重新解包**：有會做錯事的表跟遊戲對不上了。")
            print("   D:\\RPGViewer 解包 → 覆蓋 setting\\ → 重跑 tools\\build_*.py"
                  " → py tools\\stamp_tables.py")
        else:
            print("✔ setting 這次**不必重新解包**：會做錯事的表都跟這一版對得上。")
            print("   （名稱類查不到只會顯示成編號。想熄掉掛機頁警示："
                  "py tools\\stamp_tables.py）")
        print(f"\n報告：{REPORT}")
        return 1 if hard_bad else 0
    finally:
        sc.close()


if __name__ == "__main__":
    sys.exit(main())
