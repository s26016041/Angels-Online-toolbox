"""補給中途叫停的離線測試 —— supply.run_full_supply(should_stop=…)（不碰遊戲、不碰 Qt）。

驗的規格（2026-09-09 使用者：「觸發回程補給，我把開始掛機關閉就要停止」）：
    · 沒給 should_stop ＝ 舊行為，永遠不會被中止
    · 給了而且回 True → 所有等待（`_nap`）與進度點（`note`）當場丟 Aborted
    · 外殼攔下 Aborted → **先收尾**（關商店視窗、送 0x22 離開 NPC 互動）
      再回 (False, 中止訊息)；⛔ 不收尾會把角色卡在互動狀態
    · 回呼存 thread-local：兩條執行緒各跑各的，不會互相踩
    · 不管跑完、失敗還是中止，`_ABORT.fn` 都要清乾淨（別留給下一趟）

用法：py tools\\supply_stop_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import supply                     # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


def _raises_aborted(fn) -> bool:
    try:
        fn()
    except supply.Aborted:
        return True
    except Exception:                                  # noqa: BLE001
        return False
    return False


print("\n_nap／_abort_check 的叫停")
supply._ABORT.fn = None
check("沒設回呼 → _nap 不會丟東西", not _raises_aborted(lambda: supply._nap(0)))
supply._ABORT.fn = lambda: False
check("回呼說「繼續」→ 照樣不丟", not _raises_aborted(lambda: supply._nap(0)))
supply._ABORT.fn = lambda: True
check("★★ 回呼說「停」→ _nap 丟 Aborted", _raises_aborted(lambda: supply._nap(0)))
check("★★ _abort_check 也丟", _raises_aborted(supply._abort_check))
supply._ABORT.fn = None

print("\n回呼是 thread-local（多台分身各跑各的）")
seen = {}


def _worker():
    seen["fn"] = getattr(supply._ABORT, "fn", None)


supply._ABORT.fn = lambda: True
t = threading.Thread(target=_worker)
t.start()
t.join()
check("★★ 別條執行緒看不到這條設的回呼", seen.get("fn") is None,
      f"實得 {seen.get('fn')}")
supply._ABORT.fn = None

print("\nrun_full_supply 外殼：中止要收尾、旗標要清乾淨")
CLEAN: list[str] = []
_real_full, _real_close, _real_leave = (
    supply._full_supply, supply.close_sale, supply.leave_npc)
supply.close_sale = lambda mv, sc: CLEAN.append("close_sale")
supply.leave_npc = lambda mv, sc=None: CLEAN.append("leave_npc")
try:
    # ① 跑到一半被叫停
    def _abort_midway(mv, sc, **kw):
        kw["say"]("走去維修商…")
        supply._nap(0)                    # 真的那趟：等待／進度點都會問「要不要停」
        return True, "不該走到這裡"

    supply._full_supply = _abort_midway
    said: list[str] = []
    ok, msg = supply.run_full_supply(
        object(), object(), say=said.append, should_stop=lambda: True)
    check("★★★ 被叫停 → 回 (False, 中止訊息)", ok is False and "中止" in msg,
          f"實得 {(ok, msg)}")
    check("★★ 中止前有收尾（關商店＋離開 NPC 互動）",
          CLEAN == ["close_sale", "leave_npc"], f"實得 {CLEAN}")
    check("　中止也有講一聲", any("中止" in s for s in said), f"實得 {said}")
    check("★★ 旗標清乾淨（不留給下一趟）",
          getattr(supply._ABORT, "fn", None) is None)

    # ② 沒被叫停的正常一趟：結果照舊原樣傳回來
    CLEAN.clear()
    supply._full_supply = lambda mv, sc, **kw: (True, "補完了")
    ok, msg = supply.run_full_supply(object(), object(),
                                     should_stop=lambda: False)
    check("沒叫停 → 結果原樣回來", (ok, msg) == (True, "補完了"), f"實得 {(ok, msg)}")
    check("　沒中止就不必收尾", CLEAN == [], f"實得 {CLEAN}")
    check("　旗標一樣清乾淨", getattr(supply._ABORT, "fn", None) is None)

    # ③ 裡面自己爆掉：例外照樣往上丟（呼叫端的 try 才看得到真正的原因）
    def _boom(mv, sc, **kw):
        raise RuntimeError("壞了")

    supply._full_supply = _boom
    try:
        supply.run_full_supply(object(), object(), should_stop=lambda: False)
        check("裡面爆掉 → 例外照樣往上丟", False, "沒丟")
    except RuntimeError:
        check("裡面爆掉 → 例外照樣往上丟", True)
    check("　爆掉之後旗標也清乾淨",
          getattr(supply._ABORT, "fn", None) is None)
finally:
    supply._full_supply, supply.close_sale, supply.leave_npc = (
        _real_full, _real_close, _real_leave)

print("\n補給流程裡不准再有裸的 time.sleep（會漏掉叫停）")
import pathlib                                            # noqa: E402

src = pathlib.Path(supply.__file__).read_text(encoding="utf-8")
bare = [ln for ln in src.splitlines()
        if "time.sleep(" in ln and "def _nap" not in ln
        and not ln.strip().startswith("#")]
check("★★ 只剩 _nap 裡面那一個真的 sleep", len(bare) == 1, f"實得 {bare}")

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
