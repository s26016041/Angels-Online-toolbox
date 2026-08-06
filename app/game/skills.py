"""技能資料（持續時間、對象、射程、耗魔），從遊戲資源包抽出來的。

    skills.of(5424)   → Skill(id=5424, secs=1200, target='自己', rng=0, mp=30)

★ **持續時間的單位是「秒」**。使用者實測：F12 的技能 5424 表裡寫 1200，
  實際持續 20 分鐘 = 1200 秒。
⚠ 同一列的 `前置時間`／`後置時間` 卻是**毫秒**（600 = 前搖 0.6 秒）。
  同一張表混用兩種單位，不要看到數字就當成同一種。

★ 只收「有持續時間」的 10516 個（沒有持續時間的就不是 buff，用不到）。
⚠ 改版調整技能要重跑 `tools/build_skills.py`。
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass

from app.paths import resource

DATA_FILE = "assets/skills.tsv.gz"
# 名稱另外一張表（tools/build_skill_names.py，全部 19310 筆都有）——
# 主表只收 buff 類，攻擊技能查名字不能靠它。
NAMES_FILE = "assets/skill_names.tsv.gz"
# 射程另一張表（tools/build_skill_range.py，全部 19312 筆）——
# 主表只收 buff 類，攻擊技能查射程不能靠它。見 range_of()。
RANGE_FILE = "assets/skill_range.tsv.gz"
SELF_ONLY = "自己"          # 對象＝自己 的技能按了就對自己放，不用先選人


@dataclass(frozen=True)
class Skill:
    id: int
    secs: int               # 持續時間（秒）
    target: str             # '自己'（= SELF_ONLY）/ '角色' / …
    rng: int                # 射程（格）
    mp: int


_table: dict[int, Skill] | None = None


def _load() -> dict[int, Skill]:
    global _table
    if _table is None:
        out: dict[int, Skill] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) == 5:
                        out[int(p[0])] = Skill(int(p[0]), int(p[1]), p[2],
                                               int(p[3]), int(p[4]))
        except Exception:                                  # noqa: BLE001
            out = {}
        _table = out
    return _table


def of(skill_id: int) -> Skill | None:
    """查一個技能；沒有持續時間（不是 buff）或查不到就回 None。"""
    return _load().get(int(skill_id or 0))


_names: dict[int, str] | None = None


def name_of(skill_id: int) -> str:
    """技能的中文名（例如 743 → '幻影刺殺Ⅳ'）；查不到回空字串。

    跟 itemname.of 同一套做法：第一次用到才載入、之後整支程式共用；
    查不到就讓呼叫端自己退回顯示編號，絕不假裝知道。
    """
    global _names
    if _names is None:
        out: dict[int, str] = {}
        try:
            with gzip.open(resource(NAMES_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    sid, _, name = line.rstrip("\n").partition("\t")
                    if name:
                        out[int(sid)] = name
        except Exception:                                  # noqa: BLE001
            out = {}          # 檔案缺了就退化成顯示編號，不要爆掉
        _names = out
    return _names.get(int(skill_id or 0), "")


_ranges: dict[int, int] | None = None
_targets: dict[int, str] | None = None
_attacks: set[int] | None = None

GROUND = "地面"          # 對象＝地面：施放要指定位置（見 is_ground）


def _load_ranges() -> None:
    """射程／對象／攻擊型表（id \\t 射程 \\t 對象 \\t 攻擊型）。第一次用到才載入。"""
    global _ranges, _targets, _attacks
    if _ranges is not None:
        return
    rng: dict[int, int] = {}
    tgt: dict[int, str] = {}
    atk: set[int] = set()
    try:
        with gzip.open(resource(RANGE_FILE), "rt", encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 2 and p[1]:
                    rng[int(p[0])] = int(p[1])
                if len(p) >= 3 and p[2]:
                    tgt[int(p[0])] = p[2]
                if len(p) >= 4 and p[3] == "1":
                    atk.add(int(p[0]))
    except Exception:                                      # noqa: BLE001
        rng, tgt, atk = {}, {}, set()
    _ranges, _targets, _attacks = rng, tgt, atk


def is_attack(skill_id: int) -> bool:
    """這招打不打得死怪（遊戲資料的「攻擊型」）。

    ★★ 掛機的攻擊循環**只收攻擊型**。實際踩到的坑：黑狐把技能鍵勾成
      F3 瞬移術Ⅳ —— 那是位移技能，我們照樣每 0.15 秒送一次帶座標的施放，
      等於**不停往怪的位置瞬移**，距離在 0.4~24 格之間亂跳、一次都沒交戰，
      然後「10 秒沒進展 → 換一隻」無限循環（使用者回報的卡住）。
    ⚠ **只有表裡明確說「不是攻擊型」才擋**。整張表壞掉、或改版後出現表裡
      沒有的新技能，一律當成攻擊技能放出去 —— 寧可多放一招，也不要讓使用者
      的技能鍵被靜默跳過（那種故障最難查）。
    """
    _load_ranges()
    sid = int(skill_id or 0)
    if not _attacks or sid not in (_ranges or {}):
        return True                       # 表壞了／表裡沒這一筆：不要因此癱瘓
    return sid in _attacks


def target_of(skill_id: int) -> str:
    """技能的「對象」：自己／角色／地面／隊伍／屍體；查不到回空字串。"""
    _load_ranges()
    return (_targets or {}).get(int(skill_id or 0), "")


def is_ground(skill_id: int) -> bool:
    """要不要指定地面位置才放得出來（對象＝地面）。

    ★★ 這種技能**不能**用 quickbar.use（＝按 F2）發動：遊戲會跳出選範圍的
      游標等使用者點地板，角色就站在那不動（使用者實際遇到）。
      掛機要改送**帶格子座標的施放封包**（attack.cast_at）。
      例：爆彈狙擊Ⅱ、麻痺荊棘Ⅱ、瞬移術Ⅳ。全表共 856 個。
    """
    return target_of(skill_id) == GROUND


def range_of(skill_id: int) -> int | None:
    """技能射程（格）；查不到回 None（呼叫端要自己有退路）。

    ★ 為什麼不能用主表：`skills.of()` 只收**有持續時間**的 buff，
      攻擊技能一個都不在裡面 —— 而掛機要的正是攻擊技能能打多遠。
      所以另外一張 skill_range.tsv.gz（tools/build_skill_range.py）。

    ⚠ 這是**遊戲資料表的格數**，不是歐氏距離：斜角相鄰算一格，
      所以實際站位要換算（近戰射程 1 實測 2.0~2.2 格都打得到）。
    """
    _load_ranges()
    return (_ranges or {}).get(int(skill_id or 0))
