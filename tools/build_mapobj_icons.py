"""把場景物件的圖解成縮圖 → `assets/mapobj_icons.zip`。

    py tools\\build_mapobj_icons.py [GAMEDATA 資料夾] [--max 64]

## 為什麼

副本腳本製作那頁列「附近可互動的物件」時，光有名字還不夠 ——
「人型系雕像01」跟「人型系雕像02」長什麼樣還是看不出來（使用者 2026-09-02
要求：滑鼠移到清單某一條就跳個框顯示它的圖）。

## 為什麼是縮圖不是原圖

`GAMEDATA\\shape\\stage` 整包 **225MB**，塞不進發布檔。所以：

* 只收**點得到的**物件（資源包標了 `SP_ATTRIB_HITTEST`，3598 筆）——
  那才是會出現在清單裡的東西。
* 每張縮到最長邊 `--max`（預設 64）像素，PNG（RGBA）。

.SHP 的格式與解碼共用 `tools/build_item_icons.py` 的 `decode_shp()`
（同一套自家格式，2026-08-28 解的，含「一列可以有好幾段」那個坑）。

⚠ 官方改版新增物件要重跑；查不到圖就退化成不顯示（不會顯示錯的圖）。
"""
from __future__ import annotations

import gzip
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_item_icons import decode_shp                # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAMES = ROOT / "assets" / "mapobj_names.tsv.gz"
OUT = ROOT / "assets" / "mapobj_icons.zip"


def load_wanted() -> list[tuple[int, str]]:
    """(外觀編號, 圖檔相對路徑)，只收「點得到」而且有圖檔名的。"""
    out = []
    with gzip.open(NAMES, "rt", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) >= 4 and c[2] == "HITTEST" and c[3]:
                out.append((int(c[0]), c[3]))
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    size = 64
    if "--max" in sys.argv:
        size = int(sys.argv[sys.argv.index("--max") + 1])
    shape = gamedata / "shape"
    if not shape.is_dir():
        print(f"⛔ 找不到 {shape}")
        return 2
    if not NAMES.is_file():
        print(f"⛔ 先跑 py tools\\build_mapobj.py 產生 {NAMES.name}")
        return 2

    from PIL import Image                                    # noqa: PLC0415

    wanted = load_wanted()
    print(f"要收 {len(wanted)} 個點得到的物件，縮圖最長邊 {size}px")
    index: dict[int, str] = {}
    blobs: dict[str, bytes] = {}
    missing = bad = 0
    for seq, rel in wanted:
        stem = rel.split("/")[-1]
        path = shape / Path(rel.replace("/", "\\"))
        for cand in (path.with_suffix(".SHP"), path.with_suffix(".shp")):
            if cand.is_file():
                path = cand
                break
        else:
            missing += 1
            continue
        key = f"{stem}.png"
        if key not in blobs:
            try:
                w, h, rgba = decode_shp(path.read_bytes())
                img = Image.frombytes("RGBA", (w, h), rgba)
                img.thumbnail((size, size), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "PNG", optimize=True)
                blobs[key] = buf.getvalue()
            except Exception:                                # noqa: BLE001
                bad += 1
                continue
        index[seq] = stem
    print(f"　解出 {len(blobs)} 張圖，涵蓋 {len(index)} 個編號"
          f"（檔案找不到 {missing}、解不開 {bad}）")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("index.tsv",
                   "\n".join(f"{k}\t{v}" for k, v in sorted(index.items())))
        for name, data in sorted(blobs.items()):
            z.writestr(name, data)
    print(f"→ {OUT}（{OUT.stat().st_size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
