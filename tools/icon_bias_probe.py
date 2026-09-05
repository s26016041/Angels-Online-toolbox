"""逐位元驗證圖示換色算法：把遊戲自己建的兩張 RGB565↔HSV 表讀出來跟 iconbias 算的比。

    py tools\\icon_bias_probe.py            （要開著遊戲；純讀，不寫入、不注入）

遊戲開機時 `0x67ce70` 建兩張 64K × u16 的表（[iconbias.TABLE_A_PTR]／[TABLE_B_PTR]），
換色只查這兩張表；兩張表逐位元一樣 ＝ 我們畫出來的顏色跟遊戲一樣。
結束碼：0 全對、1 有差、2 找不到遊戲／讀不到。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
except Exception:                                          # noqa: BLE001
    pass

import numpy as np                                         # noqa: E402

from app.core import preload                               # noqa: E402
from app.core.memory import MemoryScanner                  # noqa: E402
from app.game import iconbias, locate                      # noqa: E402


def main() -> int:
    wins = preload.windows()
    if not wins:
        print("找不到遊戲視窗")
        return 2
    sc = MemoryScanner()
    sc.open(wins[0].pid)
    locate.warm(sc)
    print(f"pid {wins[0].pid}　A 指標 @ {iconbias.TABLE_A_PTR:#x}　B 指標 @ {iconbias.TABLE_B_PTR:#x}")
    bad = False
    ours = iconbias.tables()
    for label, ptr, mine in (("A RGB565→HSV", iconbias.TABLE_A_PTR, ours[0]),
                             ("B HSV→RGB565", iconbias.TABLE_B_PTR, ours[1])):
        raw = sc._read_bytes(ptr, 4)
        if not raw:
            print(f"✘ {label}：讀不到指標")
            return 2
        table = struct.unpack("<I", bytes(raw))[0]
        if not 0x10000 <= table < 0x7FFF0000:
            print(f"✘ {label}：指標不合理 {table:#x}（遊戲還沒建表？）")
            return 2
        data = sc._read_bytes(table, 0x20000)
        if not data or len(data) != 0x20000:
            print(f"✘ {label}：表讀不到（{table:#x}）")
            return 2
        theirs = np.frombuffer(bytes(data), dtype="<u2")
        diff = np.nonzero(theirs != mine)[0]
        if len(diff):
            bad = True
            print(f"✘ {label}：{len(diff)} 項不一樣，例如 "
                  + ", ".join(f"[{i:#06x}] 遊戲 {int(theirs[i]):#06x} 我們 {int(mine[i]):#06x}"
                              for i in diff[:6]))
        else:
            print(f"✔ {label}：65536 項逐位元一樣（{table:#x}）")
    print("結論：" + ("有差 —— 別發版，先查 iconbias 哪一步算錯" if bad else
                    "換色算法跟遊戲一模一樣 ✅"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
