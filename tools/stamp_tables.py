"""對著開著的遊戲蓋「資料表核對戳記」（assets/table_stamp.json）。

    py tools\\stamp_tables.py

什麼時候跑
----------
**遊戲改版、且你已經重跑 tools/build_*.py 核對過 assets 資料表之後。**
蓋了章，工具箱就把「目前這版遊戲」當成資料表的已核對基準；下次遊戲再換版，
掛機分頁會亮警示提醒重新核對（機制見 app/game/tablestamp.py）。

⚠ 純讀取：只讀 PE 表頭 4KB 算 CRC，不寫遊戲記憶體、不送封包。
⚠ 別在「還沒核對資料表」時蓋章 —— 那等於把警示靜音，回到安靜過期的老路。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import window as win          # noqa: E402
from app.core.memory import MemoryScanner   # noqa: E402
from app.game import locate, tablestamp     # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "assets" / "table_stamp.json"


def main() -> int:
    sc = None
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name:
            continue
        s = MemoryScanner()
        try:
            s.open(w.pid)
        except Exception:                                  # noqa: BLE001
            continue
        sc = s
        break
    if sc is None:
        print("⛔ 找不到遊戲 —— 先把遊戲開起來（任何一台分身都行）。")
        return 1

    try:
        locate.warm(sc)
        ident = locate.image_identity()
        if ident is None:
            print("⛔ 讀不到遊戲模組（剛開還在載入？），等進遊戲再跑一次。")
            return 1
        base, crc = ident

        old = tablestamp.read_stamp()
        OUT.write_text(json.dumps({
            "crc": crc,
            "base": base,
            "date": date.today().isoformat(),
            "note": "資料表（assets/*.tsv* 與程式內小表）最後一次核對時的遊戲映像身分",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if old is None:
            print(f"✔ 首次蓋章：映像 CRC {crc:#010x} → {OUT}")
        elif old == crc:
            print(f"✔ 重新蓋章（遊戲沒換版）：映像 CRC {crc:#010x}")
        else:
            print(f"✔ 蓋章更新：映像 CRC {old:#010x} → {crc:#010x}")
            print("   （前提是你已重跑 tools/build_*.py 核對過資料表！）")

        bad = locate.failed()
        if bad:
            print(f"⚠ 另外：有 {len(bad)} 個位址定位失敗（跟資料表無關，"
                  "但代表 AOB 特徵要重做）：" + "、".join(bad[:5]))
        return 0
    finally:
        sc.close()


if __name__ == "__main__":
    sys.exit(main())
