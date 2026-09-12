# -*- coding: utf-8 -*-
"""「誰在打我」那一套的離線回歸（圍毆反擊＋挑目標優先）—— 不需要開遊戲。

    py tools\\gang_check.py

驗的是五件**會安靜做錯事**的地方：
  ① 「誰在打我」只認 `+0x34C == 我的實體編號`，而且會重驗身分
     （位址被回收給別隻時不准算進來）。
  ② ⚠⚠ **屍體不准算**（2026-09-12 使用者實機回報的誤判真因：怪死掉之後
     那個欄位還留著最後打的人，實測 45 秒裡 Dead 指著我的有 352 拍次）。
  ③ 湊不到人數不出手；每一招各自比自己的射程。
  ④ ⛔ 全程不寫目標欄、不送鍵、不下移動指令（＝不跟自動掛機搶、不走路）。
  ⑤ **挑目標時「正在打我的」優先**（2026-09-13 使用者定：「打我就代表那怪物是
     活的，正在打我就該打他」）—— 冷卻中、地形圖說走不到都不准把牠濾掉，
     屍體留著的舊值照樣不算。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                              # noqa: BLE001
    pass

from app.game import entity                                    # noqa: E402

MY_EID = 0x91E70087
OTHER_EID = 0x12345678


class FakeEnt:
    """夠 entity.attackers() 用的假實體（只要 addr / eid）。"""

    def __init__(self, addr: int, eid: int) -> None:
        self.addr = addr
        self.eid = eid


class FakeScanner:
    """記憶體＝一張 {位址: bytes} 的表；沒填的地方回 None（＝讀不到）。"""

    def __init__(self) -> None:
        self.mem: dict[int, bytes] = {}

    def put_entity(self, addr: int, eid: int, target: int,
                   state: str = "Wait", flag: int = 0,
                   vtable: int | None = None,
                   pos: tuple[float, float] = (0.0, 0.0)) -> None:
        span = max(entity.LIVE_SPAN, entity.attack_target_off() + 4)
        blob = bytearray(span)
        struct.pack_into("<I", blob, 0,
                         entity.VT_ENTITY if vtable is None else vtable)
        blob[entity.OFF_STATE:entity.OFF_STATE + len(state)] = state.encode()
        struct.pack_into("<I", blob, entity.OFF_ID, eid)
        struct.pack_into("<I", blob, entity.attack_target_off(), target)
        blob[entity.OFF_DEAD_FLAG] = flag
        # 座標是 16.16 定點數、一格 32 個世界單位（見 entity.TILE_UNITS）
        struct.pack_into("<II", blob, entity.OFF_POS_X,
                         int(pos[0] * 32) << 16, int(pos[1] * 32) << 16)
        self.mem[addr] = bytes(blob)

    def _read_bytes(self, addr: int, size: int):
        got = self.mem.get(addr)
        if got is None or len(got) < size:
            return None
        return got[:size]


class FakeMon(FakeEnt):
    """夠 `CharFarmPage._candidates()` 用的假怪（多了名字與種類編號）。"""

    def __init__(self, addr: int, eid: int, name: str = "稻草人",
                 type_id: int = 1) -> None:
        super().__init__(addr, eid)
        self.name = name
        self.type_id = type_id


class FakeGrid:
    """假地形圖：`reach` 裡的格才可走，也就是我站的那一塊連通區。"""

    def __init__(self, reach) -> None:
        self.reach = {tuple(t) for t in reach}

    def nearest_open(self, x: int, y: int, radius: int = 4):
        return (x, y) if (x, y) in self.reach else None

    def reachable(self, x: int, y: int, avoid=None):
        return set(self.reach) if (x, y) in self.reach else None

    def route(self, start, goal, relax: int = 4, max_cost=None, avoid=None):
        s, g = tuple(start), tuple(goal)
        if s not in self.reach or g not in self.reach:
            return None                    # ＝走不到
        path = [s]
        x, y = s
        while (x, y) != g:                 # 一格一格斜著走過去就夠算長度了
            x += (g[0] > x) - (g[0] < x)
            y += (g[1] > y) - (g[1] < y)
            path.append((x, y))
        if max_cost is not None and len(path) - 1 > max_cost:
            return None
        return path


class FakeMaps:
    def __init__(self, grid) -> None:
        self._grid = grid

    def get(self, _sc):
        return self._grid


class FakeKeys:
    def __init__(self) -> None:
        self.eid = None
        self.ent_addr = 0
        self.on = False

    def set_on(self, on: bool) -> None:
        self.on = on


class FakeAtk:
    def __init__(self) -> None:
        self.picked = None

    def attack(self, _state, ent) -> None:
        self.picked = ent

    def hold_off(self) -> None:
        self.picked = None


class FakeText:
    def __init__(self) -> None:
        self._t = ""

    def setText(self, t: str) -> None:
        self._t = t

    def text(self) -> str:
        return self._t


class FakeCB:
    def __init__(self, on: bool = False) -> None:
        self._on = on

    def isChecked(self) -> bool:
        return self._on


def make_farm_tab(sc, mons, me=(10.0, 10.0), grid=None):
    """組一個**只夠挑目標用**的掛機頁 —— 不建 UI、不開遊戲。"""
    from app.tabs import farm_tab                # noqa: PLC0415
    farm_tab.bag.my_entity_id = lambda _sc: MY_EID     # 我的實體編號（假的）

    class PickTab(farm_tab.CharFarmPage):
        """只借 CharFarmPage（單一分身那一頁）的挑目標邏輯。

        ⛔ **故意不呼叫 `__init__`**：那會建整頁 UI 並開四條 QThread
          （掃描／寫目標／送鍵／圍毆），離線回歸不需要，而且沒收掉的執行緒
          會讓腳本印了成功卻回非 0（見 memory packaging-and-release）。
        """

        def __init__(self) -> None:                    # noqa: D107
            pass

    tab = PickTab()
    tab.sc = sc
    tab.account = "check"
    tab.mons = list(mons)
    tab.state = 0x00100000
    tab.player = 0x0BADF00D
    tab._killed = {}
    tab._unreach_n = {}
    tab._reach = None
    tab._reach_grid = None
    tab._maps = FakeMaps(grid)
    tab._hp_drop_t = -1e9                 # 沒在掉血（＝弱訊號的保底不成立）
    tab._dbg_empty_t = 0.0
    tab._kills = 0
    tab._keys = FakeKeys()
    tab._atk = FakeAtk()
    tab.status = FakeText()
    tab.boss_cb = FakeCB(False)
    tab.my_pos = lambda: me
    tab.wanted = lambda: {m.name for m in mons}
    return tab


def check(name: str, got, want) -> bool:
    ok = got == want
    print(f"  {'✔' if ok else '✘'} {name}：{got!r}"
          + ("" if ok else f"（該是 {want!r}）"))
    return ok


def main() -> int:
    ok = True
    sc = FakeScanner()
    a = FakeEnt(0x10000000, 0xAAA1)
    b = FakeEnt(0x20000000, 0xAAA2)
    c = FakeEnt(0x30000000, 0xAAA3)
    d = FakeEnt(0x40000000, 0xAAA4)      # 故意不放進假記憶體＝讀不到

    print("① 誰在打我")
    sc.put_entity(a.addr, a.eid, MY_EID)          # 正在打我
    sc.put_entity(b.addr, b.eid, OTHER_EID)       # 在打別人
    sc.put_entity(c.addr, c.eid, 0)               # 沒有目標
    ents = [a, b, c, d]
    ok &= check("只挑出正在打我的那一隻",
                [e.eid for e in entity.attackers(sc, ents, MY_EID)], [a.eid])
    ok &= check("我的編號讀不到（0）＝當成沒人打我",
                entity.attackers(sc, ents, 0), [])
    ok &= check("讀不到的那隻不會被算進來（安全退化）",
                d in entity.attackers(sc, ents, MY_EID), False)

    # 位址被回收給別隻：+0x1C8 的編號對不上 → 不准算
    sc.put_entity(a.addr, 0xBBBB, MY_EID)
    ok &= check("位址被回收（編號對不上）就不算",
                entity.attackers(sc, ents, MY_EID), [])
    # vtable 被換掉（物件歸還空物件池）→ 不准算
    sc.put_entity(a.addr, a.eid, MY_EID, vtable=0x11223344)
    ok &= check("vtable 換掉（物件被回收）就不算",
                entity.attackers(sc, ents, MY_EID), [])

    print("② 屍體不准算（2026-09-12 誤判真因）")
    sc.put_entity(a.addr, a.eid, MY_EID, state="Dead")
    ok &= check("動畫 Dead 的屍體留著「目標是我」也不算",
                entity.attackers(sc, ents, MY_EID), [])
    sc.put_entity(a.addr, a.eid, MY_EID, state="Wait", flag=7)
    ok &= check("死亡旗標 +0x3D6==7（柱子那種）也不算",
                entity.attackers(sc, ents, MY_EID), [])
    sc.put_entity(a.addr, a.eid, MY_EID, state="Att")
    ok &= check("活的、正在揮我 → 算",
                [e.eid for e in entity.attackers(sc, ents, MY_EID)], [a.eid])

    print("③ 人數與射程")
    from app.tabs import farm_tab                 # noqa: PLC0415
    sc2 = FakeScanner()
    foes = []
    for i in range(3):
        e = FakeEnt(0x50000000 + i * 0x1000, 0xBB00 + i)
        sc2.put_entity(e.addr, e.eid, MY_EID, state="Att")
        foes.append(e)
    ok &= check("三隻都在打我", len(entity.attackers(sc2, foes, MY_EID)), 3)
    # 其中一隻死了 → 只剩兩隻（門檻 3 就不該觸發）
    sc2.put_entity(foes[0].addr, foes[0].eid, MY_EID, state="Dead")
    ok &= check("其中一隻變屍體 → 只剩兩隻",
                len(entity.attackers(sc2, foes, MY_EID)), 2)
    ok &= check("射程查不到 → inf（不擋）",
                farm_tab.reach_of(0) == float("inf"), True)

    print("④ 不搶、不走路（靜態檢查 GangWorker.step 的原始碼）")
    import inspect                               # noqa: PLC0415
    src = inspect.getsource(farm_tab.GangWorker)
    for bad, why in (("set_target", "寫目標欄＝跟自動掛機搶"),
                     ("_send_scan", "送技能鍵＝對遊戲選定的目標出手"),
                     ("walk_", "走路"),
                     ("navigate", "走路"),
                     ("path_to", "走路")):
        ok &= check(f"沒有 {bad}（{why}）", bad in src, False)
    ok &= check("出手只走官方施放函式", "attack.cast_skill" in src, True)

    print("⑤ 挑目標：正在打我的優先（2026-09-13 使用者定）")
    # 場景：B 在 4 格但沒理我、C 在 8 格正在咬我。使用者的症狀＝打死目標後
    # 挑到遠的那隻，因為挑目標整條路**只看最近**、完全不看誰在打我。
    # ⚠ C 的動畫故意是 'Wait'（兩次揮擊之間的空檔）——舊的弱訊號
    #   （交戰槽＋動畫 Att）在這一拍**抓不到**，這正是實際會失手的那一拍。
    sc3 = FakeScanner()
    near = FakeMon(0x60000000, 0xCC01, name="近的")
    far = FakeMon(0x60001000, 0xCC02, name="遠的")
    sc3.put_entity(near.addr, near.eid, 0, pos=(14.0, 10.0))
    sc3.put_entity(far.addr, far.eid, 0, pos=(18.0, 10.0))
    grid = FakeGrid([(x, 10) for x in range(5, 25)])
    tab = make_farm_tab(sc3, [near, far], grid=grid)
    ok &= check("沒人打我 → 照舊挑最近的",
                tab._pick_next() and tab._cur.name, "近的")
    sc3.put_entity(far.addr, far.eid, MY_EID, pos=(18.0, 10.0))
    tab = make_farm_tab(sc3, [near, far], grid=grid)
    ok &= check("★ 遠的那隻在咬我 → 挑牠（不再挑近的）",
                tab._pick_next() and tab._cur.name, "遠的")
    ok &= check("　攻擊執行緒也鎖到牠身上",
                (tab._atk.picked is tab._cur, tab._keys.eid, tab._keys.on),
                (True, far.eid, True))
    # ★ 使用者實際遇到的：牠先前被記成「走不到」冰起來 → 挑目標永遠跳過牠
    tab = make_farm_tab(sc3, [near, far], grid=grid)
    tab._killed[far.eid] = 1e18                 # 冷卻到天荒地老
    tab._unreach_n[far.eid] = 5
    ok &= check("★★ 冷卻中但正在咬我 → 無條件解冷卻並挑牠",
                (tab._pick_next() and tab._cur.name,
                 far.eid in tab._killed), ("遠的", False))
    # ★ 地形圖說走不到（牠站的格被判成牆／隔著薄牆）也不准放掉：
    #   「打我就代表那怪物是活的，正在打我就該打他」
    tab = make_farm_tab(sc3, [near, far],
                        grid=FakeGrid([(x, 10) for x in range(5, 16)]))
    ok &= check("★★ 地形圖說走不到、路徑也算不出來 → 還是挑牠",
                tab._pick_next() and tab._cur.name, "遠的")
    # ⛔ 屍體身上那個欄位會留著最後打的人（實測 352 拍次）→ 不准當「在打我」
    sc3.put_entity(far.addr, far.eid, MY_EID, state="Dead", pos=(18.0, 10.0))
    tab = make_farm_tab(sc3, [near, far], grid=grid)
    ok &= check("⛔ 屍體留著「目標是我」不算 → 挑回近的",
                tab._pick_next() and tab._cur.name, "近的")

    print("\n" + ("全部通過" if ok else "⛔ 有項目沒過"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
