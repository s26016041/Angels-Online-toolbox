r"""人多的時候「講不到話」到底卡在哪一關 —— **純讀，一個封包都不送**。

    py tools\npcbusy_probe.py                # 每一台都看
    py tools\npcbusy_probe.py --who 白狐      # 只看那一台
    py tools\npcbusy_probe.py --watch 60     # 盯 60 秒，每 2 秒一行（卡住時用這個）

★ 用法：**補給卡住的當下**跑它。工具箱開著也能跑（純讀，不裝跳板）。

把 `supply._engage_npc` 會用到的每一個判斷單獨印出來，就知道是哪一關過不了：

  ① 找得到那隻 NPC 嗎（`find_npc`）—— 找不到就會一直「用地形圖靠近再試」
  ② 離他幾格（`_npc_gap`）
  ③ 我在不在**講話方框**內（TryAct kind 2：|Δx|≤5、|Δy|≤3）—— 在框內
     伺服器就吃選項，站定重點就夠
  ④ 「離 NPC 最近可到的格」有幾個、前三個是誰（`_near_spots`）——
     ⚠ 這個算法**不看別人站不站在那**（使用者定：人牆偵測刪掉），
     所以這裡會順便印「那格上現在有沒有人」給你對
  ⑤ 附近有幾個玩家、場景實體表多大、`find_npc` 掃一遍要多久
     （人多＝表變大＝每次重找都要掃一遍）

⚠ 這支**不點 NPC、不走路、不送任何封包**，跑它不會影響正在進行的補給。
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, preload                      # noqa: E402
from app.core.memory import MemoryScanner                   # noqa: E402
from app.game import bag, locate, move, scene, supply, terrain  # noqa: E402

KINDS = (("buy", "補給商"), ("repair", "維修商"), ("bank", "銀行"))
NEAR_R = 8.0          # 「附近的玩家」算幾格內


def _u32(sc, a):
    raw = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else 0


def _tile(sc, ent):
    raw = sc._read_bytes(ent + 0xC6, 8)                 # +0xC6 X、+0xCA Y（世界＝格×32）
    if not raw:
        return None
    x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
    return x / 32.0, y / 32.0


def _players(sc, me):
    """場景表裡的**其他玩家**（NPC 編號欄是大指標值的那些）→ [(tile_x, tile_y)]。"""
    out = []
    for e in supply._scene_entities(sc):
        if e == me:
            continue
        num = _u32(sc, e + supply.OFF_NPC_NUM)
        if num and num < 0x10000:                       # 小編號＝NPC／怪，不是玩家
            continue
        p = _tile(sc, e)
        if p:
            out.append(p)
    return out


def _one(sc, who: str, verbose: bool) -> None:
    sid = scene.current_id(sc)
    table = supply.NPC_TABLE.get(sid) or {}
    ents = supply._scene_entities(sc)
    me = bag.player_entity(sc)
    pf, here = supply._player_tile(sc)
    g, why = terrain.load(sc)
    head = (f"{who}　{scene.scene_name(sid)}（{sid}）"
            f"　我在 ({here[0]:.1f},{here[1]:.1f})" if here else f"{who}　座標讀不到")
    print(head + f"　場景實體 {len(ents)} 個" + ("" if g is not None else f"　⚠ 地形圖：{why}"))
    if not table:
        print("   （這張圖不是補給城，沒有商人表）")
        return
    plist = _players(sc, me) if me else []
    for key, label in KINDS:
        npc = table.get(key)
        if not npc:
            continue
        npc_id, tx, ty = npc
        t0 = time.time()
        found = supply.find_npc(sc, npc_id)
        dt = (time.time() - t0) * 1000
        if not found:
            print(f"   {label}({npc_id})　⛔ **找不到這隻**（表裡寫 {tx},{ty}）"
                  f"　掃一遍 {dt:.0f}ms")
            continue
        ent, _sel = found
        gap = supply._npc_gap(sc, npc_id)
        box = supply._box_status(sc, npc_id, ent)
        npos = _tile(sc, ent)
        near = [p for p in plist
                if npos and math.hypot(p[0] - npos[0], p[1] - npos[1]) <= NEAR_R]
        line = (f"   {label}({npc_id})　在 ({npos[0]:.1f},{npos[1]:.1f})"
                if npos else f"   {label}({npc_id})")
        line += f"　離我 {gap if gap is None else round(gap, 1)} 格"
        line += f"　旁邊 {len(near)} 人"
        line += f"　掃一遍 {dt:.0f}ms"
        print(line)
        if box is None:
            print("      ⚠ 講話方框算不出來（地形圖／座標讀不到）")
            continue
        free = box["free"]
        mark = "✅ 在框內（站定重點就該開）" if box["in_box"] else "✘ **不在講話方框內**"
        print(f"      {mark}　可站的格 {len(free)} 個")
        for c in free[:3]:
            on = [p for p in plist if int(p[0]) == c[0] and int(p[1]) == c[1]]
            d = math.hypot(c[0] + .5 - here[0], c[1] + .5 - here[1]) if here else -1
            print(f"        候選格 {c}　離我 {d:.1f} 格"
                  + ("　⚠ **有人站在上面**" if on else ""))
        if not free:
            print("        ⛔ 一個可站的格都沒有 —— 這就是「走不過去」的來源")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--who", default="")
    ap.add_argument("--watch", type=float, default=0.0, help="盯幾秒（每 2 秒一輪）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    wins = preload.windows()
    if not wins:
        print("沒有開著的分身")
        return 1
    scs = []
    first = True
    for w in wins:
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            if first:
                locate.warm(sc)
                first = False
            nm = preload.name_of(w.pid, sc, charname.account_from_title(w.title))
        except Exception as exc:                       # noqa: BLE001
            print(f"pid {w.pid}：開不起來（{exc}）")
            continue
        if a.who and a.who not in (nm or "") and a.who not in w.title:
            sc.close()
            continue
        scs.append((nm or str(w.pid), sc))
    if not scs:
        print(f"找不到「{a.who}」")
        return 1
    t0 = time.time()
    while True:
        print(time.strftime("── %H:%M:%S ──"))
        for nm, sc in scs:
            try:
                _one(sc, nm, a.verbose)
            except Exception as exc:                   # noqa: BLE001
                print(f"{nm}：讀的時候出錯（{exc}）")
        if time.time() - t0 >= a.watch:
            break
        time.sleep(2.0)
    for _nm, sc in scs:
        sc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
