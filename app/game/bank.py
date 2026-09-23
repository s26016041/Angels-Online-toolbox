"""個人倉庫：「存個人倉庫」清單 —— **全部分身共用同一張清單**（config `bank.items`）。

使用者 2026-09-23 定：跟「存公會倉庫」「自動丟棄」同一套小視窗；**補給回城存倉庫從此只看
這張清單**，⛔ 不再讀遊戲補給頁的處理清單（robot var AS_STRLIST/INTLIST_TODISCARD 那兩張，
已從 supply.py 拆掉）。

能不能存＝表（item.xml）沒有「不可存倉庫」（itemflags.storable）；不可交易／綁定的東西
自己的倉庫本來就收，不擋。表沒那筆一律不列、不存（查不到就少做事）。
存的動作在 supply.run_bank（開自己的倉庫 → deposit_slot → poll 序號消失；滿了關窗）。
"""
from __future__ import annotations

from app.config import config
from app.game import bag, itemflags

CFG_KEY = "bank.items"           # 清單：物品種類 ID 的 list（全部分身共用）


def wanted() -> set[int]:
    """要存個人倉庫的物品種類 ID（config 讀出來；壞值一律丟掉）。"""
    raw = config.get(CFG_KEY, []) or []
    out: set[int] = set()
    for x in raw if isinstance(raw, (list, tuple, set)) else []:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def set_wanted(ids) -> None:
    """寫回清單。⚠ config.set 不寫檔，要接 save()（memory config-set-needs-save）。"""
    config.set(CFG_KEY, sorted({int(x) for x in ids}))
    config.save()


def eligible(it: bag.Item) -> bool:
    """這件東西能不能放進自己的倉庫：只看表的「不可存倉庫」。"""
    return itemflags.storable(it.type_id)


def candidates(scanner) -> tuple[list[bag.Item], list[bag.Item], bool]:
    """(能存的, 不能存的, 整袋讀完整了嗎)。給小視窗列清單用。"""
    items, complete = bag.scan(scanner)
    ok = [it for it in items if eligible(it)]
    no = [it for it in items if not eligible(it)]
    return ok, no, complete


def pending(scanner, ids: set[int] | None) -> list[bag.Item] | None:
    """背包裡「在清單上而且能存」的東西；背包讀不到回 None（⚠ None ≠ 空）。"""
    if not ids:
        return []
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    return [it for it in items if it.type_id in ids and eligible(it)]
