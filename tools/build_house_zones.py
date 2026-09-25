r"""把**每張地圖可以擺（展示）房子的格子**從遊戲資源包抽成一個檔 → `assets/house_zones.json`。

    py tools\build_house_zones.py [GAMEDATA資料夾]
    py tools\build_house_zones.py --check       # 只比對，過期回傳碼 1

## 這張表是幹嘛的

自動生產勾「房屋製作」、房子沒擺出來時，工具箱要自己挑一格站上去按「展示」
（`house.register`）。展示封包**不帶座標**、伺服器用角色當下的位置，所以挑格子
這件事只能靠這張表。`app/game/housezone.py` 讀它。

## 資料在哪（2026-09-25 從 `.mpc` 找到，使用者同意先這樣認定）

`GAMEDATA/map/*.mpc` 地形段每格的第 3 個 byte（`build_map_grids.py` 那個
「牆＝& 3」的同一個 byte）**bit 0x20** 只出現在 5 大主城（3/26/29/38/88）＋
天使住宅區(370)，其他 157 張圖全 0；已知的 3 棟房子（和風林×2、棕櫚基地）
所在格全部是 0x20 → 當成「可展示房屋區」。
⚠ **沒有反組譯證實**：客戶端沒有這個檢查（「距離附近的房屋太近」訊息 2457 在
  客戶端找不到引用＝伺服器判的）。擺不下去由呼叫端換格子重試。

座標：跟 `build_map_grids.py` 同一件事 —— `.mpc` 列序上下翻，**格 y = 高 − 1 − 檔案列**。

## 輸出格式（JSON）

    {"<場景編號>": {"w": 寬, "h": 高, "file": "map088.mpc",
                    "runs": [[y, x起, x迄(含)], ...]}}

只收有可擺格的場景。

⚠ 官方改版新增／改動地圖要重跑這支（登記在 memory `items-table-maintenance`
  與 `.claude/commands/_patchCheck.md` 第 7 步）。
"""
from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "house_zones.json"

HDR = 0x60
FLAG_OFF = 2                     # 每格的第 3 個 byte（同 build_map_grids）
ZONE_BIT = 0x20                  # 可展示房屋區（見檔頭）
PER_BY_VER = {0x10006: 7, 0x10005: 6}
STAGE_ROW = re.compile(r'<場景 編號="(\d+)"[^>]*?地圖檔="map\\([^"]+)"')


def read_mpc(path: Path):
    """回 (寬, 高, [[y, x0, x1], ...])；版面對不上回 None。"""
    d = path.read_bytes()
    if len(d) < HDR or d[:4] != b"MAP\0":
        return None
    w, h = struct.unpack_from("<II", d, 4)
    ver = struct.unpack_from("<I", d, 0x0C)[0]
    end = struct.unpack_from("<I", d, 0x34)[0]
    per = PER_BY_VER.get(ver)
    if per is None or not (0 < w <= 4096 and 0 < h <= 4096):
        return None
    if HDR + w * h * per != end or len(d) < end:
        return None
    runs = []
    for mpc_row in range(h):
        y = h - 1 - mpc_row                  # ★ 上下翻成遊戲的 ty
        base = HDR + mpc_row * w * per
        x0 = None
        for x in range(w + 1):
            on = x < w and bool(d[base + x * per + FLAG_OFF] & ZONE_BIT)
            if on and x0 is None:
                x0 = x
            elif not on and x0 is not None:
                runs.append([y, x0, x - 1])
                x0 = None
    runs.sort()
    return w, h, runs


def build(gamedata: Path) -> dict:
    mapdir = gamedata / "map"
    stage = (gamedata / "setting" / "base" / "stage.xml").read_text("utf-8")
    lower = {p.name.lower(): p for p in mapdir.iterdir() if p.is_file()}
    cache: dict[str, object] = {}
    out: dict[str, dict] = {}
    for sid_s, fn in STAGE_ROW.findall(stage):
        key = fn.lower()
        if key not in cache:
            path = lower.get(key)
            cache[key] = read_mpc(path) if path else None
        got = cache[key]
        if not got or not got[2]:
            continue
        w, h, runs = got
        out[sid_s] = {"w": w, "h": h, "file": fn, "runs": runs}
    cells = sum(r[2] - r[1] + 1 for v in out.values() for r in v["runs"])
    print(f"有可擺房屋格的場景 {len(out)} 個：{sorted(int(k) for k in out)}，共 {cells} 格")
    return out


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has(data: dict, sid: int, x: int, y: int) -> bool:
    return any(r[0] == y and r[1] <= x <= r[2]
               for r in data.get(str(sid), {}).get("runs", []))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    if not (gamedata / "map").is_dir():
        print(f"找不到 {gamedata}\\map —— 請給 GAMEDATA 資料夾路徑。")
        return 2
    data = build(gamedata)
    text = dump(data)
    if check:
        old = OUT.read_text("utf-8") if OUT.exists() else ""
        same = old == text
        print("表是最新的。" if same else "⚠ 過期了，請重跑本工具（不加 --check）。")
        return 0 if same else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, "utf-8")
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    # 抽驗：2026-09-25 實機讀到的三棟房子所在格（和風林(24,58)/(28,59)、棕櫚基地(184,81)）
    ok = all(_has(data, s, x, y) for s, x, y in ((29, 24, 58), (29, 28, 59), (88, 184, 81)))
    print(f"   驗證 已知房子三格都在可擺區 {'✔' if ok else '✘'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
