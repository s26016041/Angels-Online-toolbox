"""「自動丟棄」小視窗（掛機設定裡那顆鈕開的）。

視窗本體在 app/tabs/itemlist_dialog.py（跟「存公會倉庫」共用）；這裡只給它一份 Spec：
左邊＝這台背包裡的東西（全部都能丟）、右邊＝要丟的清單（config `discard.items`，
全部分身共用）；掛機中每隔 discard.GAP 秒讀一次背包，清單上的東西當場丟（不用走去哪裡）。
"""
from __future__ import annotations

from app.game import bank, discard, guildbank
from app.tabs.itemlist_dialog import ItemListDialog, Spec


def spec() -> Spec:
    # ⚠ 在呼叫當下才讀 discard 模組的函式（離線測試會換掉那個模組）
    return Spec(
        title="自動丟棄",
        left_label="這台背包裡的東西",
        right_label="要自動丟棄的清單（全部分身共用）",
        verb="丟",
        wanted=discard.wanted,
        set_wanted=discard.set_wanted,
        candidates=discard.candidates,
        test_text="🧪 現在就丟（就地測試）",
        test_tip=("把清單上、這台背包裡有的東西現在就丟掉（不用走去哪裡）。\n"
                  "⚠ 會真的把東西丟掉，丟了拿不回來。"),
        others=lambda: bank.wanted() | guildbank.wanted(),
    )


class DiscardDialog(ItemListDialog):
    def __init__(self, parent, scanner, who: str, test_run=None, clients=None) -> None:
        super().__init__(parent, scanner, who, spec(), test_run=test_run, clients=clients)
