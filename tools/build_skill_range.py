"""把每個技能的「射程」與「對象」抽成小表，給 app/game/skills.py 用。

    py tools\\build_skill_range.py [GAMEDATA/setting資料夾]

為什麼要獨立一張表：`assets/skills.tsv.gz` 只收**有持續時間**的技能（buff），
攻擊技能全都不在裡面 —— 而掛機要知道的正是攻擊技能能打多遠。

★ 射程的單位是「格」。實測（雪狐 82 級、破甲劈擊Ⅳ 射程 1）：
  站 2.0~2.2 格 3 秒打死，站 7 格以上 12 秒 101 次出手零傷害。
  所以歐氏距離的有效值約 = 射程 + 1（斜角相鄰算一格）。

⚠ 遊戲改版調整技能要重跑這支。
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets" / "skill_range.tsv.gz"
ROW = re.compile(r"<法術 ([^>]*)/>")
ATTR = re.compile(r'([\w一-鿿]+)="([^"]*)"')


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "GAMEDATA" / "setting"
    src = None
    for rel in ("base/magic.xml", "BASE/MAGIC.XML"):
        if (setting / rel).is_file():
            src = setting / rel
            break
    if src is None:
        sys.exit("⛔ 找不到 base/magic.xml")

    text = src.read_text(encoding="utf-8", errors="replace")
    rows = []
    for m in ROW.finditer(text):
        a = dict(ATTR.findall(m.group(1)))
        if "編號" not in a:
            continue
        # 對地技能（順移之類）用「範圍」，一般攻擊技能用「射程」
        rng = a.get("射程") or a.get("範圍") or 0
        # ★ 對象決定「怎麼施放」：地面＝要指定位置（按鍵會跳出選範圍的游標，
        #   所以掛機必須改送帶座標的施放封包）；其餘可以直接叫遊戲的快捷鍵。
        # ★ 攻擊型：分辨「這招打不打得死人」。瞬移術、歃血狂暴、聖療術、
        #   復活術都不是攻擊型 —— 放進攻擊循環只會浪費甚至幫倒忙
        #   （實際踩到：黑狐勾了 F3 瞬移術，等於每 0.15 秒往怪的位置瞬移一次）。
        # ★ 召喚型：`動態參數1` ＝召喚出來的**怪物範本編號**（實體的 type_id）。
        #   實機對照：781 召喚噬魂怪Ⅰ → 5014（黑狐實測召喚物 type_id 正是 5014）；
        #   150 召喚吸血鬼Ⅲ → 222 → str_monster 1200000222「死亡吸血鬼Ⅲ」。
        #   自動召喚認養**不能**拿「技能名去掉『召喚』」去比名字 ——
        #   吸血鬼系的召喚物就不叫「吸血鬼Ⅲ」，比名字＝永遠認不到＝每 6 秒白放
        #   （朋友實掛踩到的 bug）。⚠ 非召喚型的 動態參數1 是別的意思，不能抽。
        smn = (a.get("動態參數1", "") if a.get("召喚型") == "是" else "")
        # ★ 第 6/7 欄：前置時間／後置時間（毫秒）——「施放鎖定」的長度。
        #   magic.xml **沒有**冷卻時間欄，前後搖就是這遊戲的技能 CD；
        #   掛機首發拿它當「打下一隻前要等多久」的查表值（skills.lockout_of）。
        rows.append((int(a["編號"]), int(float(rng or 0)), a.get("對象", ""),
                     1 if a.get("攻擊型") == "是" else 0,
                     int(float(smn)) if smn else "",
                     int(float(a.get("前置時間") or 0)),
                     int(float(a.get("後置時間") or 0))))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{i}\t{r}\t{t}\t{k}\t{s}\t{pre}\t{post}\n"
                   for i, r, t, k, s, pre, post in rows)
    OUT.write_bytes(gzip.compress(body.encode("utf-8"), 9))
    print(f"技能 {len(rows)} 個 → {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    ground = sum(1 for row in rows if row[2] == "地面")
    atk = sum(1 for row in rows if row[3])
    smn_n = sum(1 for row in rows if row[4] != "")
    lock_n = sum(1 for row in rows if row[5] or row[6])
    print(f"   其中對象=地面（要指定位置）{ground} 個、攻擊型 {atk} 個、"
          f"召喚型 {smn_n} 個、有前置/後置 {lock_n} 個")
    for probe in (737, 743, 257, 920, 330, 259, 150, 781, 946):
        hit = next((r for r in rows if r[0] == probe), None)
        print(f"   驗證 {probe}（{probe:#x}）→ 射程 {hit[1] if hit else '?'}"
              f" 對象 {hit[2] if hit else '?'} 攻擊型 {hit[3] if hit else '?'}"
              f" 召喚 {hit[4] if hit else '?'}"
              f" 前置 {hit[5] if hit else '?'} 後置 {hit[6] if hit else '?'}")


if __name__ == "__main__":
    main()
