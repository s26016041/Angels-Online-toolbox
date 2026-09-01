r"""副本腳本：一趟副本要照順序做哪些事，存成 JSON。

    dungeon.folder()            # 腳本資料夾（%APPDATA%\AngelsOnlineToolbox\副本）
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
`clear`（清光周圍的怪）就是一個獨立的步驟，排在對話前面，那個延遲問題
自然消失 —— 而不是靠猜一個等待秒數。

## 步驟種類

    {"do": "walk",     "to": [x, y]}                 走到這一格
    {"do": "interact", "at": [x, y], "model": 60307,  點那個物件，然後照順序
                       "menu": [1, 2]}                送對話選項（1 起算）
    {"do": "clear"}                                  把周圍的怪清光
    {"do": "wait",     "secs": 3}                    單純等幾秒

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
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import APP_DIR_NAME

FOLDER_NAME = "副本"

# 步驟種類
WALK, INTERACT, CLEAR, WAIT = "walk", "interact", "clear", "wait"
KINDS = (WALK, INTERACT, CLEAR, WAIT)

# 對話選單最多幾項（talkaction 碼只到第 10 項，見 supply.talk_option）
MENU_MAX = 10


def folder() -> Path:
    """腳本資料夾。跟 config.json 放一起（打包成 exe 之後專案目錄是唯讀的）。"""
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    p = Path(base) / APP_DIR_NAME / FOLDER_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_scripts() -> list[Path]:
    """資料夾裡所有腳本，依檔名排序。"""
    try:
        return sorted(folder().glob("*.json"))
    except OSError:
        return []


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

    # -- 讀寫 ---------------------------------------------------------
    def to_json(self) -> dict:
        return {"name": self.name, "scene": self.scene, "map": self.map,
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
    sc = Script(name=str(d.get("name") or Path(path).stem),
                scene=d.get("scene"),
                map=d.get("map") or {},
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
    elif kind == WAIT:
        s = step.get("secs")
        if not isinstance(s, (int, float)) or not 0 < s <= 600:
            return False, "wait 的 secs 要在 0~600 秒"
    return True, ""


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
        return f"對話 ({x}, {y})　外觀 {step.get('model', '?')}{tail}"
    if kind == CLEAR:
        return "清光周圍的怪"
    if kind == WAIT:
        return f"等 {step.get('secs')} 秒"
    return f"？{kind}"


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
        if fp.get("walkable") and fp["walkable"] != now["walkable"]:
            return False, (f"可走格數變了（腳本 {fp['walkable']}、"
                           f"現在 {now['walkable']}）—— 官方改過地圖？")
    return True, ""
