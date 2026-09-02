r"""副本腳本：一趟副本要照順序做哪些事，存成 JSON。

    dungeon.folder()            # 腳本資料夾（**專案裡的** assets/副本）
    dungeon.list_scripts()      # 資料夾裡所有 .json
    dungeon.load(path)          # → Script
    script.save(path)
    dungeon.fingerprint(grid)   # 目前這張地圖的指紋
    dungeon.check_map(script, grid)   # 腳本跟現在這張圖對不對得上

## 為什麼是「步驟」不是「點位清單」

使用者原本的想法是「一堆要照順序去的點位」。實作時改成**步驟**，因為單純照
順序走是盲走 —— 使用者自己指出的病灶就是最好的例子：

> 「NPC 對話有延遲，比如要把怪物殺光他不是殺光馬上就能點擊，
>   如果太早點擊他會出現無異議對話」

所以每一步除了「做什麼」，還要有「什麼時候可以做」與「怎樣算做完」。

⚠⚠ **`clear`（清光周圍的怪）已經不再是要自己加的步驟**（使用者 2026-09-02：
  「為何會有清光周圍怪物按鈕？這事本來在執行跑點位或對話之前就該這樣」）——
  執行端的規矩已經是「走得到的怪還有一隻就先打，一隻都不剩才做下一步」，
  所以清怪是**每一步的前提**，不是一個要記得加的動作。製作頁的按鈕拿掉了；
  舊腳本裡有 `clear` 照樣跑得動（多一道 3 秒確認），格式也還收。

## 步驟種類

    {"do": "walk",     "to": [x, y]}                 走到這一格
    {"do": "interact", "at": [x, y], "model": 60307,  點那個物件，然後照順序
                       "menu": [1, 2]}                送對話選項（1 起算）
    {"do": "clear"}                                  ⚠ 舊步驟，介面已不再產生
                                                     （清怪本來就是每一步的前提）
    {"do": "wait",     "secs": 3}                    單純等幾秒
    {"do": "portal",   "to": [x, y], "model": 60xxx, 走進傳點（人被移走才算完成）
                       "land": [x, y], "scene": 76}

## 傳點為什麼要獨立一種步驟（使用者 2026-09-02 問對了）

> 「如果我把點位放在傳點上，因為我要進入傳點，那會卡住嗎永遠到不了那個點？」

會卡。`walk` 的完成條件是「站到那一格附近」，但踩上傳點的**下一瞬間人就被
移走了** —— 那一格永遠不會「到達」，只會耗到 `STEP_TIMEOUT` 才停。

⚠⚠ **完成條件是「順移」不是「換地圖」**（使用者 2026-09-02 當場更正：

> 「但是人被傳走不會換地圖，有順移就算吧，有時候傳點之間也很短」

吞噬之間那 6 間互不相通的房間**都在同一個場景編號裡**，傳點是把人搬到同一
張圖的另一個地方，場景編號完全不變。所以：

    完成 ＝ 一拍之間位置跳了一大段（順移）　或　場景真的變了

⛔ 不可以用「離傳點多遠」當訊號 —— 使用者說「有時候傳點之間也很短」，
  出口可能就在幾格外，用距離門檻會漏判。跳一拍的**位移速度**才分得出來
  （跑步一拍 0.1 秒最多動 0.6 格，順移一定遠大於這個）。

`land` ＝ 腳本製作時**實際看到**的出口位置（不是算出來的）；跑的時候拿來
確認「傳到的地方跟當初一樣」，差太遠就大聲停下。

⚠⚠ **`interact` 只存位置與外觀編號，絕對不存「選定 id」。**
  出處 `scenery.py` 檔頭（2026-08-12 實機攔包）：選定 id 的高 16 位是
  伺服器**每次載入地圖重配的世代碼**，跨一次進場就作廢。所以到現場要
  用位置找最近、`model` 對得上的那個物件，當場重讀它的 `+0x1D0`。
  存 id ＝ 每次都點到不存在的東西，而且不會有任何錯誤訊息。

## 地圖指紋

腳本記下錄製當下地圖的寬、高、可走格數。開跑前比對一次：官方改版把地圖
改了就**大聲停用**，不會拿舊腳本盲走（CLAUDE.md 只允許大聲停用或安全退化）。

⚠ 場景編號存的是**剝掉分流序號的 base id**（`scene.map_key`）：同一個副本
  分流 1~5 是不同的 raw 編號、同一張地圖（見 `scene.split`）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import APP_DIR_NAME
from app.game import mapobj
from app.paths import resource

FOLDER_NAME = "副本"

# 步驟種類
WALK, INTERACT, CLEAR, WAIT = "walk", "interact", "clear", "wait"
PORTAL = "portal"
KINDS = (WALK, INTERACT, CLEAR, WAIT, PORTAL)

# 對話選單最多幾項（talkaction 碼只到第 10 項，見 supply.talk_option）
MENU_MAX = 10
# 可走格數容許差幾成（見 check_map）。副本的門會開關，格數本來就會變。
WALKABLE_TOLERANCE = 0.02


def folder() -> Path:
    """腳本資料夾＝**專案裡的** `assets/副本`，隨工具箱一起發出去。

    ★ 使用者 2026-09-02 定案：「副本 json 是存在我們專案不是使用者端，
      這功能是我們自己寫路徑，使用者只負責使用。」
      —— 副本怎麼跑是我們研究出來的東西（哪個雕像先繞、對話選第幾項），
      應該像 `supply_shop.json`、`mapobj_names.tsv.gz` 那樣當成**資源**發，
      不是叫每個使用者自己在自己電腦上重做一份。
    """
    p = resource(f"assets/{FOLDER_NAME}")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass                       # 打包後解壓目錄建不了也沒關係，只是列不到
    return p


def user_folder() -> Path:
    r"""舊版存過腳本的地方（`%APPDATA%\AngelsOnlineToolbox\副本`）。

    ⚠ 只為了**還讀得到**舊檔而留（不主動建）：2026-09-02 之前存的腳本都在
      這裡，直接改路徑會讓使用者的腳本憑空消失。
    """
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP_DIR_NAME / FOLDER_NAME


def save_folder() -> Path:
    """製作分頁存檔的位置。

    跑原始碼（我們自己做腳本）＝專案那份，存完 commit 就發給所有人。
    打包成 exe 之後專案目錄在 PyInstaller 的暫存區（關掉程式就沒了），
    退回使用者資料夾 —— 安全退化，不要安靜地存進一個等下會被刪掉的地方。
    """
    if hasattr(sys, "_MEIPASS") or getattr(sys, "frozen", False):
        p = user_folder()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return folder()


def list_scripts() -> list[Path]:
    """所有看得到的腳本：專案內建的優先，再補上使用者資料夾裡的舊檔。

    同名（檔名一樣）時以專案那份為準 —— 我們發出去的才是維護中的版本。
    """
    out: list[Path] = []
    seen: set[str] = set()
    for d in (folder(), user_folder()):
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for p in files:
            if p.stem in seen:
                continue
            seen.add(p.stem)
            out.append(p)
    return out


def fingerprint(grid) -> dict:
    """目前這張地圖的指紋（寬、高、可走格數）。grid 是 terrain.Grid。"""
    return {"w": grid.w, "h": grid.h,
            "walkable": sum(sum(r) for r in grid.open)}


def rooms(grid, min_cells: int = 20) -> tuple[dict, list[int]]:
    """把地圖切成互不相通的區塊 → ({(x, y): 房號}, [每間的格數])。

    ★ 為什麼副本一定要看這個：2026-09-01 實測吞噬之間 1（420x230、可走 22675）
      切出來是 **6 間互不相通的房間**（最厚的牆超過 5 格，沒有任何門）——
      房間之間只能靠傳送點。所以「走到某一格」之前得先知道那一格跟我在不在
      同一間，不然會算出一條根本走不到的路。

    `min_cells` 以下的碎片不編號（實測有 9/4/2 格的零星角落，沒有意義）。
    """
    if grid is None:
        return {}, []
    seen: set = set()
    of: dict = {}
    sizes: list[int] = []
    for y in range(grid.h):
        row = grid.open[y]
        for x in range(grid.w):
            if not row[x] or (x, y) in seen:
                continue
            comp = grid.reachable(x, y) or set()
            seen |= comp
            if len(comp) < min_cells:
                continue
            n = len(sizes)
            for c in comp:
                of[c] = n
            sizes.append(len(comp))
    return of, sizes


@dataclass
class Script:
    """一份副本腳本。"""

    name: str = ""
    scene: int | None = None          # base 場景編號（已剝掉分流序號）
    map: dict = field(default_factory=dict)     # 地圖指紋
    steps: list[dict] = field(default_factory=list)
    saved_at: str = ""
    # ★ 進副本的入口傳送點（使用者 2026-09-02）。整份腳本共用一個，不是步驟：
    #   {"scene": 71, "to": [x, y], "model": 60xxx, "land": [x, y]}
    #   scene ＝ **外面那張圖**的 map_key（站在那裡才撞得到入口）。
    entrance: dict = field(default_factory=dict)

    # -- 讀寫 ---------------------------------------------------------
    def to_json(self) -> dict:
        return {"name": self.name, "scene": self.scene, "map": self.map,
                "entrance": self.entrance,
                "saved_at": self.saved_at, "steps": self.steps}

    def save(self, path: Path) -> None:
        self.saved_at = time.strftime("%Y-%m-%d %H:%M:%S")
        path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠ 一律 UTF-8：步驟裡有中文備註，用系統預設編碼寫出去，
        #   換一台機器（cp950）就讀不回來。
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    # -- 步驟操作（給介面用）------------------------------------------
    def add(self, step: dict, at: int | None = None) -> None:
        if at is None or not 0 <= at <= len(self.steps):
            self.steps.append(step)
        else:
            self.steps.insert(at, step)

    def move(self, i: int, delta: int) -> int:
        """把第 i 步往上／往下搬，回傳搬完之後的位置。"""
        j = i + delta
        if not (0 <= i < len(self.steps) and 0 <= j < len(self.steps)):
            return i
        self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        return j

    def remove(self, i: int) -> None:
        if 0 <= i < len(self.steps):
            del self.steps[i]

    def points(self) -> list[tuple[int, tuple[float, float], str]]:
        """所有帶座標的步驟 → (第幾步, (x, y), 種類)。畫地圖用。"""
        out = []
        for i, s in enumerate(self.steps):
            xy = s.get("to") or s.get("at")
            if xy and len(xy) == 2:
                out.append((i, (float(xy[0]), float(xy[1])), s.get("do", "")))
        return out


def load(path: Path) -> tuple[Script | None, str]:
    """讀一份腳本；讀不到／格式不對回 (None, 原因)。

    ⚠ 壞掉的腳本一律**不要半套載入** —— 少一步就是走到一半沒人接。
    """
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        return None, f"讀不到或不是合法 JSON：{e}"
    if not isinstance(d, dict):
        return None, "格式不對（最外層不是物件）"
    steps = d.get("steps")
    if not isinstance(steps, list):
        return None, "沒有 steps 陣列"
    for i, s in enumerate(steps):
        ok, why = validate(s)
        if not ok:
            return None, f"第 {i + 1} 步有問題：{why}"
    ent = d.get("entrance") or {}
    ok, why = validate_entrance(ent)
    if not ok:
        return None, f"入口傳送點有問題：{why}"
    sc = Script(name=str(d.get("name") or Path(path).stem),
                scene=d.get("scene"),
                map=d.get("map") or {},
                entrance=ent,
                steps=steps,
                saved_at=str(d.get("saved_at") or ""))
    return sc, ""


def validate(step: dict) -> tuple[bool, str]:
    """單一步驟的格式檢查。回 (可不可以用, 原因)。"""
    if not isinstance(step, dict):
        return False, "不是物件"
    kind = step.get("do")
    if kind not in KINDS:
        return False, f"不認得的動作「{kind}」"
    if kind == WALK:
        xy = step.get("to")
        if not (isinstance(xy, list) and len(xy) == 2):
            return False, "walk 少了 to:[x,y]"
    elif kind == INTERACT:
        xy = step.get("at")
        if not (isinstance(xy, list) and len(xy) == 2):
            return False, "interact 少了 at:[x,y]"
        if "select_id" in step:
            # 存過 id 的舊檔一律拒收：那個值跨一次進場就作廢（見檔頭）。
            return False, "interact 不可以存 select_id（世代碼會變）"
        menu = step.get("menu", [])
        if not isinstance(menu, list):
            return False, "menu 要是陣列"
        for n in menu:
            if not isinstance(n, int) or not 1 <= n <= MENU_MAX:
                return False, f"選項序號要在 1~{MENU_MAX}（收到 {n}）"
        gap = step.get("gap")
        if gap is not None and (not isinstance(gap, (int, float))
                                or not 0 < gap <= 30):
            return False, f"選項間隔要在 0~30 秒（收到 {gap}）"
    elif kind == WAIT:
        s = step.get("secs")
        if not isinstance(s, (int, float)) or not 0 < s <= 600:
            return False, "wait 的 secs 要在 0~600 秒"
    elif kind == PORTAL:
        xy = step.get("to")
        if not (isinstance(xy, list) and len(xy) == 2):
            return False, "portal 少了 to:[x,y]"
        dst = step.get("scene")
        if dst is not None and not isinstance(dst, int):
            return False, "portal 的 scene 要是場景編號"
        land = step.get("land")
        if land is not None and not (isinstance(land, list) and len(land) == 2):
            return False, "portal 的 land 要是 [x,y]"
        mdl = step.get("model")
        if mdl is not None and not isinstance(mdl, int):
            return False, "portal 的 model 要是外觀編號"
    return True, ""


def validate_entrance(ent) -> tuple[bool, str]:
    """入口傳送點的格式檢查。空的（沒設）也算合法 —— 就是「只在副本裡跑」。"""
    if not ent:
        return True, ""
    if not isinstance(ent, dict):
        return False, "不是物件"
    if not isinstance(ent.get("scene"), int):
        return False, "少了 scene（入口在**外面**那張圖的編號）"
    xy = ent.get("to")
    if not (isinstance(xy, list) and len(xy) == 2):
        return False, "少了 to:[x,y]（入口傳送點的位置）"
    for key in ("model",):
        v = ent.get(key)
        if v is not None and not isinstance(v, int):
            return False, f"{key} 要是編號"
    land = ent.get("land")
    if land is not None and not (isinstance(land, list) and len(land) == 2):
        return False, "land 要是 [x,y]"
    return True, ""


def describe_entrance(ent: dict) -> str:
    """入口傳送點給人看的一行。"""
    if not ent:
        return "沒有記入口（只能在副本裡面開跑）"
    from app.game import scene as _scene         # 迴圈匯入：用時才拉
    x, y = ent.get("to", ["?", "?"])
    m = ent.get("model")
    who = f"　{mapobj.label(m)}" if isinstance(m, int) else ""
    return (f"入口：{_scene.scene_name(ent.get('scene'))}"
            f"（{ent.get('scene')}）({x}, {y}){who}")


def describe(step: dict) -> str:
    """把一步變成人看得懂的一行（介面清單用）。"""
    kind = step.get("do")
    if kind == WALK:
        x, y = step.get("to", ["?", "?"])
        return f"走到 ({x}, {y})"
    if kind == INTERACT:
        x, y = step.get("at", ["?", "?"])
        menu = step.get("menu") or []
        tail = ("　選項 " + " → ".join(f"第{n}項" for n in menu)) if menu \
            else "　（只說話，不選項）"
        m = step.get("model")
        # ★ 有名字就印名字（「惡魔系雕像01（60049）」）—— 光看編號認不出是什麼
        #   東西（2026-09-02 使用者回報）。查不到就退回只顯示編號。
        who = mapobj.label(m) if isinstance(m, int) else f"外觀 {m}"
        gap = step.get("gap")
        return (f"對話 ({x}, {y})　{who}{tail}"
                + (f"　間隔 {gap} 秒" if gap and menu else ""))
    if kind == CLEAR:
        return "清光周圍的怪"
    if kind == WAIT:
        return f"等 {step.get('secs')} 秒"
    if kind == PORTAL:
        x, y = step.get("to", ["?", "?"])
        land = step.get("land")
        if not land:
            # ⚠ 還沒看到出口就要講出來 —— 沒看到 ≠ 沒有出口，但也不能裝作記到了。
            m = step.get("model")
            who = f"　{mapobj.label(m)}" if isinstance(m, int) else ""
            return (f"走進傳點 ({x}, {y}){who}"
                    "　⚠ 還沒看到出口（走進去一次就會記起來）")
        tail = ""
        dst = step.get("scene")
        if dst is not None:
            from app.game import scene as _scene   # 迴圈匯入：用時才拉
            tail = f"　{_scene.scene_name(dst)}"
        m = step.get("model")
        who = f"　{mapobj.label(m)}" if isinstance(m, int) else ""
        return (f"走進傳點 ({x}, {y}){who} → 出口 "
                f"({land[0]:g}, {land[1]:g}){tail}")
    return f"？{kind}"


def map_at(script: Script, upto: int | None = None) -> int | None:
    """跑到第 `upto` 步（不含）時**應該**站在哪一張圖（map_key）。

    起點是腳本的地圖章，每經過一個記了目的地的 `portal` 就換一次。
    `upto=None` ＝整份腳本跑完之後那一張。
    傳點還沒記到目的地（`scene` 是 None）就回 None ＝「不知道」，
    呼叫端不要拿 None 去比對（不知道 ≠ 對不上）。
    """
    key = script.scene
    for st in script.steps[:upto]:
        if st.get("do") != PORTAL:
            continue
        key = st.get("scene")
        if key is None:
            return None
    return key


def check_map(script: Script, grid, scene_id: int | None,
              map_key) -> tuple[bool, str]:
    """腳本跟眼前這張地圖對不對得上。對不上就要**大聲停用**，不要盲走。

    `map_key` 傳 `scene.map_key`（剝掉分流序號）；不想比場景就傳 None。
    """
    if script.scene is not None and map_key is not None:
        here = map_key(scene_id)
        want = map_key(script.scene)
        if here is None:
            return False, "讀不到目前場景編號"
        if here != want:
            return False, (f"這裡不是腳本寫的那張圖"
                           f"（腳本 {want}、現在 {here}）")
    fp = script.map or {}
    if fp and grid is not None:
        now = fingerprint(grid)
        if (fp.get("w"), fp.get("h")) != (now["w"], now["h"]):
            return False, (f"地圖大小變了（腳本 {fp.get('w')}x{fp.get('h')}、"
                           f"現在 {now['w']}x{now['h']}）—— 官方改過地圖？")
        # ⚠⚠ 可走格數**不是固定的**（2026-09-02 實測踩到）：同一個副本的
        #   分流 5 是 22675 格、分流 6 是 22659 格，差 16 格 —— 因為副本裡有
        #   會開關的門（`靜態-資料片門01關`），門關著那幾格就不能走。
        #   所以只能比「有沒有差很多」，比精確值會在每一趟都誤報成「官方改圖」。
        want, got = fp.get("walkable") or 0, now["walkable"]
        if want and abs(got - want) > want * WALKABLE_TOLERANCE:
            return False, (f"可走格數差太多（腳本 {want}、現在 {got}）"
                           f"—— 官方改過地圖？")
    return True, ""
