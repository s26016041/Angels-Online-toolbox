"""從資源包抽「物品限制旗標」表 → assets/item_flags.tsv.gz。

    py tools\\build_item_flags.py          # 專案根目錄那份 GAMEDATA
    py tools\\build_item_flags.py --check  # 只比對，不寫檔（改版體檢用）

來源＝ `GAMEDATA/setting/base/item*.xml` 每筆 `<道具 …>` 的三個屬性（有寫「是」才算）：
    不可存倉庫="是"  → NO_BANK  (1)
    不可交易="是"    → NO_TRADE (2)
    裝備綁定="是"    → BIND     (4)
輸出每行 `種類ID<TAB>旗標位元<TAB>原型介面`，**全部道具都寫**（旗標 0 也寫）——這樣讀取端才分得出
「查得到而且沒限制」跟「表裡沒這筆（新道具，還沒重跑）」；後者一律當不能存（安全退化）。
第三欄「原型介面」＝圖示編號（給不在這台背包的清單項目畫圖用；沒有那個屬性寫 0）。

用途：公會（社團）倉庫的存放清單過濾（`app/game/itemflags.py`／`guildbank.py`）。
使用者 2026-09-06 定：綁定／不可交易的東西也不能存公會倉庫。
⚠ `綁定次數` 那欄的意思沒驗證過（1/2/3/5），這裡**不看它**——要用先弄清楚再加。

改版：官方新增道具要重跑（跟 build_item_names 同一份 GAMEDATA），見
.claude/commands/_patchCheck.md 第 7 步與 memory items-table-maintenance。
"""
from __future__ import annotations

import gzip
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SETTING = ROOT / "GAMEDATA" / "setting"
OUT = ROOT / "assets" / "item_flags.tsv.gz"
ITEM_FILES = ["item.xml"] + [f"item{i}.xml" for i in range(2, 10)]

NO_BANK, NO_TRADE, BIND = 1, 2, 4
ATTR_BITS = (("不可存倉庫", NO_BANK), ("不可交易", NO_TRADE), ("裝備綁定", BIND))


def extract() -> dict[int, tuple[int, int]]:
    """{種類ID: (旗標位元, 原型介面)}"""
    out: dict[int, tuple[int, int]] = {}
    for fn in ITEM_FILES:
        p = SETTING / "base" / fn
        if not p.exists():
            continue
        text = io.open(p, encoding="utf-8").read()
        for m in re.finditer(r"<道具 ([^>]*?)/?>", text):
            a = m.group(1)
            idm = re.search(r'編號="(\d+)"', a)
            if not idm:
                continue
            tid = int(idm.group(1))
            if tid in out:                      # 同編號後面的檔不蓋前面的（跟 supply_shop 同規矩）
                continue
            bits = 0
            for key, bit in ATTR_BITS:
                if re.search(r'%s="是"' % key, a):
                    bits |= bit
            im = re.search(r'原型介面="(\d+)"', a)
            out[tid] = (bits, int(im.group(1)) if im else 0)
    return out


def load_existing() -> dict[int, tuple[int, int]]:
    if not OUT.exists():
        return {}
    got: dict[int, tuple[int, int]] = {}
    with gzip.open(OUT, "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] and parts[1]:
                got[int(parts[0])] = (int(parts[1]), int(parts[2]) if len(parts) >= 3 else 0)
    return got


def main() -> int:
    check_only = "--check" in sys.argv
    if not (SETTING / "base" / "item.xml").exists():
        print(f"⛔ 找不到 {SETTING / 'base' / 'item.xml'} —— 先把 GAMEDATA 解包放到專案根目錄")
        return 2
    table = extract()
    if not table:
        print("⛔ item*.xml 一筆道具都沒抽到（格式變了？）—— 不出表")
        return 1
    n_bank = sum(1 for v, _ in table.values() if v & NO_BANK)
    n_trade = sum(1 for v, _ in table.values() if v & NO_TRADE)
    n_bind = sum(1 for v, _ in table.values() if v & BIND)
    n_ok = sum(1 for v, _ in table.values() if v == 0)
    n_icon = sum(1 for _, ic in table.values() if ic)
    print(f"item*.xml 共 {len(table)} 筆：不可存倉庫 {n_bank}、不可交易 {n_trade}、"
          f"裝備綁定 {n_bind}；三項都沒有（公會倉庫可存）{n_ok}；有原型介面（圖示編號）{n_icon}")
    old = load_existing()
    if old:
        added = sorted(set(table) - set(old))
        gone = sorted(set(old) - set(table))
        changed = sorted(t for t in set(table) & set(old) if table[t] != old[t])
        print(f"跟現有表比：新增 {len(added)}、消失 {len(gone)}、旗標變了 {len(changed)}")
        if check_only:
            return 0 if not (added or gone or changed) else 1
    elif check_only:
        print("現有表不存在")
        return 1
    with gzip.open(OUT, "wt", encoding="utf-8", newline="\n") as f:
        for tid in sorted(table):
            bits, icon = table[tid]
            f.write(f"{tid}\t{bits}\t{icon}\n")
    print(f"✔ 寫出 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
