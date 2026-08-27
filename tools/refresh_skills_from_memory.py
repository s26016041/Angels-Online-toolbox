"""用遊戲自己載進記憶體的 Magic 表，刷新 `assets/skills.tsv.gz` 的持續時間。

    py tools\\refresh_skills_from_memory.py            # 只看差異，不寫檔
    py tools\\refresh_skills_from_memory.py --write    # 真的更新那張表

## 為什麼要有這支

`skills.tsv.gz` 是從資源包 `setting/base/magic.xml` 抽出來的（見 build_skills.py）。
官方調整技能數值時它就過期，而**重新解包要用 D:\\RPGViewer 那個 GUI，我們代跑
不了** —— 結果就是掛機頁的「資料表過期」警示一直亮著、使用者被丟一件雜事。

但那份資料**遊戲自己會載進記憶體**（Magic 範本表，見 skillcost.TABLE_PTR）：
持續時間在範本 +0x100，`recheck_tables` 本來就是拿它來對帳的。所以「把表刷新到
跟這一版遊戲一致」這件事，不必等解包 —— 直接從記憶體讀就是同一份內容。

⚠⚠ **這不牴觸「表就是權威」**（memory `table-is-authority`）：那條規矩講的是
**執行時以表為準、不准改成讀記憶體繞過**。這支沒有改讀取路徑，它只是把表的
內容更新成這一版遊戲的值 —— 跟重新解包的結果應該一樣，只是不必開 GUI。

⚠ 只刷新**持續時間**這一欄。`對象`（自己／角色）在記憶體裡還沒找到對應欄位，
  原樣保留；`射程`／`消耗MP` 也保留（那兩張各自有自己的對帳，目前全對）。
⚠ 記憶體讀不到或值不合理的那一列**原樣不動** —— 寧可留舊值，不可寫垃圾。

⚠ 純讀遊戲、只寫我們自己的 assets 檔；不碰遊戲記憶體。
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.core import charname, preload                        # noqa: E402
from app.core.memory import MemoryScanner                     # noqa: E402
from app.game import locate, skillcost                        # noqa: E402

TABLE = ROOT / "assets" / "skills.tsv.gz"
# 合理性上限（秒）：超過就當讀到垃圾，那一列不動。30 天跟 buffs.STALE_AFTER 同級。
MAX_SECS = 30 * 24 * 3600


def duration_of(sc, sid: int) -> int | None:
    """從遊戲載進記憶體的 Magic 範本讀持續時間（秒）；讀不到／不合理回 None。

    ⚠ 跟 `tools/recheck_tables.py` 對帳用的是**同一個欄位**（範本 +0x100，
      當年拿這張表的 10516 筆去掃「哪個偏移對全部樣本都成立」掃出來的）。
    """
    tmpl = skillcost.template(sc, sid)
    if tmpl is None:
        return None
    raw = sc._read_bytes(tmpl + skillcost.OFF_DURATION_SECS, 4)
    if not raw or len(raw) < 4:
        return None
    v = int.from_bytes(bytes(raw[:4]), "little")
    return v if 0 <= v <= MAX_SECS else None


def read_table():
    rows = []
    with gzip.open(TABLE, "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 5:
                rows.append(parts)
    return rows


def main() -> int:
    write = "--write" in sys.argv
    wins = preload.windows()
    if not wins:
        print("找不到遊戲視窗 —— 要開著遊戲（進到遊戲裡）才讀得到 Magic 表。")
        return 1

    rows = read_table()
    print(f"表裡有 {len(rows)} 列")

    # ★ 跨分身交叉比對：同一份 angel.dat，值必須一致；不一致就不敢動。
    per_client = {}
    for w in wins:
        acc = charname.account_from_title(w.title) or str(w.pid)
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            locate.warm(sc)
            got = {}
            for sid, *_rest in rows:
                v = duration_of(sc, int(sid))
                if v:                      # 0／None 一律當「沒讀到」
                    got[sid] = v
            per_client[acc] = got
            print(f"  {acc}: 讀到 {len(got)} 列")
        finally:
            sc.close()

    if not per_client:
        print("一台都讀不到")
        return 1

    accs = list(per_client)
    base = per_client[accs[0]]
    conflict = 0
    for sid in list(base):
        for a in accs[1:]:
            if sid in per_client[a] and per_client[a][sid] != base[sid]:
                conflict += 1
                base.pop(sid, None)
                break
    if conflict:
        print(f"⚠ {conflict} 列在不同分身讀到不同值 —— 那幾列不動")

    diffs = []
    for r in rows:
        sid, old = r[0], int(r[1])
        new = base.get(sid)
        if new is not None and new != old:
            diffs.append((sid, old, new))

    print(f"\n跟這一版遊戲對不上的：{len(diffs)} 列")
    for sid, old, new in diffs[:30]:
        print(f"   技能 {sid}: 表 {old} 秒 → 遊戲 {new} 秒")
    if len(diffs) > 30:
        print(f"   …另外 {len(diffs) - 30} 列")

    if not diffs:
        print("\n✔ 已經一致，不必更新。")
        return 0
    if not write:
        print("\n（只看差異，沒寫檔。要真的更新加 --write）")
        return 0

    fix = {sid: new for sid, _o, new in diffs}
    for r in rows:
        if r[0] in fix:
            r[1] = str(fix[r[0]])
    body = "".join("\t".join(r) + "\n" for r in rows)
    TABLE.write_bytes(gzip.compress(body.encode("utf-8"), 9))
    print(f"\n✔ 已更新 {TABLE}（{len(diffs)} 列，{TABLE.stat().st_size / 1024:.0f} KB）")
    print("   接著跑 py tools\\recheck_tables.py 確認全對，再 py tools\\stamp_tables.py 蓋章")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
