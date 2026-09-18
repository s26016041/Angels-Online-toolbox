"""掛機設定（掛機頁右上角「掛機設定」小視窗存的值）—— **全部分身共用**。

2026-09-06 使用者要求：自動掛機右上角一顆「掛機設定」鈕，按了跳小視窗；目前只有
一項「負重設定」＝補給時藥水要買到負重的幾 %（原本寫死 `supply.FILL_PCT` 95%）。
存 config 一個全域鍵（跟公會倉庫清單 `guildbank.items` 同一套做法），不分角色。

讀取端（`supply.run_potion_fill`）拿到的是**百分比整數**，夾在 FILL_MIN~FILL_MAX：
值壞掉（打錯／手改 config）一律退回預設 95，不會拿垃圾值去算購買量。
⚠ 慣例：config 在**主執行緒**讀（呼叫 `fill_pct()`）再帶給背景執行緒的補給，
  跟 `guildbank.wanted()` 一樣，別讓背景執行緒去碰 config。
"""
from __future__ import annotations

from app.config import config

CFG_FILL = "farm.fill_pct"       # 補給時藥水買到負重的百分比（整數；全部分身共用）
FILL_DEFAULT = 95                # 百分比不是偏移；2026-08-19 使用者指定的原值
FILL_MIN = 10                    # 低於這個等於沒買，防呆
FILL_MAX = 100


def clamp_pct(raw, default: int = FILL_DEFAULT) -> int:
    """把任何輸入夾成合法百分比；不是數字或超出範圍 → 預設值（安全退化，不猜）。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v < FILL_MIN or v > FILL_MAX:
        return default
    return v


def fill_pct() -> int:
    """補給時藥水買到負重的百分比（整數 10~100）。"""
    return clamp_pct(config.get(CFG_FILL, FILL_DEFAULT))


def set_fill_pct(value) -> int:
    """寫回並存檔（⚠ config.set 不寫檔，要接 save()）。回實際存進去的值。"""
    v = clamp_pct(value)
    config.set(CFG_FILL, v)
    config.save()
    return v
