"""掛機設定（掛機頁右上角「掛機設定」小視窗存的值）—— **全部分身共用**。

2026-09-06 使用者要求：自動掛機右上角一顆「掛機設定」鈕，按了跳小視窗。目前兩項：
  · 負重設定 ＝ 補給時藥水要買到負重的幾 %（原本寫死 `supply.FILL_PCT` 95%）
  · 補給要去哪座城（2026-09-18）＝ 回城改用趴趴GO 之後要指定的目的地，預設棕櫚基地
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

# ── 補給要去哪座城（2026-09-18 使用者要求）────────────────────────────
# ★ 為什麼會有這個設定：回城以前是燒一張天使之翼，去哪座城由遊戲決定（標記點）。
#   2026-09-18 實測確認趴趴GO **不用道具、沒冷卻、野外也能送**
#   （memory `jumpmap-teleport`），回城改成自己飛 —— 飛就得指定目的地。
#
# ⚠ 存的是**場景編號**不是名字：下拉選單存標籤被坑過
#   （memory `exe-vs-py-differences`：exe 的標籤會多帶「（內建）」）。
CFG_CITY = "farm.supply_city"
CITY_DEFAULT = 88                # 棕櫚基地（使用者指定的預設）

# ★ 清單只放**有藥水商人**的城（2026-09-18 使用者定「5 個就好」）。
#   補給城表 `supply.NPC_TABLE` 有 16 張，但只有這 5 張有 'buy' —— 其餘只有
#   銀行／維修，飛過去買不到水，那趟補給等於半殘。
#   ⚠ 這五個編號是從 NPC_TABLE 篩出來的，不是手打；改版增減補給商要重跑
#     `py toolsuild_supply_merchants.py` 之後回來對一次（見 _patchCheck 第 7 步）。
SUPPLY_CITIES: tuple[int, ...] = (88, 3, 26, 29, 38)


def clamp_city(raw, default: int = CITY_DEFAULT) -> int:
    """把任何輸入夾成清單裡的場景編號；不在清單裡 → 預設（安全退化，不猜）。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v in SUPPLY_CITIES else default


def supply_city() -> int:
    """補給要去哪座城（場景編號）。"""
    return clamp_city(config.get(CFG_CITY, CITY_DEFAULT))


def set_supply_city(value) -> int:
    """寫回並存檔（⚠ config.set 不寫檔，要接 save()）。回實際存進去的值。"""
    v = clamp_city(value)
    config.set(CFG_CITY, v)
    config.save()
    return v


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
