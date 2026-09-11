r"""「被伺服器拉回」的善後 —— 離線測試（offscreen Qt＋假遊戲層）。

    py tools\rollback_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

背景（2026-09-11 使用者：「我們一直被伺服器拉回，會不會是走路封包送太快？」）
--------------------------------------------------------------------------
實測（tools/rollback_probe.py 10Hz 純讀、148 秒 5 台）**不是送太快**：
走路指令中位 1.5 秒才一次，而拉回都發生在「連續跑了 2 秒以上」之後，一次退回
0.7~0.9 秒前走過的位置（6~7 格），終點與整條軌跡拿地形圖驗過都可走、沒有切角。
＝客戶端跑在伺服器前面、伺服器定期往回同步 —— 這件事擋不掉（8/18 白狐 50 分鐘
實錄已經把客戶端側的可疑因子全檢定過）。使用者定的是「B 復原型」：拉回擋不掉，
但**拉回之後的連鎖壞事**要修掉。這支驗的就是那三件事：

  ① 舊路作廢（`_way` 清空、逼下一拍重算）
  ② 卡住錨點跟著人走、卡住秒數歸零（不然倒退會被當成「一直沒進展」）
  ③ 死亡訊號降級：拉回後 ROLLBACK_GRACE 秒內「物件不見了」不算死亡證據
     （動畫 'Dead'／死亡旗標是硬證據，不受影響）

以及「不可以誤判」的兩種：往前瞬移（順移技能／傳點）、換地圖那一拍。
"""
from __future__ import annotations

import os
import sys
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import entity                         # noqa: E402
from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


def build_page():
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account="acct", char_name="小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.notices = []
    page.notify = lambda msg: page.notices.append(msg)
    return page


def walk(page, pts, scene=122, gap=0.06):
    """照著座標一路走過去（每一步之間讓時鐘前進 gap 秒）。回最後一次的判定。"""
    out = False
    for x, y in pts:
        out = page._note_rollback((x, y), scene)
        time.sleep(gap)
    return out


print("① 正常跑步不算拉回（一拍 0.6 格，門檻 3 格）")
page = build_page()
page._way = [(1.0, 1.0)]
res = walk(page, [(10.0 + i * 0.6, 10.0) for i in range(20)])
check("跑了 20 步都沒被判成拉回", res is False and page._rollbacks == 0,
      f"實得 {page._rollbacks} 次")
check("⛔ 沒有亂清路徑快取", page._way == [(1.0, 1.0)], f"實得 {page._way}")

print("② 一拍倒退 7 格、落點是剛剛走過的地方 → 拉回＋三件善後")
page = build_page()
page._way = [(1.0, 1.0)]
page._path_pts = 5
page._stuck = 9.9
page._anchor = (0.0, 0.0)
page._gone = 1
walk(page, [(10.0 + i * 0.6, 10.0) for i in range(20)])   # 從 10.0 跑到 21.4
back = (14.0, 10.0)                                        # 退回約 0.8 秒前的位置
hit = page._note_rollback(back, 122)
check("判定是拉回", hit is True)
check("① 舊路作廢", page._way == [] and page._path_pts == -1,
      f"實得 way={page._way} pts={page._path_pts}")
check("② 卡住錨點跟著人走、秒數歸零",
      page._anchor == back and page._stuck == 0.0,
      f"實得 anchor={page._anchor} stuck={page._stuck}")
check("③ 死亡訊號降級：寬限被推到未來",
      page._atk.rollback_until > time.monotonic(),
      f"實得 {page._atk.rollback_until - time.monotonic():.2f}s")
check("「掃不到目標」的計數也歸零", page._gone == 0)
check("有記到次數（診斷用）", page._rollbacks == 1, f"實得 {page._rollbacks}")

print("③ 往前瞬移（順移技能／傳點）不算拉回 —— 落點沒走過")
page = build_page()
page._way = [(1.0, 1.0)]
walk(page, [(10.0 + i * 0.6, 10.0) for i in range(20)])
hit = page._note_rollback((21.4, 25.0), 122)      # 往旁邊跳 15 格，沒走過
check("不算拉回", hit is False and page._rollbacks == 0)
check("⛔ 路徑快取沒被清掉", page._way == [(1.0, 1.0)])

print("④ 換地圖那一拍不算拉回")
page = build_page()
walk(page, [(10.0 + i * 0.6, 10.0) for i in range(10)])
hit = page._note_rollback((10.0, 10.0), 130)      # 場景不同
check("不算拉回", hit is False and page._rollbacks == 0)

print("⑤ 目標寫入執行緒：寬限內「物件不見了」不算死，硬證據照算")
DEAD = {"alive": False, "state": "Wait", "flag": 0}
real_entity = farm_tab.entity
fake = types.SimpleNamespace(
    read_target_checked=lambda sc, st: (True, 0, 100),
    read_live=lambda sc, ent: (DEAD["alive"], DEAD["state"], (0.0, 0.0),
                               DEAD["flag"]),
    is_alive=lambda sc, ent: DEAD["alive"],
    looks_dead=entity.looks_dead,
    set_target_id=lambda sc, st, eid: None,
    STATE_DEAD=entity.STATE_DEAD,
    DEAD_FLAG=entity.DEAD_FLAG,
)
farm_tab.entity = fake
try:
    tgt = farm_tab.TargetWorker(FakeSC())
    tgt.packets = True
    ent = entity.Entity(0x1000, 0x55, 999, "怪")
    seen: list = []
    tgt.died.connect(lambda eid, ok: seen.append((eid, ok)))

    tgt.attack(0x2000, ent)
    tgt.rollback_until = time.monotonic() + 5.0
    tgt.step()
    APP.processEvents()
    check("⛔ 寬限內：只有『物件不見了』→ 不判死", seen == [], f"實得 {seen}")

    DEAD["state"] = "Dead"                     # 硬證據：屍體動畫
    tgt.step()
    APP.processEvents()
    check("★ 寬限內：動畫 'Dead' 照樣判死", len(seen) == 1, f"實得 {seen}")

    seen.clear()
    DEAD["state"], DEAD["flag"] = "Wait", entity.DEAD_FLAG
    tgt.attack(0x2000, ent)
    tgt.step()
    APP.processEvents()
    check("★ 寬限內：死亡旗標＝7 照樣判死", len(seen) == 1, f"實得 {seen}")

    seen.clear()
    DEAD["flag"] = 0
    tgt.attack(0x2000, ent)
    tgt.rollback_until = 0.0                   # 寬限過了
    tgt.step()
    APP.processEvents()
    check("寬限過後：物件不見了 → 照舊判死", len(seen) == 1, f"實得 {seen}")
finally:
    farm_tab.entity = real_entity

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
