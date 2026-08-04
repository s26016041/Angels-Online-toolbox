"""全功能自測：把每個功能的「讀取路徑」跑一遍，確認遊戲改版後還準不準。

    py tools\\selfcheck.py

什麼時候跑
----------
* **遊戲更新之後**（最重要）
* 覺得哪裡怪怪的、想知道是不是位址失效
* 改完 app/game/ 底下的東西之後

它會對每個開著的分身檢查：AOB 自動定位、狀態／玩家物件、怪物清單與死活、
目前地圖、怪物範本（王／等級／HP）、分流、能量晶化欄位、角色屬性。

怎麼看結果
----------
* **全部 ✔** —— 沒事。
* **「AOB 自動定位」說有位移** —— 遊戲改版了，但特徵碼自己跟上了，正常用。
* **「AOB 自動定位」✘（有幾個失敗）** —— 那幾個的函式本體被改寫了，
  特徵碼要重做（見 scratchpad/sigbuild.py 與 memory 的 aob-auto-locate）。
* **定位都 ✔ 但下面某幾項 ✘** —— 位址對了、**欄位偏移變了**，
  那是 AOB 救不了的，只能重新逆向那個結構。

⚠ 純讀取。不寫記憶體、不注入、不送任何封包。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import charname  # noqa: E402
from app.core import window as win  # noqa: E402
from app.core.memory import MemoryScanner  # noqa: E402
from app.game import (channel, energy, entity, locate, monsters,  # noqa: E402
                      player, scene)


def main() -> int:
    ok = bad = 0

    def chk(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, bad
        if cond:
            ok += 1
        else:
            bad += 1
        print(f"   {'✔' if cond else '✘'} {label:<26}{detail}")

    wins = [w for w in win.enumerate_windows(title_contains="Angels Online")
            if "_MIDAGEONL_" in w.class_name]
    if not wins:
        print("找不到遊戲視窗 —— 先把遊戲開起來。")
        return 1
    print(f"分身 {len(wins)} 個\n")

    for w in wins:
        acc = charname.account_from_title(w.title)
        sc = MemoryScanner()
        sc.open(w.pid)
        rep = locate.warm(sc, force=True)
        name = charname.read_character_name(sc, acc) or acc
        print(f"=== {name}（{acc}） ===")

        failed, moved = locate.failed(rep), locate.moved(rep)
        chk("AOB 自動定位", not failed,
            f"{len(rep)} 個，位移 {len(moved)} 個"
            + (f"，失敗 {failed}" if failed else ""))
        for nm, old, new in moved:
            print(f"        ★ {nm}  {old:#x} → {new:#x}")

        st, pl, ents, _regions, _extra = entity.snapshot(sc)
        chk("狀態物件 / 玩家物件", st is not None and pl is not None,
            f"{st and hex(st)} / {pl and hex(pl)}")
        mons = [e for e in ents if e.is_monster]
        chk("怪物清單 + 死活狀態",
            all(m.state for m in mons) if mons else True,
            f"{len(mons)} 隻，屍體 {sum(1 for m in mons if m.dead)}")
        chk("玩家座標", entity.read_pos(sc, pl) is not None if pl else False,
            str(entity.read_pos(sc, pl)) if pl else "")

        here = scene.current(sc)
        chk("目前地圖（場景編號）", here is not None, str(here))

        idx = monsters.index_base(sc)
        chk("怪物範本表（王/等級/HP）", idx is not None,
            str(monsters.info(sc, mons[0].type_id, idx))
            if (mons and idx) else "附近沒怪，只驗到索引表")

        cur, total = channel.current(w.hwnd), channel.count(sc, w.hwnd)
        chk("分流（目前 / 總數）", cur is not None and total is not None,
            f"{cur} / {total}")

        es = energy.read(sc, st) if st else None
        names = energy.attr_names(sc)
        chk("能量晶化欄位", es is not None,
            f"能量 {es.energy}　選中 {es.result_name(names)}" if es else "")
        chk("屬性名稱（讀記憶體）", names != energy.FALLBACK_NAMES
            or len(names) == energy.ATTR_COUNT,
            "、".join(names[:4]) + "…")

        base = player.locate(sc)
        stats = player.read(sc, base) if base else None
        chk("角色屬性（等級/HP/經驗）", stats is not None,
            f"Lv{stats.level} HP{stats.hp}/{stats.max_hp} "
            f"經驗 {stats.exp_pct:.1f}%" if stats else "")
        sc.close()
        print()

    print(f"總計：通過 {ok}　失敗 {bad}")
    if bad:
        print("\n有失敗項目 —— 看檔頭的「怎麼看結果」判斷是位址位移還是版面改變。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
