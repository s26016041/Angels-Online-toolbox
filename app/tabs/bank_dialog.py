"""「存個人倉庫」小視窗（掛機設定裡那顆鈕開的）。

視窗本體在 app/tabs/itemlist_dialog.py（跟「存公會倉庫」「自動丟棄」共用）；這裡只給它一份 Spec：
左邊＝這台背包裡**能存倉庫**的東西（表標「不可存倉庫」的不列，見 bank.eligible）、
右邊＝要存的清單（config `bank.items`，全部分身共用）；回程補給到銀行時每台照這張存
自己背包裡有的（⛔ 2026-09-23 起不再讀遊戲補給頁的處理清單）。
"""
from __future__ import annotations

from app.game import bank, discard, guildbank
from app.tabs.itemlist_dialog import ItemListDialog, Spec


def spec() -> Spec:
    # ⚠ 在呼叫當下才讀 bank 模組的函式（離線測試會換掉那個模組）
    return Spec(
        title="存個人倉庫",
        left_label="這台背包裡能存的",
        right_label="要存個人倉庫的清單（全部分身共用）",
        verb="存",
        wanted=bank.wanted,
        set_wanted=bank.set_wanted,
        candidates=bank.candidates,
        why_not_hint="不可存倉庫",
        others=lambda: guildbank.wanted() | discard.wanted(),
    )


class BankDialog(ItemListDialog):
    def __init__(self, parent, scanner, who: str, clients=None) -> None:
        super().__init__(parent, scanner, who, spec(), test_run=None, clients=clients)
