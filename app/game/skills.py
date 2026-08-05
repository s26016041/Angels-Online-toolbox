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
SELF_ONLY = "自己"          # 對象＝自己 的技能按了就對自己放，不用先選人


@dataclass(frozen=True)
class Skill:
    id: int
    secs: int               # 持續時間（秒）
    target: str             # '自己' / '角色' / …
    rng: int                # 射程（格）
    mp: int

    @property
    def self_cast(self) -> bool:
        """按了就直接對自己生效（不用多一個選自己的動作）。"""
        return self.target == SELF_ONLY


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


def loaded() -> int:
    return len(_load())
