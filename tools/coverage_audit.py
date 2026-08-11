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

# 偏移常數的名字長什麼樣（AST 掃出來之後再過這個篩）
OFF_NAME = re.compile(r"(^OFF_|_OFF$|_OFF_|STRIDE|^VT_OFF|^ITEM_)")
# ⚠ `SCRATCH_OFF` 是**我們自己注入的暫存區**裡的位置（`mover.scratch()+這個`），
#   不是遊戲的結構偏移 —— 遊戲改版跟它一點關係都沒有。第一版把 4 個算成
#   「沒保護」，是假債。
NOT_GAME_OFF = ("SCRATCH",)

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
    ("dailygift.py REWARD_IDS", "在線獎勵格數", "OnlineGift",
     "★ 可以改讀記憶體的 OnlineGift 表"),
    ("energy.py DECOMP_ITEMS", "自動分解白名單 2 個 ID", "Item",
     "★★ 後果最嚴重的一份；Item 表在記憶體，值得找出能認出「充能-小背包」的欄位"),
    ("daily_tab.py WING_ITEM/TOKEN_ITEM", "兩個道具編號", "Item",
     "★ 可以拿 Item 表交叉驗證"),
    ("bag.py FIRST_SLOT/LAST_SLOT", "背包格號範圍", None,
     "抄遊戲賣東西視窗的迴圈；改版擴充格數會無聲漏格"),
)

try:
    from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    _md = Cs(CS_ARCH_X86, CS_MODE_32)
except Exception:                                          # noqa: BLE001
    _md = None


# ---------------------------------------------------------------------------
def scan_consts():
    """掃出 (位址常數, 偏移常數) 兩份清單。"""
    addrs, offs = [], []
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
                if not (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, int)):
                    continue
                v = node.value.value
                for t in node.targets:
                    if not isinstance(t, ast.Name):
                        continue
                    where = f"{os.path.relpath(path, ROOT)}:{node.lineno}"
                    if ADDR_LO <= v < ADDR_HI and not t.id.startswith(NOT_ADDR):
                        addrs.append((mod, t.id, v, where))
                    elif (0 < v < 0x400000 and OFF_NAME.search(t.id)
                          and not any(k in t.id for k in NOT_GAME_OFF)):
                        offs.append((mod, t.id, v, where))
    return addrs, offs


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

    addrs, offs = scan_consts()
    sig_addr = {(s.module, s.attr) for s in locate.SIGS if s.kind != "off"}
    sig_off = {(s.module, s.attr) for s in locate.SIGS if s.kind == "off"}
    inv = offsets_with_invariant()

    lines = ["=== ① 位址（應該 100% 有 AOB 特徵）==="]
    bad_addr = [a for a in addrs
                if (a[0], a[1]) not in sig_addr and (a[0], a[1]) not in ADDR_ALLOW]
    lines.append(f"  共 {len(addrs)} 個，沒有特徵的 {len(bad_addr)} 個")
    for mod, name, v, where in bad_addr:
        lines.append(f"  ⚠ {mod}.{name} = {v:#x}   {where}")

    lines += ["", "=== ② 結構偏移（改版真的會搬家）==="]
    auto, loud, silent = [], [], []
    for mod, name, v, where in offs:
        key = f"{mod}.{name}"
        if (mod, name) in sig_off:
            auto.append((key, v, where))
        elif has_invariant(mod, name, inv):
            loud.append((key, v, where))
        else:
            silent.append((key, v, where))
    lines.append(f"  共 {len(offs)} 個：AOB 自動跟上 {len(auto)}、"
                 f"有不變量會大聲 {len(loud)}、**兩者都沒有 {len(silent)}**")
    for tag, group in (("✅ AOB", auto), ("🟡 不變量", loud), ("⚠ 沒保護", silent)):
        lines.append(f"  --- {tag} ---")
        for key, v, where in sorted(group):
            lines.append(f"    {key} = {v:#x}   {where}")

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
    for mod, name, v, where in bad_addr:
        print(f"   ⚠ {mod}.{name} = {v:#x}  {where}")
    print(f"② 偏移　　{len(offs)} 個：AOB {len(auto)}、不變量 {len(loud)}、"
          f"沒保護 {len(silent)}")
    for key, v, where in sorted(silent)[:12]:
        print(f"   ⚠ {key} = {v:#x}  {where}")
    if len(silent) > 12:
        print(f"   …還有 {len(silent) - 12} 個，看報告")
    print(f"③ 寫死資料 {len(HARDCODED_DATA)} 份，其中 {len(can_move)} 份"
          "記憶體裡有對應的表（★ 可以改成現場讀）")
    print(f"④ 呼叫版面 {len(conv)} 支"
          + ("（第一次跑，已建立基準）" if not old
             else f"，變了 {len(changed)} 支"))
    print(f"\n報告：{REPORT}")
    return 1 if (bad_addr or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
