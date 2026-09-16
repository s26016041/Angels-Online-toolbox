"""把**每張地圖的可走格**從遊戲資源包抽成一個檔 → `assets/map_grids.bin.gz`。

    py tools\\build_map_grids.py [GAMEDATA資料夾]
    py tools\\build_map_grids.py --check       # 只比對，過期回傳碼 1

## 這張表是幹嘛的

`app/game/terrain.py` 讀的是**記憶體裡當下那張圖**——人沒站上去就沒有。
但「趴趴GO 回巡邏點時，一張圖有好幾個傳送點，要挑**走過去最短**的那個」
需要的是**目標那張圖**的地形，而人還在別張圖上。所以要一份離線的。

## 資料在哪（2026-09-16 解開、實機逐格對帳過）

`GAMEDATA/map/*.mpc`，二進位：

    @0x00  "MAP\\0"
    @0x04  寬(格) u32        @0x08  高(格) u32
    @0x0C  版本：0x10006 → 每格 7 bytes / 0x10005 → 每格 6 bytes
    @0x10  32   @0x14  32    ← tile 大小（座標 = 格×32 的由來）
    @0x34  地形段結束 offset  ==  0x60 + 寬*高*每格
    @0x60  地形段：列主序，每列 寬 格；**牆 = 每格 byte[2] & 3**

場景編號 → 檔名在 `setting/base/stage.xml` 的 `地圖檔="map\\map026.mpc"`。

⚠⚠ **每格幾 bytes 不是常數**——151 張裡 30 張是 6 bytes。一律讀 @0x0C 版本欄，
   公式對不上就**整張跳過**（寧可沒有，不要一張假地圖）。
⚠⚠ **.mpc 的列序是上下翻的**：`遊戲的 ty = 高 − 1 − mpc_row`。這裡**存檔時就翻正**，
   讀取端拿到的跟記憶體那張同一個方向。

✅ 對帳（2026-09-16，5 台實機純讀）：千夜魔宮(126) 380×250、無限塔(110) 320×300
   跟 `terrain.load()` 讀到的**逐格比對 191,000 格、不同 0**。
⚠ 對到的兩張都是 per=7；per=6 那 30 張還沒實機站上去驗過。

## 輸出格式（`assets/map_grids.bin.gz`，gzip）

    magic  b"AOMG1"
    u16    地圖檔數 N
    N ×  { u8 檔名長 + 檔名(utf-8) + u16 寬 + u16 高 + bitmap }
           bitmap = 列主序、每格 1 bit（1=可走）、**已翻正**，每列各自補滿到整個 byte
    u16    場景數 M
    M ×  { u32 場景編號 + u16 檔案索引 }

每列各自對齊 byte 是故意的：讀取端可以一列一列切，不必整張攤平。

⚠ 官方改版新增／改動地圖要重跑這支（登記在 memory `items-table-maintenance`
  與 `.claude/commands/_patchCheck.md` 第 7 步）。
"""
from __future__ import annotations

import gzip
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "map_grids.bin.gz"
MAGIC = b"AOMG1"

HDR = 0x60                       # 地形段起點（151 張全數成立）
FLAG_OFF = 2                     # 每格的第 3 個 byte
BLOCK_MASK = 3                   # & 3 != 0 → 牆
PER_BY_VER = {0x10006: 7, 0x10005: 6}

STAGE_ROW = re.compile(r'<場景 編號="(\d+)"[^>]*?地圖檔="map\\([^"]+)"')


def read_mpc(path: Path) -> tuple[int, int, list[bytearray]] | None:
    """回 (寬, 高, 每列的可走位元組)；版面對不上回 None（整張跳過）。

    列序在這裡就翻正成**遊戲的 ty**（見檔頭）。
    """
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

    stride = (w + 7) // 8
    rows: list[bytearray] = []
    for mpc_row in range(h):
        base = HDR + mpc_row * w * per
        bits = bytearray(stride)
        for tx in range(w):
            if not (d[base + tx * per + FLAG_OFF] & BLOCK_MASK):
                bits[tx >> 3] |= 1 << (tx & 7)
        rows.append(bits)
    rows.reverse()                       # ★ mpc 列序上下翻 → 翻正成遊戲的 ty
    return w, h, rows


def build(gamedata: Path) -> bytes:
    mapdir = gamedata / "map"
    stage = (gamedata / "setting" / "base" / "stage.xml").read_text("utf-8")
    lower = {p.name.lower(): p for p in mapdir.iterdir() if p.is_file()}

    order: list[str] = []                # 檔名（小寫）→ 索引
    index: dict[str, int] = {}
    blobs: list[bytes] = []
    scenes: list[tuple[int, int]] = []
    skipped: list[tuple[int, str, str]] = []

    for sid_s, fn in STAGE_ROW.findall(stage):
        sid = int(sid_s)
        key = fn.lower()
        if key not in index:
            path = lower.get(key)
            if path is None:
                skipped.append((sid, fn, "檔案不存在"))
                continue
            got = read_mpc(path)
            if got is None:
                skipped.append((sid, fn, "版面對不上"))
                continue
            w, h, rows = got
            index[key] = len(order)
            order.append(fn)
            name = fn.encode("utf-8")
            blobs.append(struct.pack("<B", len(name)) + name
                         + struct.pack("<HH", w, h) + b"".join(rows))
        scenes.append((sid, index[key]))

    out = [MAGIC, struct.pack("<H", len(blobs))]
    out.extend(blobs)
    out.append(struct.pack("<H", len(scenes)))
    out.extend(struct.pack("<IH", s, i) for s, i in scenes)
    raw = b"".join(out)

    print(f"地圖檔 {len(order)} 張、場景 {len(scenes)} 個"
          f"（未壓縮 {len(raw) / 1024:.0f} KB）")
    if skipped:
        print(f"   ⚠ 跳過 {len(skipped)} 個場景（那張圖會退回舊做法）：")
        for sid, fn, why in skipped[:12]:
            print(f"      場景 {sid} {fn} —— {why}")
    return gzip.compress(raw, 9)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    if not (gamedata / "map").is_dir():
        print(f"找不到 {gamedata}\\map —— 請給 GAMEDATA 資料夾路徑。")
        return 2

    data = build(gamedata)
    if check:
        old = OUT.read_bytes() if OUT.exists() else b""
        # ⚠ gzip 每次輸出的位元組不保證一樣，要比**內容**（見 memory patch-2026-09-06）
        same = old and gzip.decompress(old) == gzip.decompress(data)
        print("表是最新的。" if same else "⚠ 過期了，請重跑本工具（不加 --check）。")
        return 0 if same else 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(data)
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")

    # 抽驗：實機對帳過的兩張
    sys.path.insert(0, str(ROOT))
    from app.game import mapfile                      # noqa: E402
    mapfile.reload()
    for sid, want in ((126, (380, 250)), (110, (320, 300))):
        g = mapfile.grid_of(sid)
        got = (g.w, g.h) if g else None
        walk = sum(sum(r) for r in g.open) if g else 0
        print(f"   驗證 場景{sid} → {got} {'✔' if got == want else '✘'}"
              f"　可走 {walk} 格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
