"""覆蓋率稽核：專案裡每一筆「跟遊戲要資料」的方式，改版之後撐不撐得住？

    py tools\\coverage_audit.py          # 開著遊戲（登入畫面就夠）
    py tools\\coverage_audit.py --image reports\\angel_image.bin   # 離線

CLAUDE.md 的資料來源優先序是「記憶體 > AOB 定位 > 資源包抄本」。這支把整個
專案照那個優先序盤點一次，列出**還沒到位的**，依風險排序。它不修東西，只告訴
你哪裡有債。

## 盤四件事

1. **位址**（`0x400000~0xA00000` 的常數）→ 應該 100% 在 `locate.SIGS` 裡。
   ⚠ 靠「值落在模組範圍」判斷，上限值會誤報（`MAX_ENERGY = 0x989680` 中槍過），
   所以 `MAX_/MIN_/LIMIT_` 開頭的名字一律當數值。
2. **結構偏移**（`OFF_TARGET = 0x270` 這種）→ 三種狀態：
   * ✅ 有 AOB 特徵（`kind="off"`）—— 改版自動跟上
   * 🟡 有 `verify_offsets.py` 的不變量 —— 不會自動跟上，但改版後會**大聲**
   * ⚠ 兩者都沒有 —— 改版後**安靜地讀錯**，這就是待辦清單
   （2026-08-11 一次改版就搬了 6 個偏移，這格不是理論風險。）
3. **寫死的遊戲資料**（`assets/*.tsv*` 與程式裡的小表）→ 遊戲有沒有把同一份
   資料載進記憶體？有的話就該改成現場讀（CLAUDE.md 第一鐵則）。
   對照用的 41 張表是 `recheck_tables.find_tables()` 現場撈的。
4. **呼叫版面**（每支要 call 的函式吃幾個參數）→ 存一份基準，改版後 diff。
   ⚠ 這擋得住「參數個數變了」，**擋不住「參數語意變了」** ——
   2026-08-11 的 `usequickkey` 第三參數從「0 可以」變「0 一律失敗」就是後者，
   `ret N` 完全沒變。那種只有動作層實測抓得到。

⚠ 純讀取：不寫記憶體、不呼叫遊戲函式。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                    # noqa: E402
from app.core.memory import MemoryScanner             # noqa: E402
from app.game import locate                           # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "reports", "coverage_audit.txt")
BASELINE = os.path.join(ROOT, "reports", "callconv_baseline.json")

ADDR_LO, ADDR_HI = 0x400000, 0xA00000
NOT_ADDR = ("MAX_", "MIN_", "LIMIT_", "CAP_", "THRESHOLD_")
ADDR_ALLOW = {("injector", "CODE_LO"), ("injector", "CODE_HI")}

# ⚠⚠ 偏移**不一定叫 OFF_***：`SRV_BEGIN`、`M_ID`、`OBJ_UI_MGR`、`PENDING_OFF`
#   都是結構偏移。第一版用名字白名單（`^OFF_|STRIDE|…`）去撈，`SRV_BEGIN`
#   整個沒被看到 —— 盲點比錯誤更難發現，因為報告上什麼都沒有。
#   現在改成**先全收 app/game 的模組層整數常數，再明列排除了什麼**，
#   排除清單也印進報告，讓「不算偏移」這個判斷本身可以被檢查。
NOT_GAME_OFF = (
    "SCRATCH",          # 我們自己注入的暫存區，跟遊戲無關
    "PAGE_",            # WinAPI 記憶體保護旗標（PAGE_EXECUTE_READWRITE），不是遊戲結構
    "TIMEOUT", "_MS", "MS_", "_SEC", "TRIES", "RETRY", "INTERVAL", "SETTLE",
    "SPAN", "MAX", "MIN", "LIMIT", "CAP_", "NAME_MAX", "COUNT", "PAGES",
    "SLOTS", "SIZE", "VK_", "KIND_", "BIT_", "RUN_", "TILE", "FULL",
)
# 全名（模組.名字）精準排除 —— 名字太短、子字串比對會誤傷別人的才放這裡
# （例如 "_CODE" 會吃掉 SWITCH_CODE/SELECT_CODE 那些真封包代碼＝假覆蓋率）。
NOT_GAME_OFF_EXACT = (
    # move.py 跳板 stub 的版面（緊鄰的 _FLAG.._ESP 是元組賦值本來就掃不到）：
    # 我們自己 _stub_asm 產的緩衝區，跟遊戲結構無關、改版不會壞。
    "move._A6", "move._BUSY", "move._CODE",
)
# 這些模組不是「跟遊戲要資料」的層，裡面的數字是我們自己的設定
SKIP_MODULES = ("locate", "aob", "signatures", "navigate", "watcher",
                "itemname", "items", "skills", "tablestamp")

# 寫死的遊戲資料清單。第三欄＝遊戲記憶體裡有沒有同一份資料（41 張表的表名，
# 或 None）。⚠ 新增寫死表時要一起加進來，不然這支就漏掉它了。
HARDCODED_DATA = (
    ("assets/skill_range.tsv.gz", "技能射程／對象", "Magic",
     "已有 recheck_tables 對帳（19312 筆）"),
    ("assets/skills.tsv.gz", "buff 持續時間", "Magic",
     "⚠ 還沒對帳 —— 持續時間在 Magic 範本裡，可以補"),
    ("assets/jumpmap.tsv", "趴趴GO 傳送點", "JumpMap",
     "已有 recheck_tables 對帳（120 筆）"),
    ("assets/jumpmap_class.tsv", "傳送點分類名", "JumpMapClass",
     "⚠ 名稱在字串資源檔，記憶體只有編號 —— 只驗得到編號"),
    ("assets/item_names.tsv.gz", "物品名稱", None,
     "名稱不在記憶體（字串資源檔）；過期只會顯示成編號"),
    ("assets/skill_names.tsv.gz", "技能名稱", None, "同上"),
    ("items.py TIER_BALLS/SHOP_BALLS", "經驗球上限", None,
     "上限不在記憶體（實測過）—— 只能寫死"),
    ("scene.py SCENE_NAMES", "地圖名", None,
     "名稱不在記憶體；過期只會顯示成「場景 123」"),
    ("scene.py SAME_MAP_AS", "同圖別名 15 筆", None,
     "⚠ 過期 → 巡邏點被判成不同圖；還沒找到記憶體來源"),
    ("dailygift.py REWARD_IDS", "在線獎勵格數（退路）", "OnlineGift",
     "✅ 2026-08-14 正路已改 reward_ids() 現場讀表；這份只剩安全退路，"
     "recheck_tables 有對帳"),
    ("energy.py DECOMP_ITEMS", "自動分解白名單 2 個 ID", "Item",
     "⚠ 白名單是**使用者明令**（只能拆這兩個）——不該改自動認欄位；"
     "✅ 2026-08-14 起 recheck_tables 對帳範本（分類/分解值），編號被挪用會亮紅；"
     "執行時另有三道記憶體驗證（紙娃娃/分解值>0/沒時限）"),
    ("daily_tab.py WING/GUIDE/TOKEN_ITEM", "三個道具編號", "Item",
     "✅ 執行時已自防：兌換編號從記憶體反查、對不上就拒送（安全退化的範本）；"
     "2026-08-20 加導引之翼(82050)，daily_check.py 驗名字對得上資源包表"),
    ("bag.py FIRST_SLOT/LAST_SLOT", "背包格號範圍", None,
     "抄遊戲賣東西視窗的迴圈；改版擴充格數會無聲漏格"),
)

try:
    from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    _md = Cs(CS_ARCH_X86, CS_MODE_32)
except Exception:                                          # noqa: BLE001
    _md = None


# ---------------------------------------------------------------------------
def _const_of(node):
    """`X = 0x10` 與 `X = -0x50` 都要抓得到（負偏移是 UnaryOp，不是 Constant）。

    ⚠ 第一版漏掉負的 —— `player.OFF_LAST_SKILL = -0x50` 就這樣不在清單裡，
      而它一樣會隨改版搬家。
    """
    v = node.value
    if isinstance(v, ast.Constant) and isinstance(v.value, int):
        return v.value
    if (isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub)
            and isinstance(v.operand, ast.Constant)
            and isinstance(v.operand.value, int)):
        return -v.operand.value
    return None


def _justified(path: str, lineno: int) -> bool:
    """這個常數上面有沒有**說明為什麼不能更好**的註解？

    使用者的規矩是「一律用特徵搜尋，**除非真的無法或有更好的方式**」——
    那個「除非」必須寫下來。沒寫理由的例外就是債，不是設計。
    往上找連續的註解行（跳過空行），看有沒有交代限制的字眼。
    """
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    words = ("⚠", "⛔", "★", "不能", "無法", "沒辦法", "只能", "沒有更好",
             "改版", "會壞", "出處", "反組譯", "實測", "驗證")
    i = lineno - 2                       # lineno 是 1 起算，-2 = 上面那行
    seen = []
    while i >= 0 and len(seen) < 8:
        s = lines[i].strip()
        if not s:
            i -= 1
            continue
        if not s.startswith("#"):
            break
        seen.append(s)
        i -= 1
    return any(w in s for s in seen for w in words)


def scan_consts():
    """掃出 (位址常數, 偏移常數, 函式內裸數字, 被排除的) 四份清單。"""
    addrs, offs, inline, skipped = [], [], [], []
    app = os.path.join(ROOT, "app")
    for dirpath, _d, files in os.walk(app):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            mod = fn[:-3]
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
            except SyntaxError:
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                v = _const_of(node)
                if v is None:
                    continue
                for t in node.targets:
                    if not isinstance(t, ast.Name):
                        continue
                    rel = os.path.relpath(path, ROOT)
                    where = f"{rel}:{node.lineno}"
                    why = _justified(path, node.lineno)
                    if ADDR_LO <= v < ADDR_HI and not t.id.startswith(NOT_ADDR):
                        addrs.append((mod, t.id, v, where, why))
                    elif not (0 < abs(v) < 0x400000):
                        continue
                    elif "game" not in dirpath or mod in SKIP_MODULES:
                        continue          # 只看 app/game 這層跟遊戲要資料的
                    elif (any(k in t.id for k in NOT_GAME_OFF)
                          or f"{mod}.{t.id}" in NOT_GAME_OFF_EXACT):
                        skipped.append(f"{mod}.{t.id} = {v:#x}   {where}")
                    else:
                        offs.append((mod, t.id, v, where, why))
            # 函式內直接寫的 `+ 0x2FC` —— 這種連名字都沒有，最難維護
            for node in ast.walk(tree):
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                        and isinstance(node.right, ast.Constant)
                        and isinstance(node.right.value, int)
                        and 0x20 <= node.right.value < 0x400000):
                    inline.append((mod, node.right.value,
                                   f"{os.path.relpath(path, ROOT)}:{node.lineno}"))
    return addrs, offs, inline, skipped


def offsets_with_invariant() -> list[tuple[str, str]]:
    """`verify_offsets.py` 自己宣告的 `COVERS`（模組, 名字或前綴）。

    ⚠ 第一版是去 parse 那些 `put("…")` 的中文標題猜涵蓋範圍 —— **錯得很危險**：
      `entity.OFF_POS_X/Y` 被當成「entity 全部偏移都有驗」，一口氣多算 16 個
      假覆蓋，報告看起來很漂亮而實際上沒人在守。改成讓那支自己宣告，
      唯一的真相就在檢查旁邊。
    ⚠ 用 AST 讀那個字面值，**不 import** —— import 會把 app.game 整串載進來，
      而這支要能在「遊戲沒開／模組載不起來」時照樣跑，也不必為此把 tools/
      變成一個套件。
    """
    try:
        src = open(os.path.join(ROOT, "tools", "verify_offsets.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception:                                      # noqa: BLE001
        return []
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        names = ([node.target] if isinstance(node, ast.AnnAssign)
                 else node.targets)
        if not any(isinstance(t, ast.Name) and t.id == "COVERS" for t in names):
            continue
        try:
            return [tuple(x) for x in ast.literal_eval(node.value)]
        except Exception:                                  # noqa: BLE001
            return []
    # 退路：那支還沒宣告 COVERS 的話，只認完全寫死的 `模組.常數`
    return [(m.group(1), m.group(2)) for m in
            re.finditer(r'put\(\s*"[^"]*?([a-z_]+)\.([A-Z_][A-Z0-9_]*)', src)]


def has_invariant(mod: str, name: str, cov: list[tuple[str, str]]) -> bool:
    return any(m == mod and (name == p or (p.endswith("_")
                                           and name.startswith(p)))
               for m, p in cov)


# 「怎麼跟遊戲要資料」的手段，由穩到脆。key = 在原始碼裡找得到的樣子。
# ★ 這一段回答的是使用者問的「還有哪裡沒有用特徵搜尋或更好的方式」——
#   常數盤點只看得到位址與偏移，看不到「這個功能是用全掃找的」這種事。
MEANS = (
    ("指標路徑／AOB", r"locate\.warm|\.MGR_PTR|locate\.located", "最穩"),
    ("全記憶體掃描", r"_iter_regions|_read_region\(|aob\.scan\(", "慢＋會誤中；有指標路徑就別用"),
    ("vtable 掃描", r"_scan_vtables?\(", "比全掃準，但仍是掃描"),
    ("視窗標題", r"GetWindowTextW|w\.title|\.title\b", "遊戲改標題就壞；分流／帳號目前只有這條"),
    # ⚠ 別只比對字面路徑：實際寫法是 `resource(RANGE_FILE)`，第一版的
    #   `resource("assets/` 一個都沒中 → 報告顯示 0 檔，又是一個假數字。
    ("讀 assets 檔", r"resource\(|assets/", "資源包抄本，改版會安靜過期"),
    ("呼叫 Lua", r"lua\.(call|getfield|pcall)", "會跟遊戲自己的 Lua 搶堆疊"),
)


def scan_means() -> dict[str, list[str]]:
    """每種手段出現在哪些檔案（只掃 app/，不含 tools/）。"""
    out: dict[str, list[str]] = {k: [] for k, _p, _w in MEANS}
    app = os.path.join(ROOT, "app")
    for dirpath, _d, files in os.walk(app):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding="utf-8").read()
            except OSError:
                continue
            rel = os.path.relpath(path, ROOT)
            for name, pat, _why in MEANS:
                n = len(re.findall(pat, src))
                if n:
                    out[name].append(f"{rel}({n})")
    return out


def call_conv(img: bytes, base: int) -> dict[str, str]:
    """每支 fn 的呼叫版面：線性反組譯到第一個 ret，記 `ret N`。"""
    out: dict[str, str] = {}
    if _md is None:
        return out
    for s in locate.SIGS:
        if s.kind != "fn":
            continue
        sig, mask = locate._parse(s.pattern)
        m_full, m_only, _t = locate._auto_mask(sig, mask, s.known,
                                               base + 0x1000, base + len(img))
        off = (locate._find_unique(img, sig, m_full, s, base)
               or locate._find_unique(img, sig, m_only, s, base))
        if off is None:
            out[f"{s.module}.{s.attr}"] = "定位不到"
            continue
        for ins in _md.disasm(img[off:off + 0x600], base + off):
            if ins.mnemonic == "ret":
                out[f"{s.module}.{s.attr}"] = f"{ins.mnemonic} {ins.op_str}".strip()
                break
        else:
            out[f"{s.module}.{s.attr}"] = "（600 bytes 內沒有 ret）"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    args = ap.parse_args()

    base, img = 0x400000, b""
    if args.image:
        img = open(args.image, "rb").read()
    else:
        pid = next((w.pid for w in win.enumerate_windows(
            title_contains="Angels Online") if "_MIDAGEONL_" in w.class_name),
            None)
        if pid is None:
            print("⛔ 找不到遊戲（登入畫面就夠），或用 --image 指定映像。")
            return 2
        sc = MemoryScanner()
        sc.open(pid)
        try:
            base = sc.module_base(locate.GAME_MODULE)
            info = next(m for m in sc.list_modules()
                        if m.name.lower() == locate.GAME_MODULE)
            img = bytes(sc._read_bytes(base, info.size) or b"")
            locate.warm(sc)
        finally:
            sc.close()
    if not img:
        print("⛔ 讀不到映像。")
        return 2

    addrs, offs, inline, skipped = scan_consts()
    sig_addr = {(s.module, s.attr) for s in locate.SIGS if s.kind != "off"}
    sig_off = {(s.module, s.attr) for s in locate.SIGS if s.kind == "off"}
    inv = offsets_with_invariant()

    lines = ["=== ① 位址（應該 100% 有 AOB 特徵）==="]
    bad_addr = [a for a in addrs
                if (a[0], a[1]) not in sig_addr and (a[0], a[1]) not in ADDR_ALLOW]
    lines.append(f"  共 {len(addrs)} 個，沒有特徵的 {len(bad_addr)} 個")
    for mod, name, v, where, _why in bad_addr:
        lines.append(f"  ⚠ {mod}.{name} = {v:#x}   {where}")

    # ⚠ 偏移 vs 代碼／列舉分不出來的話，報告會一直吵。用一條**寫在報告裡、
    #   可以被質疑**的粗規則分桶：值 >= 0x20 當疑似結構偏移，否則當代碼／列舉
    #   （`attack.SELECT_CODE = 0xC`、`bag.GRADE_FINE = 3` 那種）。
    #   代碼不會因為「物件版面搬家」而錯，只有官方重新定義才會 —— 那種
    #   AOB 也救不了，只能靠實測。⚠ 這條規則會誤判（`DOLL_WORN_LAST = 0xF9`
    #   是格號不是偏移），所以真正的訊號是「有沒有寫理由」那一欄。
    def _is_offset(name: str, v: int) -> bool:
        # 名字比數值可靠：`OFF_`/`TMPL_`/`SRV_`/`M_`/`OBJ_` 開頭、或 `_OFF`
        # 結尾、含 STRIDE 的一律當偏移（`entity.OFF_ID` 是偏移不是編號）；
        # 含 CODE/SLOT/GRADE/_TYPE/_ITEM/_ID 的當代碼或編號
        # （`channel.SWITCH_CODE = 0x47` 是封包代碼，跟版面無關）；
        # 都不像就退回看值大小。
        if re.match(r"^(OFF_|TMPL_|SRV_|M_|OBJ_|VT_OFF|MEMBERS_|PENDING_)", name) \
                or name.endswith("_OFF") or "STRIDE" in name:
            return True
        if re.search(r"(CODE|SLOT|GRADE|_TYPE$|_ITEM|_ID$|_LAST$|_FIRST$)", name):
            return False
        return abs(v) >= 0x20

    codes = [r for r in offs if not _is_offset(r[1], r[2])]
    offs = [r for r in offs if _is_offset(r[1], r[2])]

    lines += ["", "=== ② 結構偏移（改版真的會搬家）==="]
    auto, loud, silent, silent_nowhy = [], [], [], []
    for mod, name, v, where, why in offs:
        key = f"{mod}.{name}"
        if (mod, name) in sig_off:
            auto.append((key, v, where))
        elif has_invariant(mod, name, inv):
            loud.append((key, v, where))
        elif why:
            silent.append((key, v, where))
        else:
            silent_nowhy.append((key, v, where))
    lines.append(f"  共 {len(offs)} 個：AOB 自動跟上 {len(auto)}、"
                 f"有不變量會大聲 {len(loud)}、只有註明理由 {len(silent)}、"
                 f"**連理由都沒寫 {len(silent_nowhy)}**")
    lines.append("  ⚠ 「有沒有理由」只看**緊貼在常數上面**的註解 —— 寫在模組"
                 "檔頭或更上面的區塊註解看不到，所以這一格會**高估**。")
    lines.append("    這是刻意的：理由要寫在用的人一眼看得到的地方才有用。")
    for tag, group in (("✅ AOB 自動跟上", auto), ("🟡 有不變量會大聲", loud),
                       ("△ 沒保護但有註明為什麼", silent),
                       ("⚠ 沒保護、也沒寫理由", silent_nowhy)):
        lines.append(f"  --- {tag} ---")
        for key, v, where in sorted(group):
            lines.append(f"    {key} = {v:#x}   {where}")

    nowhy_code = [r for r in codes if not r[4]]
    lines += ["", "=== ②d 代碼／列舉（值 < 0x20；版面搬家不會影響它們）===",
              f"  共 {len(codes)} 個，其中 {len(nowhy_code)} 個沒寫出處／理由",
              "  ⚠ 這種 AOB 救不了（不是位址也不是偏移），只有官方重新定義才會錯，",
              "    要靠動作層實測才抓得到 —— 但**出處一定要寫**（反組譯哪一段來的）。"]
    for mod, name, v, where, why in sorted(codes):
        lines.append(f"    {'  ' if why else '⚠ '}{mod}.{name} = {v:#x}   {where}")

    lines += ["", "=== ②a 判定成「不是遊戲結構偏移」而排除的 ===",
              "  （排除規則本身也要能被檢查 —— 看到不該排的就改 NOT_GAME_OFF）"]
    for s in sorted(skipped):
        lines.append(f"    {s}")

    lines += ["", "=== ②b 函式內的裸數字（連名字都沒有，最難維護）==="]
    lines.append(f"  共 {len(inline)} 處")
    byfile: dict[str, int] = {}
    for _mod, _v, where in inline:
        byfile[where.split(":")[0]] = byfile.get(where.split(":")[0], 0) + 1
    for f, n in sorted(byfile.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {f:<34} {n} 處")

    lines += ["", "=== ②c 怎麼跟遊戲要資料（由穩到脆）==="]
    means = scan_means()
    for name, _pat, why in MEANS:
        hits = means.get(name) or []
        lines.append(f"  {name}（{why}）：{len(hits)} 個檔")
        for h in hits:
            lines.append(f"    {h}")

    lines += ["", "=== ③ 寫死的遊戲資料（記憶體有沒有同一份？）==="]
    can_move = [r for r in HARDCODED_DATA if r[2]]
    lines.append(f"  共 {len(HARDCODED_DATA)} 份，其中 {len(can_move)} 份"
                 "記憶體裡有對應的表")
    for path, what, table, note in HARDCODED_DATA:
        mark = "★可改讀記憶體" if table else "只能寫死"
        lines.append(f"  {mark:<14} {path:<38} {what}"
                     + (f"　← 記憶體表 {table}" if table else ""))
        lines.append(f"                 {note}")

    lines += ["", "=== ④ 呼叫版面（參數個數變了會抓到）==="]
    conv = call_conv(img, base)
    old = {}
    if os.path.exists(BASELINE):
        try:
            old = json.load(open(BASELINE, encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            old = {}
    changed = [(k, old.get(k), v) for k, v in conv.items()
               if k in old and old[k] != v]
    if not old:
        lines.append(f"  第一次跑，建立基準（{len(conv)} 支）")
    else:
        lines.append(f"  比對基準：{len(conv)} 支，變了 {len(changed)} 支")
        for k, a, b in changed:
            lines.append(f"  ⚠ {k}：{a} → {b}")
    for k, v in sorted(conv.items()):
        lines.append(f"    {k:<26} {v}")
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    json.dump(conv, open(BASELINE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    lines += ["", "⚠ ④ 擋得住「參數個數變了」，擋不住「參數語意變了」——",
              "  2026-08-11 的 usequickkey 第三參數從 0 可以變成 0 一律失敗，",
              "  ret 完全沒變。那種只有動作層實測抓得到。"]

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    print(f"① 位址　　{len(addrs)} 個，沒有特徵 {len(bad_addr)} 個")
    for mod, name, v, where, _why in bad_addr:
        print(f"   ⚠ {mod}.{name} = {v:#x}  {where}")
    print(f"② 偏移　　{len(offs)} 個：AOB {len(auto)}、不變量 {len(loud)}、"
          f"有理由沒保護 {len(silent)}、**連理由都沒寫 {len(silent_nowhy)}**")
    for key, v, where in sorted(silent_nowhy)[:12]:
        print(f"   ⚠ {key} = {v:#x}  {where}")
    if len(silent_nowhy) > 12:
        print(f"   …還有 {len(silent_nowhy) - 12} 個，看報告")
    print(f"②d 代碼／列舉 {len(codes)} 個（值<0x20，AOB 救不了），"
          f"沒寫出處 {len(nowhy_code)} 個")
    print(f"②b 函式內裸數字 {len(inline)} 處")
    print("②c 取得資料的手段："
          + "、".join(f"{k} {len(v)} 檔" for k, v in means.items() if v))
    print(f"③ 寫死資料 {len(HARDCODED_DATA)} 份，其中 {len(can_move)} 份"
          "記憶體裡有對應的表（★ 可以改成現場讀）")
    print(f"④ 呼叫版面 {len(conv)} 支"
          + ("（第一次跑，已建立基準）" if not old
             else f"，變了 {len(changed)} 支"))
    print(f"\n報告：{REPORT}")
    # 「連理由都沒寫」也算不過 —— 使用者的規矩是「一律用特徵，除非真的無法或
    # 有更好的方式」，那個「除非」沒寫下來就不算例外，是債。
    return 1 if (bad_addr or changed or silent_nowhy) else 0


if __name__ == "__main__":
    sys.exit(main())
