"""把遊戲資源包裡的道具圖示打包成 `assets/item_icons.zip`（原始 .SHP ＋ 換色索引）。

    py tools\\build_item_icons.py [GAMEDATA/setting 資料夾]

## 為什麼要自己解圖檔
遊戲的圖示是自家格式 `GAMEDATA\\shape\\item\\i####.SHP`（4575 個檔、8.9MB），
Qt 讀不了。執行時 `app/game/itemicon.py` 自己解（numpy），這支只負責**驗過再打包**。
★ **圖示編號（原型介面）本身是從記憶體讀的**（範本 `+0x00`，遊戲自己的
  `geticon` 綁定就是讀這裡），所以改版換了道具的圖，編號會自動跟上；
  這包只提供「編號 → 圖」那一段。
⚠ **官方改版新增道具要重跑這支**，不然新道具沒有圖（會退化成不顯示圖示，
  不會做錯事）。流程見 `.claude/commands/_patchCheck.md` 第 7 步。

## ★★ 2026-09-06 改版：存原始 .SHP、索引帶換色參數（不再存 PNG）
`itemicon*.xml` 一筆長這樣：
    <icon id="5450" dir="\\item\\" normal="i0254" … basebias="0x50a08" basecolor="0x4aed" baserange="132">
**33316 個編號有 21564 個帶 basebias**＝同一張 .SHP 換個顏色（3347 張 .SHP 被不同顏色
共用）。舊圖包只看 `normal`，所有顏色版本都畫成原色 —— 使用者 2026-09-06：「同等寶石
你好像都用同一個圖片」。換色算法反組譯自 angel.dat（`app/game/iconbias.py`），執行時
照遊戲換調色盤；這裡只把三個參數抄進索引。

## 包裡有什麼
    index.tsv     編號 <TAB> 圖檔名 <TAB> basebias <TAB> basecolor <TAB> baserange（十進位）
    i####.shp     原始 TLHS 圖檔（deflate 壓過）
⚠ 同一個編號在不同 itemicon 檔裡說法矛盾（圖檔名或換色參數不同）→ **整個丟掉不收**，
  寧可沒圖也不要顯示錯的圖（照 CLAUDE.md 的安全退化原則）。
✅ 每個 .SHP 打包前都用執行時那支解碼器解一次＋「段長度總和 == 像素數」自我驗證。
"""
from __future__ import annotations

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

from app.game import itemicon                              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "item_icons.zip"
ROW_END = itemicon.ROW_END

ICON_ROW = re.compile(r'<icon id="(\d+)" dir="(.*?)" normal="(.*?)"([^>]*)>')
ATTR = re.compile(r'(\w+)="([^"]*)"')


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


def _int(s: str | None, default: int = 0) -> int:
    if not s:
        return default
    try:
        return int(s, 0)
    except ValueError:
        return default


def read_index(setting: Path) -> tuple[dict[int, tuple[str, int, int, int]], list[int]]:
    """itemicon*.xml → {編號: (圖檔名, bias, color, range)}，以及「說法互相矛盾」的編號清單。"""
    merged: dict[int, tuple[str, int, int, int]] = {}
    bad: set[int] = set()
    files = sorted(setting.glob("itemicon*.xml"))
    if not files:
        raise SystemExit(f"找不到 {setting}\\itemicon*.xml —— 資源包路徑對嗎？")
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        n = 0
        for m in ICON_ROW.finditer(text):
            iid, folder, name, rest = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            if folder.strip("\\/").lower() != "item":
                continue           # 目前資源包裡全是 \item\，別的先不收
            a = dict(ATTR.findall(rest))
            entry = (name.lower(), _int(a.get("basebias")) & 0xFFFFFFFF,
                     _int(a.get("basecolor")) & 0xFFFF, _int(a.get("baserange")) & 0xFF)
            n += 1
            if iid in merged and merged[iid] != entry:
                bad.add(iid)
            merged.setdefault(iid, entry)
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
    biased = sum(1 for e in index.values() if e[1])
    print(f"編號 {len(index)} 個（{biased} 個帶換色參數）"
          + (f"（丟掉 {len(conflict)} 個說法矛盾的：{conflict[:10]}）" if conflict else ""))

    on_disk = {p.name.lower(): p for p in shape.glob("*.SHP")}
    print(f"圖檔 {len(on_disk)} 個 @ {shape}")

    done: dict[str, bytes] = {}
    missing: list[int] = []
    failed: dict[str, str] = {}
    unverified: list[str] = []
    for iid, (name, _b, _c, _r) in sorted(index.items()):
        key = f"{name}.shp"
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
            shp = itemicon.decode_shp(raw)
            itemicon.compose(shp)             # 真的解得出圖才收
        except Exception as exc:              # noqa: BLE001
            failed[key] = f"{path.name}（{exc}）"
            continue
        done[key] = raw

    usable = {iid: e for iid, e in index.items() if f"{e[0]}.shp" in done}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("index.tsv",
                   "".join(f"{i}\t{e[0]}\t{e[1]}\t{e[2]}\t{e[3]}\n"
                           for i, e in sorted(usable.items())))
        for key, raw in sorted(done.items()):
            z.writestr(key, raw)

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
