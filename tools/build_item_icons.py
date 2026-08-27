"""把遊戲資源包裡的道具圖示轉成一包 PNG → `assets/item_icons.zip`。

    py tools\\build_item_icons.py [GAMEDATA/setting 資料夾]

## 為什麼要自己解圖檔

遊戲的圖示是自家格式 `GAMEDATA\\shape\\item\\i####.SHP`（4575 個檔、8.9MB），
Qt 讀不了。這支把它們解成 PNG 打包起來，程式執行時 `app/game/itemicon.py`
直接從包裡拿 bytes 餵 QPixmap。

★ **圖示編號（原型介面）本身是從記憶體讀的**（範本 `+0x00`，遊戲自己的
  `geticon` 綁定就是讀這裡），所以改版換了道具的圖，編號會自動跟上；
  這包只提供「編號 → 圖」那一段。
⚠ **官方改版新增道具要重跑這支**，不然新道具沒有圖（會退化成不顯示圖示，
  不會做錯事）。流程見 `.claude/commands/_patchCheck.md` 第 7 步。

## .SHP 格式（2026-08-28 自己解的，附自我驗證）

    +0x00  'TLHS'
    +0x08  型別：2 或 4（跟調色盤欄一起決定每點幾 bytes，見下表）
    +0x10  透明色　+0x14 寬　+0x18 高
    +0x28  像素數　+0x2C 像素資料起點
    +0x40  調色盤起點（256 × RGB565 = 512 bytes）；**0 ＝ 沒有調色盤**
    +0x50  每列一個 dword，指向該列的「段清單」
    段：8 bytes ＝ [(起始x << 16) | 長度, 像素資料位移]，一列可以有好幾段，
        讀到 0xFFFF0000 就是這一列結束。

像素怎麼存（拿 4575 個檔統計「(檔長 − 資料起點) ÷ 像素數」定出來的，
每一組都剛好整齊落在 1.00 / 2.00）：

    型別 2 ＋ 有調色盤 → 每點 1 byte：調色盤索引　　　　　　（4353 個）
    型別 2 ＋ 沒調色盤 → 每點 2 bytes：直接 RGB565　　　　　（ 179 個）
    型別 4 ＋ 有調色盤 → 每點 2 bytes：低位＝索引、高位＝alpha（   5 個）

⚠ `BASE.SHP` / `MASK.SHP`（型別 4 沒調色盤）是格子背景不是道具圖，沒解。

⚠⚠ **一列不是只有一段** —— 第一版這樣寫，4568 個檔裡有 3542 個至少有一列
   多段，圖會缺一塊（法杖的火焰整片不見）而且不會報錯。
✅ 自我驗證：每個檔「所有段長度總和 == 標頭 +0x28 的像素數」，4568/4568 相符。

## 包裡有什麼

    index.tsv     原型介面編號 <TAB> 圖檔名（不含副檔名）
    i####.png     RGBA，沒畫到的地方是全透明

同一張圖被好幾個編號共用，所以圖按檔名存、編號另外查 index。
⚠ 有 3 個編號在不同 itemicon 檔裡指到不同圖（說法互相矛盾）—— **整個丟掉
  不收**，寧可沒圖也不要顯示錯的圖（照 CLAUDE.md 的安全退化原則）。
"""
from __future__ import annotations

import io
import re
import struct
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
except Exception:                                          # noqa: BLE001
    pass

from PIL import Image                                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "item_icons.zip"
MAGIC = b"TLHS"
ROW_END = 0xFFFF0000
KIND_INDEX = 2                  # 標頭 +0x08：索引色（或無調色盤時的 RGB565）
KIND_INDEX_ALPHA = 4            # 索引 ＋ alpha（每點 2 bytes）

ICON_ROW = re.compile(r'<icon id="(\d+)" dir="(.*?)" normal="(.*?)"')


# --- .SHP 解碼 -----------------------------------------------------------
def _rgb565(v: int) -> tuple[int, int, int]:
    r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def decode_shp(data: bytes) -> tuple[int, int, bytes]:
    """(寬, 高, RGBA)；格式不認得就丟 ValueError（呼叫端跳過該檔）。"""
    if data[:4] != MAGIC:
        raise ValueError("不是 TLHS 圖檔")
    kind = struct.unpack_from("<I", data, 0x08)[0]
    _key, w, h = struct.unpack_from("<3I", data, 0x10)
    if not (0 < w <= 512 and 0 < h <= 512):
        raise ValueError(f"寬高不合理 {w}x{h}")
    pal_off = struct.unpack_from("<I", data, 0x40)[0]
    pal = ([_rgb565(v) for v in struct.unpack_from("<256H", data, pal_off)]
           if pal_off else None)
    if kind == KIND_INDEX and pal:
        mode, step = "index", 1
    elif kind == KIND_INDEX and not pal:
        mode, step = "rgb565", 2
    elif kind == KIND_INDEX_ALPHA and pal:
        mode, step = "index_alpha", 2
    else:
        raise ValueError(f"型別 {kind}＋{'有' if pal else '沒'}調色盤 還沒解")
    rows = struct.unpack_from(f"<{h}I", data, 0x50)
    out = bytearray(w * h * 4)                 # 預設全透明
    for y, roff in enumerate(rows):
        off = roff
        while True:
            head = struct.unpack_from("<I", data, off)[0]
            if head == ROW_END:
                break
            src = struct.unpack_from("<I", data, off + 4)[0]
            off += 8
            x0, n = head >> 16, head & 0xFFFF
            for i in range(n):
                x = x0 + i
                if x >= w or src + i * step + step > len(data):
                    break
                alpha = 255
                if mode == "index":
                    r, g, b = pal[data[src + i]]
                elif mode == "index_alpha":
                    r, g, b = pal[data[src + i * 2]]
                    alpha = data[src + i * 2 + 1]
                else:
                    r, g, b = _rgb565(
                        struct.unpack_from("<H", data, src + i * 2)[0])
                p = (y * w + x) * 4
                out[p:p + 4] = bytes((r, g, b, alpha))
    return w, h, bytes(out)


def pixel_count_ok(data: bytes) -> bool:
    """自我驗證：所有段長度總和要等於標頭記的像素數。"""
    try:
        _key, _w, h = struct.unpack_from("<3I", data, 0x10)
        want = struct.unpack_from("<I", data, 0x28)[0]
        rows = struct.unpack_from(f"<{h}I", data, 0x50)
        tot = 0
        for roff in rows:
            off = roff
            while True:
                head = struct.unpack_from("<I", data, off)[0]
                if head == ROW_END:
                    break
                tot += head & 0xFFFF
                off += 8
        return tot == want
    except Exception:                                      # noqa: BLE001
        return False


# --- 編號 → 圖檔名 --------------------------------------------------------
def read_index(setting: Path) -> tuple[dict[int, str], list[int]]:
    """itemicon*.xml → {原型介面: 圖檔名}，以及「說法互相矛盾」的編號清單。"""
    merged: dict[int, str] = {}
    bad: set[int] = set()
    files = sorted(setting.glob("itemicon*.xml"))
    if not files:
        raise SystemExit(f"找不到 {setting}\\itemicon*.xml —— 資源包路徑對嗎？")
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        n = 0
        for m in ICON_ROW.finditer(text):
            iid, folder, name = int(m.group(1)), m.group(2), m.group(3)
            if folder.strip("\\/").lower() != "item":
                continue           # 目前資源包裡全是 \item\，別的先不收
            n += 1
            if iid in merged and merged[iid] != name:
                bad.add(iid)
            merged.setdefault(iid, name)
        print(f"  {path.name:<18} {n} 筆")
    for iid in bad:
        merged.pop(iid, None)
    return merged, sorted(bad)


def main() -> int:
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "GAMEDATA" / "setting"
    shape = setting.parent / "shape" / "item"
    if not shape.is_dir():
        raise SystemExit(f"找不到圖檔資料夾 {shape}")

    index, conflict = read_index(setting)
    print(f"編號 {len(index)} 個" + (f"（丟掉 {len(conflict)} 個說法矛盾的："
                                     f"{conflict}）" if conflict else ""))

    on_disk = {p.name.lower(): p for p in shape.glob("*.SHP")}
    print(f"圖檔 {len(on_disk)} 個 @ {shape}")

    done: dict[str, bytes] = {}
    missing: list[int] = []
    failed: dict[str, str] = {}
    unverified: list[str] = []
    for iid, name in sorted(index.items()):
        key = f"{name}.shp".lower()
        path = on_disk.get(key)
        if path is None:
            missing.append(iid)
            continue
        if key in done or key in failed:
            continue
        raw = path.read_bytes()
        if not pixel_count_ok(raw):
            unverified.append(path.name)      # 自我驗證沒過：照樣試，但要講
        try:
            w, h, rgba = decode_shp(raw)
        except ValueError as exc:
            failed[key] = f"{path.name}（{exc}）"
            continue
        buf = io.BytesIO()
        Image.frombytes("RGBA", (w, h), rgba).save(buf, "PNG", optimize=True)
        done[key] = buf.getvalue()

    usable = {iid: n for iid, n in index.items() if f"{n}.shp".lower() in done}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # PNG 本身已經壓過了，再壓一次只是浪費時間 → 用 ZIP_STORED
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_STORED) as z:
        z.writestr("index.tsv",
                   "".join(f"{i}\t{n}\n" for i, n in sorted(usable.items())))
        for key, png in sorted(done.items()):
            z.writestr(key[:-4] + ".png", png)

    print(f"\n收進 {len(done)} 張圖、{len(usable)} 個編號查得到 → {OUT}"
          f"（{OUT.stat().st_size / 1024 / 1024:.1f} MB）")
    if missing:
        print(f"⚠ {len(missing)} 個編號的圖檔不在資源包裡（會沒有圖示）："
              f"{missing[:10]}{' …' if len(missing) > 10 else ''}")
    if failed:
        print(f"⚠ {len(failed)} 個圖檔解不開：{list(failed.values())}")
    if unverified:
        print(f"⚠ {len(unverified)} 個圖檔沒通過「段長度總和」自我驗證，"
              f"圖可能不完整：{unverified[:10]}")

    # ★ 真正該看的數字：**道具**查不查得到圖（編號對得上但圖檔不在也算沒有）
    proto: set[int] = set()
    for path in sorted((setting / "base").glob("item*.xml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        proto.update(int(x) for x in re.findall(r'原型介面="(\d+)"', text))
    if proto:
        no_icon = sorted(p for p in proto if p not in usable)
        print(f"涵蓋率：item.xml 的 {len(proto)} 個原型介面，"
              f"有圖 {len(proto) - len(no_icon)}、沒圖 {len(no_icon)}"
              + (f"（{no_icon[:10]}{' …' if len(no_icon) > 10 else ''}）"
                 if no_icon else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
