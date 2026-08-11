"""寫死資料表 vs 遊戲記憶體：改版之後**不必重新解包**就能知道表有沒有過期。

    py tools\\recheck_tables.py        # 遊戲開著（登入畫面就夠？不夠，要進遊戲）

## 為什麼

`assets/` 底下那幾張表是從遊戲資源包（SETTING）解包抄出來的。官方改版換掉
UPDATE.PAK 時，它們**不會報錯，只會安靜過期** —— 射程錯 → 走位停太遠 → 零傷害；
趴趴GO 地圖編號錯 → 傳到別的地方。以前唯一的辦法是請使用者用 RPGViewer 重新
解包再重跑 build 工具（那是 GUI，自動化不了）。

★ 但**遊戲自己把 41 張資料表都載進記憶體了**（那 41 支「依 ID 查表」的函式共用
同一句錯誤訊息 `Get %s Data Error, ID:%d >= MAX:%d`，%s 就是表名：Npc、Magic、
JumpMap、Item、OnlineGift、Exchange…）。所以能直接拿記憶體當真相去對帳。
使用者的實務經驗也是「通常只有大更新新增內容才需要重新解包」——
這支就是把那句話變成**可驗證的事實**而不是猜測。

⚠ 名稱類（物品名／技能名／地圖名）不在這些表裡，在字串資源檔，記憶體查不到 ——
那幾張只能等解包。好消息是它們過期只會「顯示成編號」，不會做錯事。

純讀取，不寫入、不呼叫遊戲函式。
"""
from __future__ import annotations

import gzip
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                    # noqa: E402
from app.core.memory import MemoryScanner             # noqa: E402
from app.game import locate, skillcost, skills        # noqa: E402
from app.paths import resource                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "reports", "table_recheck.txt")

# 遊戲載進記憶體的資料表（tools 用 scratchpad/list_tables.py 從那 41 支查表函式
# 的錯誤訊息字串抽出來的）。⚠ 這些位址跟別的一樣會隨改版位移 —— 需要用到哪一張
# 就照 skillcost.TABLE_PTR 的做法進 locate.SIGS（錨在那支查表函式尾巴 push 的
# 表名字串），**不要直接抄下面的數字**。這裡列出來是給「這份資料要不要寫死」
# 這個問題一個現成答案：清單裡有的，就別抄資源包。
IN_MEMORY_TABLES = (
    "Achievement ActivityURL Adv CatchPet Class Collection Crops Crown "
    "CustomAdv Doll Drop Exchange ExchangeGroup Furniture Gcontrib Item "
    "Itemset Jeweleffect JumpMap JumpMapClass LoginGift Magic Make Mall Mat "
    "Npc OnlineGift Pet Petaspect Petskill Petstar Prestige Quest Roulette "
    "Shop Skill Stage Theme Treasuremap Word"
).split()

# JumpMap 範本的欄位偏移。2026-08-11 用「拿 120 筆寫死表去對，看哪個偏移對全部
# 都成立」定出來的，三個欄位各自**只有一個**偏移全中（120/120），不是猜的。
JUMPMAP_TABLE_PTR = 0x0098FD54     # ⚠ 會位移；只給這支對帳工具用，不進產品路徑
JM_OFF_SCENE, JM_OFF_X, JM_OFF_Y = 0x04, 0x10, 0x14


def _u32(sc, addr):
    raw = sc._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def check_skill_range(sc, lines) -> tuple[str, bool]:
    """assets/skill_range.tsv.gz 的射程 vs 記憶體的技能範本（+0x50/+0x54）。"""
    skills._load_ranges()
    table = skills._ranges or {}
    tab = _u32(sc, skillcost.TABLE_PTR)
    if not tab or not skillcost._sane_ptr(tab):
        return "讀不到技能範本表（還沒進遊戲？）", False
    ptrs = sc._read_bytes(tab + 4, skillcost.MAX_SKILL_ID * 4)
    if not ptrs:
        return "讀不到技能範本表內容", False
    ptrs = bytes(ptrs)
    same = diff = miss = 0
    for sid, want in sorted(table.items()):
        if not 1 <= sid <= skillcost.MAX_SKILL_ID:
            continue
        p = struct.unpack_from("<I", ptrs, (sid - 1) * 4)[0]
        if not skillcost._sane_ptr(p):
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
    return (f"{same} 對上、{diff} 對不上、{miss} 記憶體查不到（共 {len(table)}）",
            diff == 0 and same > 0)


def check_jumpmap(sc, lines) -> tuple[str, bool]:
    """assets/jumpmap.tsv 的 場景/座標 vs 記憶體的 JumpMap 範本。"""
    try:
        rows = []
        with open(resource("assets/jumpmap.tsv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 4:
                    rows.append((int(p[0]), int(p[1]), int(p[2]), int(p[3])))
    except Exception as exc:                               # noqa: BLE001
        return f"讀不到 assets/jumpmap.tsv（{exc}）", False
    tab = _u32(sc, JUMPMAP_TABLE_PTR)
    if not tab or not 0x10000 < tab < 0x7FFF0000:
        return "讀不到 JumpMap 表（還沒進遊戲？）", False
    same = diff = miss = 0
    for jid, scene, x, y in rows:
        p = _u32(sc, tab + jid * 4)
        if not p or not 0x10000 < p < 0x7FFF0000:
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
        if got == (scene, x, y):
            same += 1
        else:
            diff += 1
            if diff <= 40:
                lines.append(f"    跳點 {jid}：表=(場景{scene}, {x}, {y}) "
                             f"記憶體=(場景{got[0]}, {got[1]}, {got[2]})")
    return (f"{same} 對上、{diff} 對不上、{miss} 記憶體查不到（共 {len(rows)}）",
            diff == 0 and same > 0)


CHECKS = (
    ("assets/skill_range.tsv.gz（技能射程）", check_skill_range,
     "過期後果：走位停太遠 → 零傷害，完全不報錯"),
    ("assets/jumpmap.tsv（趴趴GO 傳送點）", check_jumpmap,
     "過期後果：傳到錯的地方"),
)

# 驗不了的（名稱類在字串資源檔，記憶體裡沒有）
CANT_CHECK = (
    ("assets/item_names.tsv.gz（物品名）", "過期只會顯示成編號，不會做錯事"),
    ("assets/skill_names.tsv.gz（技能名）", "同上"),
    ("assets/jumpmap_class.tsv（傳送點分類名）", "同上"),
    ("assets/skills.tsv.gz（buff 持續時間）",
     "⚠ 還沒做對帳；持續時間在記憶體的 Magic 範本裡，之後可以補"),
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
        lines = [f"pid={pid}", ""]
        allok = True
        for name, fn, why in CHECKS:
            lines.append(f"=== {name} ===")
            lines.append(f"  {why}")
            try:
                msg, ok = fn(sc, lines)
            except Exception as exc:                       # noqa: BLE001
                msg, ok = f"{type(exc).__name__}: {exc}", False
            lines.append(f"  {'✔' if ok else '✘'} {msg}")
            lines.append("")
            print(f"{'✔' if ok else '✘'} {name}：{msg}")
            allok = allok and ok
        lines += ["=== 記憶體驗不了的（只能等解包）===", ""]
        for name, why in CANT_CHECK:
            lines.append(f"  · {name} —— {why}")
        lines += ["", "=== 遊戲載進記憶體的資料表（要寫死之前先看這裡）===",
                  "  " + "、".join(IN_MEMORY_TABLES)]
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print()
        if allok:
            print("✔ 會做錯事的那幾張表都跟這一版遊戲對得上 —— **不必重新解包**。")
            print("  （剩下的是名稱類，過期只會顯示成編號。想熄掉掛機頁的警示："
                  "py tools\\stamp_tables.py）")
        else:
            print("⛔ 有表跟遊戲對不上了 —— 這次要重新解包 SETTING、重跑 "
                  "tools\\build_*.py，再蓋章。")
        print(f"\n報告：{REPORT}")
        return 0 if allok else 1
    finally:
        sc.close()


if __name__ == "__main__":
    sys.exit(main())
