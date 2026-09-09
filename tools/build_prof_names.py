"""把「熟練技能編號 → 名字」那張表從遊戲資源包重抽、寫回 `app/game/gear.py`。

    py tools\\build_prof_names.py [GAMEDATA資料夾]
    py tools\\build_prof_names.py --check      # 只比對、不寫檔（回傳碼 1 = 過期）

為什麼要有這支
--------------
裝備提示框最後一行「需要法袍技能等級20」的那個名字，就是 item.xml 的
`技能限制1`（範本 +0x38）指到的**熟練技能**編號。記憶體裡查得到名字，但那是
遊戲自己的字串表；照 CLAUDE.md 第 0 條「有表對表」，靜態定義一律從資源包抽表。

資料來源（兩個檔都在 `GAMEDATA/setting/`）
    base/skill.xml               <技能 編號="34" …/>          ← 有哪些編號
    big5/string/str_skill.xml    <表格字串 編號="1280000034" 文字1="法袍"/>
對照關係：**表格字串編號 = 1280000000 + 技能編號**（跟法術名的 118xxxxxxx、
場景名的 129xxxxxxx 同一套規則）。只收 skill.xml 真的有的編號。

⚠ 純文字處理：只讀 GAMEDATA、只改 gear.py 裡那個 dict 的內容，不碰遊戲。
⚠ 改完請跑 `py tools\\selfcheck.py`；本表登記在 memory `items-table-maintenance`
  與 `.claude/commands/_patchCheck.md` 第 7 步。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app" / "game" / "gear.py"
STR_BASE = 1280000000                      # 表格字串編號 = 這個 + 技能編號
HEAD = "PROF_NAMES: dict[int, str] = {"
PER_LINE = 4                               # 一行擺幾筆


def read_table(gamedata: Path) -> dict[int, str]:
    setting = gamedata / "setting"
    skills = (setting / "base" / "skill.xml").read_text(encoding="utf-8")
    names = (setting / "big5" / "string"
             / "str_skill.xml").read_text(encoding="utf-8")
    ids = {int(m) for m in re.findall(r'<技能 編號="(\d+)"', skills)}
    text = {int(s) - STR_BASE: n
            for s, n in re.findall(r'編號="(\d+)" 文字1="([^"]*)"', names)}
    return {i: text[i] for i in sorted(ids) if i in text and text[i]}


def current_table() -> dict[int, str]:
    src = TARGET.read_text(encoding="utf-8")
    body = src.split(HEAD, 1)[1].split("\n}\n", 1)[0]
    return {int(a): b for a, b in re.findall(r'(\d+): "([^"]*)"', body)}


def render(table: dict[int, str]) -> str:
    items = [f'{i}: "{table[i]}",' for i in sorted(table)]
    lines = ["    " + " ".join(items[i:i + PER_LINE])
             for i in range(0, len(items), PER_LINE)]
    return HEAD + "\n" + "\n".join(lines) + "\n}\n"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv[1:]
    gamedata = Path(args[0]) if args else ROOT / "GAMEDATA"
    if not (gamedata / "setting" / "base" / "skill.xml").is_file():
        print(f"⛔ 找不到 {gamedata}/setting/base/skill.xml")
        return 2

    want, have = read_table(gamedata), current_table()
    add = {i: want[i] for i in want if i not in have}
    gone = {i: have[i] for i in have if i not in want}
    diff = {i: (have[i], want[i]) for i in want
            if i in have and have[i] != want[i]}
    print(f"資源包 {len(want)} 筆／gear.py {len(have)} 筆")
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
