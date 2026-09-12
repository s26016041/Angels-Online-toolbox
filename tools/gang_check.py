# -*- coding: utf-8 -*-
"""被圍毆就反擊（GangWorker）的離線回歸 —— 不需要開遊戲。

    py tools\\gang_check.py

驗的是四件**會安靜做錯事**的地方：
  ① 「誰在打我」只認 `+0x34C == 我的實體編號`，而且會重驗身分
     （位址被回收給別隻時不准算進來）。
  ② ⚠⚠ **屍體不准算**（2026-09-12 使用者實機回報的誤判真因：怪死掉之後
     那個欄位還留著最後打的人，實測 45 秒裡 Dead 指著我的有 352 拍次）。
  ③ 湊不到人數不出手；每一招各自比自己的射程。
  ④ ⛔ 全程不寫目標欄、不送鍵、不下移動指令（＝不跟自動掛機搶、不走路）。
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
                   vtable: int | None = None) -> None:
        span = max(entity.LIVE_SPAN, entity.attack_target_off() + 4)
        blob = bytearray(span)
        struct.pack_into("<I", blob, 0,
                         entity.VT_ENTITY if vtable is None else vtable)
        blob[entity.OFF_STATE:entity.OFF_STATE + len(state)] = state.encode()
        struct.pack_into("<I", blob, entity.OFF_ID, eid)
        struct.pack_into("<I", blob, entity.attack_target_off(), target)
        blob[entity.OFF_DEAD_FLAG] = flag
        self.mem[addr] = bytes(blob)

    def _read_bytes(self, addr: int, size: int):
        got = self.mem.get(addr)
        if got is None or len(got) < size:
            return None
        return got[:size]


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

    print("\n" + ("全部通過" if ok else "⛔ 有項目沒過"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
