"""已找到並驗證的遊戲記憶體座標（指標路徑），寫死在這裡供監控使用。

╔══════════════════════════════════════════════════════════════════════╗
║  這裡就是你「把經驗值等座標寫死在程式裡」的地方。                     ║
╚══════════════════════════════════════════════════════════════════════╝

工作流程：
  1.（開發期，只做一次）用「記憶體掃描」分頁找出某個值（如經驗值）的位址，
     再按「找指標路徑」得到一條指標路徑。
  2. 重開遊戲 2~3 次，確認同一條路徑每次都還能解回正確的值（穩定），
     不穩定的丟掉。
  3. 把穩定的那條填進下面 SIGNATURES。之後 GameReader.read("exp") 就能在
     任何一次啟動、任何一個多開分身上讀到它，永遠不必再搜尋。

為什麼指標路徑重開也有效：
  它記的是「模組名 + 偏移」而非絕對位址；執行時查該行程「當下」的模組基底
  再逐層解析，所以就算 ASLR 讓位址每次不同也能還原。

多開為什麼也沒問題：
  每個分身是獨立行程。用各自的行程控制代碼去解析「同一條」路徑，就會分別
  得到各分身自己的值（見 app/core/game_reader.py 的 find_instances）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.memory import PointerPath, VALUE_TYPES, ValueType


@dataclass(frozen=True)
class Signature:
    """一個具名座標：一條指標路徑 + 它的數值型別。"""

    path: PointerPath
    vt_key: str = "int32"
    label: str = ""

    @property
    def vt(self) -> ValueType:
        return VALUE_TYPES[self.vt_key]


def sig(
    module: str,
    base_offset: int,
    offsets: list[int],
    vt_key: str = "int32",
    label: str = "",
) -> Signature:
    """建立 Signature 的便捷函式。

    對應「記憶體掃描」分頁裡看到的路徑寫法，例如
        ["game.exe"+0x3F1A20] +0x10 +0x2C
    就寫成
        sig("game.exe", 0x3F1A20, [0x10, 0x2C], "int32", "經驗值")
    靜態值（0 層，路徑本身就是位址）則 offsets 給 []。
    """
    return Signature(PointerPath(module, base_offset, list(offsets)), vt_key, label)


# ---------------------------------------------------------------------------
# 具名座標登錄表：name -> Signature
# 用「記憶體掃描」分頁找到並「重開驗證」過後，填在這裡。
# ---------------------------------------------------------------------------
SIGNATURES: dict[str, Signature] = {
    # ⚠ 這條經過實測「重開後篩選」證實不穩定（最深一層會變 null），已停用。
    #    請用掃描分頁重找、並用「儲存全部候選 → 重開篩選」挑出穩定的再填回。
    # "exp": sig("angel.dat", 0x618F3C, [0xAC, 0x124, 0xA0], "int32", "經驗值"),
}
