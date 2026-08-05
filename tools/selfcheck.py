"""全功能自測：把每個功能的「讀取路徑」跑一遍，確認遊戲改版後還準不準。

    py tools\\selfcheck.py

★ 用的是 `app/core/health.py` 裡**跟程式內建監察同一份**的檢查，
  所以指令列看到的結果跟工具箱自己判斷的一致。
  （工具箱開起來時也會在背景跑一次；壞掉會跳通知並關閉程式。）

什麼時候跑
----------
* **遊戲更新之後**（最重要）
* 覺得哪裡怪怪的、想知道是不是位址失效
* 改完 app/game/ 底下的東西之後

怎麼看結果
----------
* **全部 ✔** —— 沒事。
* **有「位址位移」但沒有失敗** —— 遊戲改版了，但 AOB 特徵碼自己跟上了，正常用。
* **位址定位失敗** —— 那幾個函式本體被改寫了，特徵碼要重做
  （見 scratchpad/sigbuild.py 與 memory 的 aob-auto-locate）。
* **定位都好、但某個項目在所有分身上都 ✘** —— 位址對了、**欄位偏移變了**，
  AOB 救不了，只能重新逆向那個結構。
* **只有某一台失敗** —— 多半是那台在讀取畫面／剛換地圖／正在重連，不是壞掉。
  程式內建的監察也是這樣判定的（要全部失敗才算壞）。

⚠ 純讀取。不寫記憶體、不注入、不送任何封包。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import health  # noqa: E402


def main() -> int:
    # 手動跑就做深度檢查（含背包物品陣列，要多花約 8 秒）
    rep = health.check(deep=True)
    if rep.skipped:
        print("找不到遊戲視窗 —— 先把遊戲開起來。")
        return 1

    if rep.locate_failed:
        print(f"⛔ 位址定位失敗 {len(rep.locate_failed)} 個："
              + "、".join(rep.locate_failed))
    else:
        print(f"✔ 位址定位：全部命中，位移 {len(rep.locate_moved)} 個")
    for nm, old, new in rep.locate_moved:
        print(f"      ★ {nm}  {old:#x} → {new:#x}")
    print()

    ok = bad = 0
    for c in rep.clients:
        print(f"=== {c.name}（{c.account}） ===")
        for key, (passed, detail) in c.checks.items():
            ok, bad = (ok + 1, bad) if passed else (ok, bad + 1)
            print(f"   {'✔' if passed else '✘'} {key:<16}{detail}")
        print()

    broken = rep.broken
    print(f"總計：通過 {ok}　失敗 {bad}")
    if broken:
        print("\n⛔ 判定為壞掉（在所有分身上都失敗）：")
        for b in broken:
            print(f"   · {b}")
        print("\n看檔頭的「怎麼看結果」判斷是位址位移還是版面改變。")
    elif bad:
        print("\n有個別分身失敗，但不是每一台都失敗 —— "
              "多半是那台在讀取畫面／剛換地圖，不算壞掉。")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
