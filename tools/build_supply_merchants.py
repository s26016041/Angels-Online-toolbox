"""把各地圖的補給相關 NPC（買天使之翼的「藥水雜貨商人」、修裝的「維修*」）
   的**編號＋格子座標**抽成一張表。

    py tools\\build_supply_merchants.py [GAMEDATA資料夾]

輸出 `assets/supply_merchants.json`：
    { 場景編號: { "buy":  [NPC編號, tile_x, tile_y],
                  "repair":[NPC編號, tile_x, tile_y] } }
（沒有該類 NPC 的地圖就沒有那個鍵。）

為什麼這樣做
------------
* NPC 擺放座標**不在** setting 的 xml，而在 `GAMEDATA/map/MAP<場景>.MPC`（二進位地圖檔）。
* 補給商各城名字統一「藥水雜貨商人」；維修商名字**不統一**（維修奴隸/維修專家/維修技師…都含「維修」）。
  → **build 時用名字發掘、把「編號」抽出來；執行時用編號精準比對**（比讀名字字串穩、也符合
    使用者「用 ID 不用名字」的要求）。編號在實體 +0x1D8。

.MPC 記錄格式（用邱比特1862、藥水雜貨商人1878 校準，見 GAMEDATA.md）
    圖號 @ 編號位址−24（u32 高16）  X @ −20（16.16 高字÷32=tile_x）
    Y    @ 編號位址−16（16.16 高字÷32=rawY）  編號 @ 位址（u32）
    地圖寬/高 @ 檔頭 +4/+8。⚠ Y 上下翻：tile_y = 高度 − rawY。

⚠ 官方改版換地圖要重跑（同其他 build_*.py）。登記在 memory `self-supply-buy`。
"""
from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

BUY_NAME = "藥水雜貨商人"      # 賣天使之翼（商店 35）
REPAIR_KEY = "維修"           # 修裝 NPC 名字都含這兩個字（維修奴隸/專家/技師…）
BANK_KEY = "銀行"             # 銀行 NPC 名字都含「銀行」（銀行員工/專員/小姐/老闆…）
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "supply_merchants.json"


def _npc_names(setting: Path) -> dict[int, str]:
    t = (setting / "big5" / "string" / "str_npc.xml").read_text(encoding="utf-8")
    return {int(s) - 1200000000: n
            for s, n in re.findall(r'編號="(\d+)" 文字1="([^"]*)"', t)}


def _npc_shapes(setting: Path) -> set[int]:
    t = (setting / "base" / "npc.xml").read_text(encoding="utf-8")
    return {int(b) for _, b in re.findall(r'編號="(\d+)" 圖號="(\d+)"', t)}


def _npcs_in_map(path: Path, want_ids: set[int], shapes: set[int]):
    """回 {編號: (tile_x, tile_y)}，只含 want_ids 裡的 NPC。"""
    d = path.read_bytes()
    if d[:4] != b"MAP\0":
        return {}
    height = struct.unpack_from("<I", d, 8)[0]
    if not 0 < height < 2000:
        return {}
    out: dict[int, tuple[int, int]] = {}
    for p in range(24, len(d) - 4):
        nid = struct.unpack_from("<I", d, p)[0]
        if nid not in want_ids or nid in out:
            continue
        shp = struct.unpack_from("<I", d, p - 24)[0] >> 16
        if shp not in shapes:
            continue
        x = (struct.unpack_from("<I", d, p - 20)[0] >> 16) / 32
        rawy = (struct.unpack_from("<I", d, p - 16)[0] >> 16) / 32
        if 0 < x < 500 and 0 < rawy < height:
            out[nid] = (round(x), round(height - rawy))
    return out


def main() -> None:
    gamedata = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "GAMEDATA"
    setting, mapdir = gamedata / "setting", gamedata / "map"
    if not mapdir.is_dir():
        sys.exit(f"⛔ 找不到 {mapdir}")
    names = _npc_names(setting)
    shapes = _npc_shapes(setting)
    buy_ids = {nid for nid, nm in names.items() if nm == BUY_NAME}
    rep_ids = {nid for nid, nm in names.items() if REPAIR_KEY in nm}
    bank_ids = {nid for nid, nm in names.items() if BANK_KEY in nm}
    print(f"買（{BUY_NAME}）編號 {len(buy_ids)}；修（含「{REPAIR_KEY}」）編號 "
          f"{len(rep_ids)}；銀行（含「{BANK_KEY}」）編號 {len(bank_ids)}")

    table: dict[str, dict[str, list[int]]] = {}
    for path in list(mapdir.glob("*.MPC")) + list(mapdir.glob("*.mpc")):
        m = re.match(r"(?i)map0*(\d+)\.mpc$", path.name)
        if not m:
            continue
        scene = str(int(m.group(1)))
        entry: dict[str, list[int]] = {}
        for key, ids in (("buy", buy_ids), ("repair", rep_ids), ("bank", bank_ids)):
            found = _npcs_in_map(path, ids, shapes)
            if found:
                nid, (x, y) = next(iter(found.items()))    # 每張圖取一隻
                entry[key] = [nid, x, y]
        if entry:
            table[scene] = entry
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(table, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"→ {OUT}（{len(table)} 張圖）")
    for scene in sorted(table, key=int):
        e = table[scene]
        print(f"  場景 {scene}: 買={e.get('buy')} 修={e.get('repair')} "
              f"銀={e.get('bank')}")


if __name__ == "__main__":
    main()
