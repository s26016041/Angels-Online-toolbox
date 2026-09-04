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
    # ★ 0 ＝「無異議對話」那一頁按確定（使用者 2026-09-02：
    #   「無異議對話 → 選項1 → 無異議對話 → 結束」）。跟送第 N 項是**不同的
    #   封包**（messageclose 0x128 vs talkaction 0x0B），所以是合法的一格。
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2],
                                "menu": [0, 1, 0]})
    ck("★ 無異議→第1項→無異議 這種路徑記得起來", ok, why)
    ck("　清單上看得出哪一格是過場",
       "過場" in dungeon.describe({"do": "interact", "at": [1, 2],
                                   "menu": [0, 1, 0]}))
    ok, why = dungeon.validate({"do": "interact", "at": [1, 2], "menu": [-1]})
    ck("選項序號 -1 → 擋下來", not ok, why)
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

    # ── 傳點（使用者 2026-09-02：點位放在傳點上會不會永遠到不了）──
    print("\n傳點步驟")
    ok, why = dungeon.validate({"do": "portal", "to": [10, 20]})
    ck("傳點沒記目的地也是合法的（進去一次才知道）", ok, why)
    ok, why = dungeon.validate({"do": "portal", "to": [10, 20], "scene": 77})
    ck("記了目的地也合法", ok, why)
    ok, why = dungeon.validate({"do": "portal", "scene": 77})
    ck("★ 少了 to → 擋下", not ok, why)
    ok, why = dungeon.validate({"do": "portal", "to": [1, 2], "scene": "77"})
    ck("★ 目的地不是編號 → 擋下", not ok, why)
    ck("清單上看得出是傳點",
       "傳點" in dungeon.describe({"do": "portal", "to": [1, 2],
                                   "scene": 76}))
    ck("★ 還沒記目的地時清單上要講出來",
       "⚠" in dungeon.describe({"do": "portal", "to": [1, 2]}))

    s2 = dungeon.Script(scene=76, steps=[
        {"do": "walk", "to": [1, 1]},
        {"do": "portal", "to": [2, 2], "scene": 77},
        {"do": "walk", "to": [3, 3]},
        {"do": "portal", "to": [4, 4], "scene": 78},
    ])
    ck("★ 起點是腳本的章", dungeon.map_at(s2, 0) == 76)
    ck("★ 傳點之前還是舊圖", dungeon.map_at(s2, 1) == 76)
    ck("★ 傳點之後換新圖", dungeon.map_at(s2, 2) == 77)
    ck("★ 整份跑完在最後一張", dungeon.map_at(s2) == 78)
    s3 = dungeon.Script(scene=76, steps=[{"do": "portal", "to": [2, 2]}])
    ck("★ 傳點還沒記目的地 → 回「不知道」，不是硬猜一個",
       dungeon.map_at(s3) is None)

    # ── 腳本放在專案裡（使用者 2026-09-02 定案）────────────────
    print("\n腳本資料夾")
    root = Path(dungeon.__file__).resolve().parents[2]
    ck("★ 資料夾在專案裡（assets/副本），不是使用者端",
       dungeon.folder() == root / "assets" / dungeon.FOLDER_NAME,
       str(dungeon.folder()))
    ck("跑原始碼時存檔位置＝專案那份",
       dungeon.save_folder() == dungeon.folder(), str(dungeon.save_folder()))
    ck("舊的使用者資料夾還讀得到（不主動建）",
       dungeon.user_folder() != dungeon.folder())
    shipped = sorted(p.stem for p in dungeon.folder().glob("*.json"))
    listed = [p.stem for p in dungeon.list_scripts()]
    ck("★ 內建腳本列得出來", bool(shipped) and shipped[0] in listed,
       f"內建 {shipped}　列到 {listed}")
    ck("同名不會列兩次", len(listed) == len(set(listed)), str(listed))

    # 內建的每一份都要讀得進來、每一步都合格 —— 發出去的東西不能是壞的。
    for p in dungeon.folder().glob("*.json"):
        sc, why = dungeon.load(p)
        ck(f"★ 內建「{p.stem}」讀得進來", sc is not None, why)
        if sc is None:
            continue
        # ⚠ 還沒加步驟的腳本（只先記了入口）本來就還沒蓋章 —— 那是正常的
        #   半成品，不算壞掉（章是第一步存進來時才蓋的）。
        if sc.steps:
            ck(f"　「{p.stem}」有蓋地圖章",
               sc.scene is not None and bool(sc.map))
        else:
            ck(f"　「{p.stem}」還沒有步驟（只記了入口）→ 沒蓋章是正常的",
               sc.scene is None)
        bad = [dungeon.validate(st)[1] for st in sc.steps
               if not dungeon.validate(st)[0]]
        ck(f"　「{p.stem}」每一步都合格", not bad, "；".join(bad))
        fp = sc.map or {}
        out = [st for st in sc.steps
               for xy in [st.get("to") or st.get("at")] if xy
               and not (0 <= xy[0] < fp.get("w", 0)
                        and 0 <= xy[1] < fp.get("h", 0))]
        # ★ 這一項就是「明明是同一張圖卻說不是」的照妖鏡：座標掉在地圖外面
        #   ＝章跟步驟不是同一張圖（2026-09-02 真的發生過，章是門口那張）。
        ck(f"　★「{p.stem}」座標都在章裡那張圖的範圍內", not out, str(out))

    # -- 存檔位置／內建判定（2026-09-04：exe 裡「儲存」內建腳本會寫進
    #    PyInstaller 暫存目錄、關程式就消失還不報錯；「開啟資料夾」也開錯地方）--
    ck("跑原始碼：save_folder 就是專案 assets/副本",
       dungeon.save_folder() == dungeon.folder())
    ck("跑原始碼：專案裡的腳本算內建",
       dungeon.is_builtin(dungeon.folder() / "x.json"))
    ck("跑原始碼：使用者資料夾的不算內建",
       not dungeon.is_builtin(dungeon.user_folder() / "x.json"))
    ck("內建名單有「吞噬之間」", "吞噬之間" in dungeon.builtin_names())
    old_frozen, old_appdata = dungeon.frozen, os.environ.get("APPDATA")
    with tempfile.TemporaryDirectory() as td:
        os.environ["APPDATA"] = td
        dungeon.frozen = lambda: True
        try:
            sf = dungeon.save_folder()
            ck("★ exe：save_folder 改到使用者資料夾（不是解壓目錄）",
               sf == Path(td) / dungeon.APP_DIR_NAME / "副本" and sf.is_dir(),
               str(sf))
            ck("★ exe：save_folder ≠ folder", sf != dungeon.folder())
            ck("exe：內建腳本標「內建」",
               dungeon.source_label(dungeon.folder() / "x.json") == "內建")
            ck("exe：使用者資料夾的標「自己做的」",
               dungeon.source_label(sf / "x.json") == "自己做的")
            # 使用者自己做的要列得出來；跟內建同名的以內建為準（使用者 9/2 定案）
            dummy = json.dumps({"name": "t", "steps": []})
            (sf / "吞噬之間.json").write_text(dummy, encoding="utf-8")
            (sf / "我的測試副本.json").write_text(dummy, encoding="utf-8")
            listed = {p.stem: p for p in dungeon.list_scripts()}
            ck("★ exe：使用者自己做的腳本列得出來",
               listed.get("我的測試副本") == sf / "我的測試副本.json")
            ck("exe：同名時以內建為準（自製的被蓋住 → 製作頁存檔前要擋）",
               listed.get("吞噬之間") == dungeon.folder() / "吞噬之間.json")
        finally:
            dungeon.frozen = old_frozen
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata

    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
