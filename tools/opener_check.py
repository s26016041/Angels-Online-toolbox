"""首次攻擊（開場那一招）的閘門離線測試 —— 驗 `KeyWorker._opener_gate` 的規格。

驗的規格：
    ① 同一隻怪**只放一次**首發：確認放出去了就轉技能鍵輪迴，不再送首發
    ② 換一隻怪就重新上鎖 —— 下一隻照樣先等首發放出去才接輪迴
    ③ 「放出去了」的唯一確認＝施放廣播（castwatch），還沒收到就一直送首發
    ④ ★★★ 2026-09-13 雪狐實錄：**hook 被卸掉之後不准無限等** ——
       `castwatch` 物件還在、但 `.active` 是 False（`casts_since()` 回空清單），
       舊寫法只判斷「物件在不在」→ `fired()` 永遠 False → 首發永遠不解鎖，
       實機 15 秒只出現幻影刺殺Ⅳ、極致劈擊Ⅳ 一次都沒放。現在要退化成
       「送一次就算」並在狀態列講一句。
    ⑤ 完全沒裝監聽（None）也走同一條退化路
    ⑥ SP 不夠（等下去也不會好）→ 跳過首發，直接輪迴
    ⑦ 沒設首發／那個鍵上沒技能 → 不上鎖
    ⑧ `_sync_castwatch`：hook 裝過又不見了 → 把死掉那份清乾淨並**重裝一次**
       （⛔ 不可以卡在 `_cw_failed` 那條 return 上，那是給「一開始就裝不起來」用的）

用法：py tools\\opener_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []
VK = farm_tab.quickbar.VK_F1          # 首發鍵＝F1
SID = 743                             # 幻影刺殺Ⅳ（雪狐那台的首發）
BYKEY = {VK: SID}


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeHook:
    """施放廣播監聽的替身。`active=False` ＝ hook 已經不在遊戲裡了。"""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.seen: list[tuple[int, int]] = []   # 之後要「收到」的廣播
        self.count = 0

    def write_count(self) -> int:
        return self.count

    def cast(self, srv: int, sid: int) -> None:
        """伺服器回了一包「srv 放出 sid」。"""
        self.count += 1
        self.seen.append((self.count, srv, sid))

    def fired(self, since: int, srv: int, sid: int) -> bool:
        # 真的那支在 _active=False 時直接回空清單 → 永遠 False（本測的重點）
        if not self.active:
            return False
        # ⚠ 照 since 過濾 —— 真的那支只看「上鎖之後」的廣播，
        #   不然上一隻的廣播會把下一隻的鎖直接打開。
        return any(i > since and (c, k) == (srv, sid) for i, c, k in self.seen)


def make_worker(hook, srv_id: int = 0x44DA0264):
    """一個只用來跑閘門的 KeyWorker（不啟動執行緒、不碰記憶體）。"""
    kw = farm_tab.KeyWorker(0, None)
    kw.opener_vk = VK
    kw.castwatch = hook
    kw._sp_blocked = lambda sid: ""            # SP 夠（另外有一節專門測不夠）
    # 施法者伺服器ID：真的那支要讀記憶體，這裡直接給值
    farm_tab.castwatch.own_server_id = lambda sc, ent: srv_id
    farm_tab.bag.player_entity = lambda sc: 0x1000
    return kw


def gate(kw, eid: int, t: float = 0.0):
    return kw._opener_gate(eid, BYKEY, t)


print("① 收到施放廣播 → 首發只放這一次，之後轉輪迴")
hook = FakeHook(True)
kw = make_worker(hook)
check("第一拍：送首發", gate(kw, 1) == VK)
check("還沒收到廣播 → 一直送首發", gate(kw, 1, 0.5) == VK)
check("　狀態列看得到「正在等首發」", kw.open_wait > 0, str(kw.open_wait))
hook.cast(0x44DA0264, SID)
check("★ 收到「我放出這一招」的廣播 → 解鎖", gate(kw, 1, 1.0) is None)
check("　解鎖後不再送首發", gate(kw, 1, 1.5) is None)
check("　等待旗歸零（換怪計時器要能跑）", kw.open_wait == 0.0)

print("② 換一隻怪 → 重新上鎖")
check("★ 下一隻先送首發（⛔ 上一隻的廣播不算數）", gate(kw, 2, 2.0) == VK)
hook.cast(0x44DA0264, SID)
check("　放出去了就解鎖", gate(kw, 2, 2.5) is None)

print("③ ★★★ hook 被卸掉（物件還在、active=False）→ 送一次就算，⛔ 不無限等")
dead = FakeHook(active=False)
kw = make_worker(dead)
first = gate(kw, 10)
check("第一拍送首發", first == VK, str(first))
check("★★★ 第二拍就解鎖（舊寫法會永遠回 F1）", gate(kw, 10, 0.5) is None)
check("　狀態列講了原因", "監聽不可用" in kw.open_note, kw.open_note)
kw.open_note = ""
check("　換一隻也是送一次就算", gate(kw, 11, 1.0) == VK
      and gate(kw, 11, 1.5) is None)

print("④ 完全沒裝監聽（None）→ 同一條退化路")
kw = make_worker(None)
check("送一次", gate(kw, 20) == VK)
check("　接著解鎖", gate(kw, 20, 0.5) is None)
check("　有講原因", "監聽不可用" in kw.open_note, kw.open_note)

print("⑤ SP 不夠 → 跳過首發（等下去也不會好）")
kw = make_worker(FakeHook(True))
kw._sp_blocked = lambda sid: "⚡ SP 不夠，這一隻跳過首發「幻影刺殺Ⅳ」"
check("★ 直接放行輪迴（不送首發）", gate(kw, 30) is None)
check("　狀態列講了 SP 不夠", "SP 不夠" in kw.open_note, kw.open_note)

print("⑥ 沒設首發／鍵上沒技能 → 不上鎖")
kw = make_worker(FakeHook(True))
kw.opener_vk = 0
check("沒設首發", gate(kw, 40) is None)
kw.opener_vk = VK
check("鍵上沒技能", kw._opener_gate(40, {}, 0.0) is None)

print("⑦ _cast_hook()：只認真的還裝著的那一份")
kw = make_worker(FakeHook(True))
check("active → 認", kw._cast_hook() is not None)
kw.castwatch.active = False
check("★ 不 active → 當成沒有", kw._cast_hook() is None)
kw.castwatch = None
check("None → 當成沒有", kw._cast_hook() is None)

print("⑧ _sync_castwatch：hook 裝過又不見了 → 清掉並重裝一次")


class FakePage:
    """只借 `_sync_castwatch`／`_release_castwatch` 兩支真的方法來跑。"""

    _sync_castwatch = farm_tab.CharFarmPage._sync_castwatch
    _release_castwatch = farm_tab.CharFarmPage._release_castwatch
    _want_castwatch = farm_tab.CharFarmPage._want_castwatch

    def __init__(self) -> None:
        self.pid = 1234
        self._castwatch = None
        self._cw_failed = False
        self._keys = types.SimpleNamespace(castwatch=None, opener_vk=VK)
        self._buff = types.SimpleNamespace(skill=0)
        self.run_cb = types.SimpleNamespace(isChecked=lambda: True)
        self.buff_cb = types.SimpleNamespace(isChecked=lambda: False)
        self.status = types.SimpleNamespace(setText=lambda _t: None)


acquired: list[int] = []
hooks: list[FakeHook] = []


def fake_acquire(pid, owner):
    acquired.append(pid)
    hooks.append(FakeHook(True))      # 重裝＝一份**新的** hook
    return hooks[-1]


farm_tab.castwatch.acquire = fake_acquire
farm_tab.castwatch.release = lambda pid, owner: None

page = FakePage()
page._sync_castwatch()
check("第一次：裝起來了", page._castwatch is hooks[-1]
      and page._keys.castwatch is hooks[-1] and acquired == [1234],
      f"{page._castwatch} {acquired}")
page._sync_castwatch()
check("　已經裝著就不重裝", acquired == [1234], str(acquired))

hooks[-1].active = False                 # ← hook 在遊戲裡被還掉了
page._cw_failed = True                   # ← 而且之前失敗過（雪狐那台的狀態）
page._sync_castwatch()
check("★★★ hook 不見了 → 清掉死的那份、重裝一次",
      len(acquired) == 2 and page._keys.castwatch is not None
      and page._keys.castwatch.active,
      f"acquired={acquired} keys={page._keys.castwatch}")

# 重裝也失敗 → 記住失敗、不要每拍狂試
farm_tab.castwatch.acquire = lambda pid, owner: (acquired.append(pid), None)[1]
page._castwatch.active = False
page._cw_failed = False
page._sync_castwatch()
n = len(acquired)
check("　重裝失敗 → 記一筆、退化路（castwatch 清成 None）",
      page._keys.castwatch is None and page._cw_failed, str(page._cw_failed))
page._sync_castwatch()
check("　失敗過就不再每拍重試", len(acquired) == n, str(len(acquired)))

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
