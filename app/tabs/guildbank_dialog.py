"""「存公會倉庫」小視窗（掛機設定裡那顆鈕開的）。

視窗本體在 app/tabs/itemlist_dialog.py（跟「自動丟棄」共用）；這裡只給它一份 Spec：
左邊＝這台背包裡**能存公會倉庫**的東西（不可交易／不可存倉庫／綁定次數用完的不列，
見 guildbank.eligible）、右邊＝要存的清單（config `guildbank.items`，全部分身共用）；
回程補給到銀行時每台照這張存自己背包裡有的。
"""
from __future__ import annotations

from app.game import bank, discard, guildbank
from app.tabs.itemlist_dialog import ItemListDialog, Spec


def spec() -> Spec:
    # ⚠ 在呼叫當下才讀 guildbank 模組的函式（離線測試會換掉那個模組）
    return Spec(
        title="存公會倉庫",
        left_label="這台背包裡能存的",
        right_label="要存公會倉庫的清單（全部分身共用）",
        verb="存",
        wanted=guildbank.wanted,
        set_wanted=guildbank.set_wanted,
        candidates=guildbank.candidates,
        why_not_hint="不可交易／不可存倉庫／綁定用完",
        # ⛔ 「🧪 現在就存」測試鈕 2026-09-23 使用者說不需要（guildbank.run_here 留著給工具用）
        others=lambda: bank.wanted() | discard.wanted(),
    )


class GuildBankDialog(ItemListDialog):
    def __init__(self, parent, scanner, who: str, test_run=None, clients=None) -> None:
        super().__init__(parent, scanner, who, spec(), test_run=None, clients=clients)
