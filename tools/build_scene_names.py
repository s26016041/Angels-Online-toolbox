"""把場景編號 → 中文地圖名那張表，從遊戲資源包重抽、寫回 `app/game/scene.py`。

    py tools\\build_scene_names.py [GAMEDATA資料夾]
    py tools\\build_scene_names.py --check          # 只比對、不寫檔（回傳碼 1 = 過期）

為什麼要有這支
--------------
`scene.SCENE_NAMES` 是**寫死的遊戲資料**（記憶體裡沒有地圖名字，見 scene.py 檔頭）。
官方改版新增地圖時它不會報錯，只會把新地圖顯示成「場景 441」—— 使用者看到的就是
巡邏點寫著一串數字。以前檔頭只寫「要更新就重跑上面兩個 XML」，但沒有工具，
等於每次都要手打；CLAUDE.md 的鐵則是「寫死表一律用 tools/build_*.py 自動抽」。

資料來源（兩個檔都在 `GAMEDATA/setting/`）
    base/stage.xml               <場景 編號="441" 地圖檔="map\\map441.mpc" …/>
    big5/string/str_stage.xml    <表格字串 編號="1290000441" 文字1="暴走穗海農場"/>
對照關係：**表格字串編號 = 1290000000 + 場景編號**（scene.py 檔頭已驗證）。
只收 stage.xml 真的有的場景編號 —— str_stage 裡還有一堆沒在用的舊字串。

⚠ 純文字處理：只讀 GAMEDATA、只改 scene.py 裡那個 dict 的內容，不碰遊戲。
⚠ 改完請跑 `py tools\\selfcheck.py`；本表登記在 memory `items-table-maintenance`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app" / "game" / "scene.py"
STR_BASE = 1290000000                      # 表格字串編號 = 這個 + 場景編號
HEAD = "SCENE_NAMES: dict[int, str] = {"


def read_table(gamedata: Path) -> dict[int, str]:
    setting = gamedata / "setting"
    stage = (setting / "base" / "stage.xml").read_text(encoding="utf-8")
    names = (setting / "big5" / "string"
             / "str_stage.xml").read_text(encoding="utf-8")
    ids = {int(m) for m in re.findall(r'<場景 編號="(\d+)"', stage)}
    text = {int(s) - STR_BASE: n
            for s, n in re.findall(r'編號="(\d+)" 文字1="([^"]*)"', names)}
    return {i: text[i] for i in sorted(ids) if i in text}


def current_table() -> dict[int, str]:
    src = TARGET.read_text(encoding="utf-8")
    body = src.split(HEAD, 1)[1].split("\n}\n", 1)[0]
    return {int(a): b for a, b in re.findall(r'(\d+): "([^"]*)"', body)}


def render(table: dict[int, str]) -> str:
    lines = [f'    {i}: "{table[i]}",' for i in sorted(table)]
    return HEAD + "\n" + "\n".join(lines) + "\n}\n"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv[1:]
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    if not (gamedata / "setting" / "base" / "stage.xml").is_file():
        print(f"⛔ 找不到 {gamedata}/setting/base/stage.xml")
        return 2

    want, have = read_table(gamedata), current_table()
    add = {i: want[i] for i in want if i not in have}
    gone = {i: have[i] for i in have if i not in want}
    diff = {i: (have[i], want[i]) for i in want
            if i in have and have[i] != want[i]}
    print(f"資源包 {len(want)} 筆／scene.py {len(have)} 筆")
    for i, n in add.items():
        print(f"  ＋新增 {i}: {n}")
    for i, n in gone.items():
        print(f"  －資源包已無 {i}: {n}（會刪掉）")
    for i, (a, b) in diff.items():
        print(f"  ≠改名 {i}: {a} → {b}")
    if not (add or gone or diff):
        print("✔ 完全一致，不必動")
        return 0
    if check:
        print("⚠ 過期（--check 不寫檔）")
        return 1

    src = TARGET.read_text(encoding="utf-8")
    head, rest = src.split(HEAD, 1)
    _old, tail = rest.split("\n}\n", 1)
    TARGET.write_text(head + render(want) + tail, encoding="utf-8")
    print(f"→ 已寫回 {TARGET}（{len(want)} 筆）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
