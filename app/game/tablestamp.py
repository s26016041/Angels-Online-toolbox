"""寫死資料表的核對戳記：遊戲換版時把「資料表安靜過期」變成大聲提醒。

## 為什麼需要

assets/ 底下有幾份從遊戲資源包（GAMEDATA/setting 解包）抽出來的寫死表：技能射程
`skill_range.tsv.gz`、趴趴GO 地圖 `jumpmap.tsv`、技能／物品名稱等，加上
程式裡的小表（`scene.SAME_MAP_AS`、`dailygift.REWARD_IDS`、`bag.FIRST_SLOT`…
完整清單見 memory 的 items-table-maintenance）。遊戲改版動到內容時它們
**不會報錯**，只會安靜過期：射程錯 → 走位停太遠 → 零傷害被當成效率下降；
地圖編號錯 → 趴趴GO 傳錯地方。

AOB 定位（locate.py）救不了這件事 —— 那救的是**位址**，這裡壞的是**資料
內容**。資料的真身在遊戲目錄的加密資源包裡，執行時讀不到（memory 的
pak-and-item-id-dead-ends），所以退而求其次：記住「這批表是對哪一版遊戲
核對的」（映像身分的 CRC），執行時發現遊戲換版就提醒去重跑 build 工具。

## 改版後的維護流程

    1. 對新版 GAMEDATA/setting 解包重跑 tools/build_*.py，核對表有沒有變
    2. py tools\\stamp_tables.py   ← 對著開著的遊戲蓋章（寫 assets/table_stamp.json）

## 限制（要知道的兩件事）

* 這是**代理訊號**：exe 沒變、只有資源包變的純內容更新抓不到（沒有更好的
  錨）；反過來 exe 變了但資料表沒動會多提醒一次 —— 重跑 build 工具確認
  沒變化後蓋章即可。寧可多提醒，不可安靜錯。
* 戳記檔不存在時**不警告**（剛拉專案、還沒蓋過章的情況），所以第一次
  要手動蓋一次章才開始有保護。
"""
from __future__ import annotations

import json

from app.game import locate
from app.paths import resource

STAMP_FILE = "assets/table_stamp.json"

# ⚠⚠ 訊息**不准點名具體是哪張表**。這支只是比映像 CRC 的**代理訊號**，
#   它根本不知道哪一張對不上（要知道得跑完整的 recheck_tables）。
#   舊版寫「技能射程／趴趴GO地圖等」，2026-08-27 使用者問「為何會說技能射程
#   趴趴GO」—— 當下那兩張其實**全對**（19312/19312、120/120），真正對不上的
#   是 skills.tsv.gz 的 1 筆。點名沒驗過的東西＝把使用者導向錯的地方。
#   → 改成只講「可能過期」＋指路去那支真的查得出來的工具。
_WARN = ("偵測到遊戲換版：寫死資料表還沒對這一版核對過（可能過期，也可能沒事）"
         " —— 跟 Claude 說一聲跑改版體檢，它會查出是哪一張、要不要緊")


def check() -> str | None:
    """遊戲映像跟戳記不符就回一句警告；沒事（或還沒能判斷）回 None。

    比對用的映像身分直接拿 locate.warm() 算好的（不再讀記憶體），所以
    要在 warm() 之後叫 —— 掛機分頁本來就是那個順序。
    """
    ident = locate.image_identity()
    if ident is None:
        return None                    # 還沒接上遊戲，無從判斷
    crc = read_stamp()
    if crc is None:
        return None                    # 還沒蓋過章：不能對新使用者亂警告
    return None if crc == ident[1] else _WARN


def read_stamp() -> int | None:
    """戳記裡記的 CRC；檔案不存在／壞掉回 None（當成沒蓋過章）。"""
    try:
        data = json.loads(resource(STAMP_FILE).read_text(encoding="utf-8"))
        return int(data["crc"])
    except Exception:                                      # noqa: BLE001
        return None
