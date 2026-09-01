"""把場景物件的「外觀編號 → 中文名字」抽成一張表 → `assets/mapobj_names.tsv.gz`。

    py tools\\build_mapobj.py [GAMEDATA/setting 資料夾]
    py tools\\build_mapobj.py --check      # 只比對，過期回傳碼 1

## 這張表是幹嘛的

副本腳本製作那頁列出「附近可互動的物件」時，本來只印得出**外觀編號**
（`外觀 60307`），使用者根本看不出那是什麼東西。有了這張表就變成

    副本2小藍火（上升）（60307）
    惡魔系雕像01（60049）
    門開關火炬（60299）

—— 2026-09-01 吞噬之間 1 那趟「解謎」是繞 8 座雕像（人型／動物／能量／惡魔
各 01、02），沒有名字完全看不出來。

## 資料在哪（2026-09-02 找到）

`GAMEDATA/setting/*.obd` 是**文字**的物件資料庫（BIG5、CRLF），一筆長這樣：

    [OBJECT]
    Name = 墓碑
    Sequence = 60097
    Flags = SP_ATTRIB_HITTEST, SP_ATTRIB_SHADOW, ...
    Process = SERV_CLASS_STATIC
    Directory = \\stage\\24\\
    Sprite = Wait, 24-012-5U0, SCROVER

`Sequence` 就是實體 `+0xB4` 讀到的那個外觀編號（實測 60097＝墓碑、
60307＝副本2小藍火，跟現場位置對得上）。各檔的編號範圍不重疊：

    server.obd 60001~112755   mapobj.obd 5001~20000   house.obd 10001~10116
    common.obd 1~134144       npc.obd 40001~111668

⚠ 編號**跨檔重複**時以先讀到的為準（依 FILES 的順序），並印出衝突數量。
⚠ 官方改版新增物件要重跑這支；查不到就退化成只顯示編號（不會顯示錯的名字）。
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "mapobj_names.tsv.gz"

# 順序 = 優先序（前面的先贏）。場景物件以 server / mapobj 為主。
FILES = ("server.obd", "mapobj.obd", "house.obd", "common.obd")

_NAME = re.compile(r"^Name\s*=\s*(.+?)\s*$")
_SEQ = re.compile(r"^Sequence\s*=\s*(\d+)\s*$")
_FLAGS = re.compile(r"^Flags\s*=\s*(.+?)\s*$")
_DIR = re.compile(r"^Directory\s*=\s*(.+?)\s*$")
_SPRITE = re.compile(r"^Sprite\s*=\s*(.+?)\s*$")


def parse(path: Path) -> list[dict]:
    """把一個 .obd 裡的 [OBJECT] 記錄抽出來。"""
    # ⚠ BIG5：用系統預設編碼讀，中文名字會整片變成問號（而且不會報錯）。
    text = path.read_bytes().decode("big5", errors="replace")
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            if cur and cur.get("seq") and cur.get("name"):
                out.append(cur)
            cur = {} if s == "[OBJECT]" else None
            continue
        if cur is None:
            continue
        m = _NAME.match(s)
        if m:
            cur["name"] = m.group(1)
            continue
        m = _SEQ.match(s)
        if m:
            cur["seq"] = int(m.group(1))
            continue
        m = _FLAGS.match(s)
        if m:
            cur["flags"] = m.group(1)
            continue
        m = _DIR.match(s)
        if m:
            cur["dir"] = m.group(1)
            continue
        m = _SPRITE.match(s)
        if m:
            cur["sprite"] = m.group(1)
    if cur and cur.get("seq") and cur.get("name"):
        out.append(cur)
    return out


def sprite_file(rec: dict) -> str:
    """這筆物件的第一張圖（`\\stage\\24\\24-012-5U0`）；抽不出來回空字串。

    版面是 `Sprite = <動作名>, [#延遲,] <檔名>, <檔名>…, [旗標]`：

        Sprite = Wait, 24-012-5U0, SCROVER
        Sprite = Wait, #3, 27-053-1U1, #8, 27-053-1U2, …      ← 動畫，取第一張
        Sprite = Wait, ptag, SCROVER                          ← 圖在 shape 根目錄

    ⚠⚠ **第一個欄位一定要跳掉** —— 它是動作名，而動作名不只叫 `Wait`，還有
      `Wait2` / `Wait6` / `Dead`。第一版只擋 `wait` 就把 `Wait6` 當成檔名，
      2068 個物件的圖全都找不到（而且不會報錯，只是安靜地沒圖）。
    """
    raw = rec.get("sprite") or ""
    folder = (rec.get("dir") or "").strip().strip("\\").replace("\\", "/")
    parts = [p.strip() for p in raw.split(",")]
    for part in parts[1:]:               # ← [0] 是動作名，一律跳掉
        if not part or part.startswith("#"):
            continue                     # #3 是延遲張數
        if part.isupper() and "-" not in part:
            continue                     # 純大寫的旗標（SCROVER…）
        return f"{folder}/{part}" if folder else part
    return ""


def build(setting: Path) -> tuple[dict[int, tuple[str, str, str]], int]:
    table: dict[int, tuple[str, str, str]] = {}
    clash = 0
    for name in FILES:
        p = setting / name
        if not p.is_file():
            print(f"　（沒有 {name}，跳過）")
            continue
        recs = parse(p)
        added = 0
        for r in recs:
            seq = r["seq"]
            if seq in table:
                clash += 1
                continue                 # 先讀到的贏（見檔頭）
            flags = r.get("flags") or ""
            # 只留我們用得到的兩個旗標：
            #   HITTEST 點得到　HIDE 看不見（TAG01 那種場景標記點）
            keep = [k for k in ("HITTEST", "HIDE")
                    if f"SP_ATTRIB_{k}" in flags]
            table[seq] = (r["name"], ",".join(keep), sprite_file(r))
            added += 1
        print(f"　{name}：{len(recs)} 筆記錄，收 {added} 筆")
    return table, clash


def write(table: dict[int, tuple[str, str, str]]) -> bytes:
    lines = ["編號\t名稱\t旗標\t圖檔"]
    for seq in sorted(table):
        nm, hit, spr = table[seq]
        lines.append(f"{seq}\t{nm}\t{hit}\t{spr}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    check = "--check" in sys.argv
    setting = Path(args[0]) if args else ROOT / "GAMEDATA" / "setting"
    if not setting.is_dir():
        print(f"⛔ 找不到 {setting}")
        return 2
    table, clash = build(setting)
    if not table:
        print("⛔ 一筆都沒抽到 —— 格式變了？")
        return 2
    hit = sum(1 for v in table.values() if "HITTEST" in v[1])
    withimg = sum(1 for v in table.values() if v[2])
    print(f"共 {len(table)} 筆（點得到的 {hit}、有圖檔名的 {withimg}）"
          f"，跨檔重複 {clash} 筆已略過")
    data = write(table)
    if check:
        try:
            old = gzip.decompress(OUT.read_bytes())
        except Exception:                                # noqa: BLE001
            print("⛔ 現有的表讀不到 → 需要重建")
            return 1
        if old != data:
            print("⛔ 資源包裡的物件表跟 assets 那份不一樣 → 需要重跑")
            return 1
        print("✔ 跟 assets 那份一致")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(gzip.compress(data, 9))
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
