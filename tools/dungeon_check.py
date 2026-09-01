"""副本腳本的回歸測試（不必開遊戲）。

    py tools\\dungeon_check.py

驗的是「格式錯了會不會被擋下來」——腳本是使用者手打的，壞掉的腳本一旦被
半套載入，執行端就會走到一半沒人接。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import dungeon                              # noqa: E402

PASS = FAIL = 0


def ck(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}　{note}")


class FakeGrid:
    """最小的假地形圖：open[y][x] 1 = 可走。"""

    def __init__(self, rows):
        self.h = len(rows)
        self.w = len(rows[0])
        self.open = [bytearray(r) for r in rows]

    def walkable(self, x, y):
        return 0 <= x < self.w and 0 <= y < self.h and bool(self.open[y][x])

    def reachable(self, tx, ty):
        if not self.walkable(tx, ty):
            return None
        seen = {(tx, ty)}
        stack = [(tx, ty)]
        while stack:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (x + dx, y + dy)
                if n not in seen and self.walkable(*n):
                    seen.add(n)
                    stack.append(n)
        return seen


def main() -> int:
    print("步驟格式檢查")
    ok, _ = dungeon.validate({"do": "walk", "to": [10, 20]})
    ck("walk 正常", ok)
    ok, why = dungeon.validate({"do": "walk"})
    ck("walk 少了 to → 擋下來", not ok, why)
    ok, _ = dungeon.validate({"do": "interact", "at": [1, 2], "menu": [1, 3]})
    ck("interact 正常", ok)
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2], "menu": [0]})
    ck("選項序號 0 → 擋下來（1 起算）", not ok, why)
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2], "menu": [11]})
    ck(f"選項序號 > {dungeon.MENU_MAX} → 擋下來", not ok, why)
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2],
                                "select_id": 0x13920001})
    ck("★ 存了 select_id → 擋下來（世代碼會變）", not ok, why)
    ok, _ = dungeon.validate({"do": "interact", "at": [1, 2], "menu": [1],
                              "gap": 1.5})
    ck("interact 帶選項間隔 正常", ok)
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2], "gap": 0})
    ck("選項間隔 0 → 擋下來", not ok, why)
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2], "gap": 999})
    ck("選項間隔 999 秒 → 擋下來", not ok, why)
    ok, _ = dungeon.validate({"do": "clear"})
    ck("clear 正常", ok)
    ok, why = dungeon.validate({"do": "wait", "secs": 0})
    ck("wait 0 秒 → 擋下來", not ok, why)
    ok, why = dungeon.validate({"do": "fly", "to": [1, 2]})
    ck("不認得的動作 → 擋下來", not ok, why)

    print("\n讀寫")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.json"
        s = dungeon.Script(name="吞噬之間1", scene=76,
                           map={"w": 420, "h": 230, "walkable": 22675})
        s.add({"do": "clear"})
        s.add({"do": "walk", "to": [306, 160]})
        s.add({"do": "interact", "at": [307.1, 161.3], "model": 60307,
               "menu": [1]})
        s.save(p)
        back, why = dungeon.load(p)
        ck("存了讀得回來", back is not None, why)
        ck("步驟數一樣", back and len(back.steps) == 3)
        ck("中文名字沒壞（UTF-8）", back and back.name == "吞噬之間1")
        ck("有蓋時間", bool(back and back.saved_at))

        bad = json.loads(p.read_text(encoding="utf-8"))
        bad["steps"][1] = {"do": "walk"}          # 弄壞第 2 步
        p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        back, why = dungeon.load(p)
        ck("★ 有一步壞掉 → 整份拒收（不半套載入）", back is None, why)
        ck("錯誤訊息指得出是第幾步", "第 2 步" in why, why)

        p.write_text("{ 不是 json", encoding="utf-8")
        back, why = dungeon.load(p)
        ck("不是 JSON → 拒收", back is None, why)

    print("\n步驟搬移")
    s = dungeon.Script()
    for i in range(3):
        s.add({"do": "wait", "secs": i + 1})
    j = s.move(0, 1)
    ck("往下搬回傳新位置", j == 1 and s.steps[1]["secs"] == 1)
    ck("往上搬過頭不動", s.move(0, -1) == 0)
    ck("往下搬過頭不動", s.move(2, 1) == 2)
    s.remove(1)
    ck("刪得掉", len(s.steps) == 2)

    print("\n連通區（副本房間）")
    #  兩塊互不相通的區域，中間隔一整排牆
    rows = [[1, 1, 0, 1, 1],
            [1, 1, 0, 1, 1],
            [1, 1, 0, 1, 1]]
    g = FakeGrid(rows)
    of, sizes = dungeon.rooms(g, min_cells=2)
    ck("切出兩間", len(sizes) == 2, str(sizes))
    ck("同一邊同一間", of.get((0, 0)) == of.get((1, 2)))
    ck("兩邊不同間", of.get((0, 0)) != of.get((3, 0)))
    ck("牆不在任何一間", (2, 0) not in of)
    of2, sizes2 = dungeon.rooms(g, min_cells=99)
    ck("碎片門檻擋得掉", not sizes2 and not of2)

    print("\n地圖指紋比對")
    s = dungeon.Script(scene=76, map={"w": 420, "h": 230, "walkable": 22675})

    class G:
        w, h = 420, 230
        open = [bytearray([1] * 22675 + [0] * (420 * 230 - 22675))]

    # 手工湊一張「可走格數剛好 22675」的假圖
    class G2:
        w, h = 420, 230

        def __init__(self, n):
            self.open = []
            left = n
            for _ in range(230):
                take = min(420, left)
                self.open.append(bytearray([1] * take + [0] * (420 - take)))
                left -= take

    ok, why = dungeon.check_map(s, G2(22675), 196684,
                                lambda v: None if v is None else (v & 0xFFFF))
    ck("同一張圖 → 過", ok, why)
    ok, why = dungeon.check_map(s, G2(22675), 196685,
                                lambda v: None if v is None else (v & 0xFFFF))
    ck("★ 換了場景 → 大聲擋下", not ok, why)
    ok, why = dungeon.check_map(s, G2(22659), 196684,
                                lambda v: None if v is None else (v & 0xFFFF))
    ck("★ 差 16 格（副本的門開關）→ 照樣過，不誤報", ok, why)
    ok, why = dungeon.check_map(s, G2(20000), 196684,
                                lambda v: None if v is None else (v & 0xFFFF))
    ck("★ 差一成（官方真的改了地圖）→ 大聲擋下", not ok, why)
    ok, why = dungeon.check_map(s, None, None, None)
    ck("不比場景也不給圖 → 過（呼叫端自己決定）", ok, why)

    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
