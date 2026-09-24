"""把**每張地圖的傳點範圍**從遊戲資源包抽成一個檔 → `assets/map_portals.json`。

    py tools\\build_map_portals.py [GAMEDATA資料夾]
    py tools\\build_map_portals.py --check       # 只比對，過期回傳碼 1

## 這張表是幹嘛的

自動戰鬥用地形圖 A* 自己算路，**不知道哪裡是傳點** → 追怪／回巡邏點時路線
剛好擦過傳點就被傳到別張圖發呆（使用者 2026-09-24 回報）。
`app/game/mapportal.py` 讀這張表，掛機那份地形快取把傳點範圍當牆（見
`terrain.Cache(avoid_portals=True)`）。

## 資料在哪（2026-09-24 從 `.mpc` 解出來）

`GAMEDATA/map/*.mpc` 檔頭 u32 欄位（跟 `build_map_grids.py` 同一個檔）：

    @0x20  事件 XML 的 offset   @0x24 長度      （UTF-8，前面多 4 bytes 長度）
    @0x4C  事件範圍段的 offset  @0x50 筆數
           每筆：u32 事件編號 + u32 點數 n + n × (u32 x, u32 y)  ← 像素座標

事件 XML 裡「**觸發="3"**（踩上去）且 **動作 編號="3"**（傳送：參數＝目標場景、落點）」
的事件就是傳點（游標="3" 是傳送門游標；節慶版有幾個游標不是 3 的，一律以動作為準）。

座標換算：格 x = 像素 x / 32；**格 y = 高 − 像素 y / 32**（`.mpc` 上下翻，
跟地形段同一件事，見 `build_map_grids.py`）。

⚠ 點怎麼圍成範圍**沒有反組譯確認**：大多數是 3 點，也有 1／2／4／6／9 點
  （6、9 點看起來是 2、3 個三角形共用一個事件）。這裡**只存原始點**，
  怎麼放大範圍由 `mapportal.py` 決定（寧可多繞幾格，不要擦邊踩進去）。
⚠ 節慶版地圖檔（XMAS／HALLOWEEN／sakura）有 37 個傳點事件沒有範圍資料；
  場景一律照 `stage.xml` 指定的那個檔（跟 `map_grids` 同一套）。

## 輸出格式（JSON）

    {"<場景編號>": {"w": 寬, "h": 高, "file": "map147.mpc",
                    "portals": [{"ev": 事件編號, "to": 目標場景, "pts": [[x, y], ...]}]}}

只收有傳點的場景。

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
OUT = ROOT / "assets" / "map_portals.json"

HDR = 0x60
OFF_EVT_XML = 0x20               # 事件 XML offset（下一格是長度）
OFF_EVT_AREA = 0x4C              # 事件範圍段 offset（下一格是筆數）
TILE = 32
MAX_PTS = 4096                   # 合理性上限（讀到亂數就整張跳過）

STAGE_ROW = re.compile(r'<場景 編號="(\d+)"[^>]*?地圖檔="map\\([^"]+)"')
EVENT = re.compile(r'<事件 編號="(\d+)"[^>]*>(.*?)</事件>', re.S)
# 踩上去（觸發=3）→ 傳送（動作 3，第一個參數＝目標場景）
WARP = re.compile(r'<觸發器 [^>]*觸發="3"[^>]*>\s*<動作 編號="3">\s*'
                  r'<參數 數值="(\d+)"/>')


def read_mpc(path: Path):
    """回 (寬, 高, [(事件, 目標場景, [(x, y) 格]), ...])；版面對不上回 None。"""
    d = path.read_bytes()
    if len(d) < HDR or d[:4] != b"MAP\0":
        return None
    w, h = struct.unpack_from("<II", d, 4)
    if not (0 < w <= 4096 and 0 < h <= 4096):
        return None
    xo, xn = struct.unpack_from("<II", d, OFF_EVT_XML)
    ao, an = struct.unpack_from("<II", d, OFF_EVT_AREA)
    if not (HDR <= xo and xo + xn <= len(d)) or not HDR <= ao <= len(d):
        return None
    text = d[xo:xo + xn].decode("utf-8", "replace")
    warps = {}
    for m in EVENT.finditer(text):
        t = WARP.search(m.group(2))
        if t:
            warps[int(m.group(1))] = int(t.group(1))
    areas = {}
    p = ao
    for _ in range(an):
        if p + 8 > len(d):
            return None
        ev, n = struct.unpack_from("<II", d, p)
        p += 8
        if not 0 < n <= MAX_PTS or p + 8 * n > len(d):
            return None
        areas[ev] = [struct.unpack_from("<II", d, p + 8 * i) for i in range(n)]
        p += 8 * n
    if p != len(d):
        return None                      # 段尾對不上＝格式不是我們以為的那樣
    out = []
    for ev in sorted(warps):
        pts = areas.get(ev)
        if not pts:
            continue                     # 有傳點事件卻沒範圍（節慶版）→ 不收
        out.append((ev, warps[ev],
                    [(round(x / TILE, 2), round(h - y / TILE, 2)) for x, y in pts]))
    return w, h, out


def build(gamedata: Path) -> dict:
    mapdir = gamedata / "map"
    stage = (gamedata / "setting" / "base" / "stage.xml").read_text("utf-8")
    lower = {p.name.lower(): p for p in mapdir.iterdir() if p.is_file()}
    cache: dict[str, object] = {}
    out: dict[str, dict] = {}
    skipped = []
    for sid_s, fn in STAGE_ROW.findall(stage):
        key = fn.lower()
        if key not in cache:
            path = lower.get(key)
            cache[key] = read_mpc(path) if path else None
            if cache[key] is None:
                skipped.append((sid_s, fn))
        got = cache[key]
        if not got or not got[2]:
            continue
        w, h, ports = got
        out[sid_s] = {"w": w, "h": h, "file": fn,
                      "portals": [{"ev": ev, "to": to, "pts": [list(q) for q in pts]}
                                  for ev, to, pts in ports]}
    n = sum(len(v["portals"]) for v in out.values())
    print(f"有傳點的場景 {len(out)} 個、傳點 {n} 個")
    if skipped:
        print(f"   ⚠ 跳過 {len(skipped)} 個場景（檔案不存在／版面對不上，那張圖不繞傳點）：")
        for sid, fn in skipped[:12]:
            print(f"      場景 {sid} {fn}")
    return out


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    if not (gamedata / "map").is_dir():
        print(f"找不到 {gamedata}\\map —— 請給 GAMEDATA 資料夾路徑。")
        return 2
    text = dump(build(gamedata))
    if check:
        old = OUT.read_text("utf-8") if OUT.exists() else ""
        same = old == text
        print("表是最新的。" if same else "⚠ 過期了，請重跑本工具（不加 --check）。")
        return 0 if same else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, "utf-8")
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    # 抽驗：集會所(147) 三個傳點，目標 140／146／148（2026-09-24 對過 XML）
    got = sorted(p["to"] for p in json.loads(text).get("147", {}).get("portals", []))
    print(f"   驗證 場景147 → 目標 {got} {'✔' if got == [140, 146, 148] else '✘'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
