r"""驗證「官方點 NPC」那一支（TryAct）能不能當拍開對話。

    py tools\npcclick_probe.py                     # 純讀：列附近 NPC ＋ 驗位元組
    py tools\npcclick_probe.py --pid 12345          # 指定分身（不指定就每台都列）
    py tools\npcclick_probe.py --pid 12345 --go 123 # ★ 對編號 123 那隻送一發 TryAct

為什麼要有它（2026-09-03，使用者「貼在 NPC 臉上都說不到話，滑鼠點卻很正常」）：

    官方滑鼠點 NPC 是兩步 ——
      ① 先直接叫 TryAct(eid, kind=3)：當拍判距離(0x508DF6)＋視線(0x5B87E4)，
         過了就直接動手（0x5065E7 → 事件 "R011" → 送 0x07 面向 + 0x05 點選）。
         回 0 ＝ 這次處理完了；回 1 ＝ 還沒到位，它已經用官方尋路幫你走一步。
      ② 只有 ① 回 1 才去設自動走路狀態機 0x5495B5(mode=1, eid)。
    我們的 supply._click_npc **只做了 ②** —— 而 ② 會先把倒數 [pf+0x41B0] 設成
    20 拍，歸零才第一次嘗試，所以貼臉也要空等；[pf+0x41A0] 沒歸零時更是整個
    寫進延後槽 [pf+0x41B8] 不執行。詳見 memory `npc-click-official-path`。

⚠ 這支是**開發用探針**，不是產品路徑。--go 會真的送一發互動（＝跟滑鼠點一下
  同一件事），不會買東西、不會送選項。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, injector                 # noqa: E402
from app.core import window as win                      # noqa: E402
from app.core.memory import MemoryScanner               # noqa: E402
from app.game import bag, entity, locate, move, scene, supply  # noqa: E402

# ★ 靜態反組譯出來的位址（2026-09-03 這版 angel.dat）。
#   還沒登記 AOB —— 所以每次用之前都**當場比對開頭位元組**，不對就拒絕呼叫。
TRY_ACT_FN = 0x00506784
TRY_ACT_HEAD = bytes.fromhex(
    "558BEC83EC28538B5D0C8BC3568BF1576A01598DBE5C03000083E803741E")
KIND_TALK = 3               # 出處：狀態機 mode 1 的 tick 就是叫 TryAct(eid, 3)

REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "reports", "npcclick_probe.txt")


def _u32(sc, addr):
    raw = sc._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else 0


def _tile(sc, ent):
    raw = sc._read_bytes(ent + 0xC6, 8)
    if not raw:
        return None
    x, _, y, _ = struct.unpack("<hhhh", bytes(raw))
    return (x / 32.0, y / 32.0)


def _nearby_npcs(sc, me, limit_tiles=25.0):
    """附近的 NPC（+0x1D8 是小編號的那種）：[(距離, 編號, 名字, 實體, eid)]"""
    mt = _tile(sc, me)
    out = []
    if mt is None:
        return out
    for e in supply._scene_entities(sc):
        if e == me:
            continue
        num = _u32(sc, e + supply.OFF_NPC_NUM)
        if not 0 < num < 0x10000:            # 玩家那欄是大指標值，濾掉
            continue
        t = _tile(sc, e)
        if t is None:
            continue
        d = ((t[0] - mt[0]) ** 2 + (t[1] - mt[1]) ** 2) ** 0.5
        if d > limit_tiles:
            continue
        out.append((d, num, supply._npc_name(sc, e), e, _u32(sc, e + 0xBC)))
    out.sort()
    return out


def _head_ok(sc) -> bool:
    raw = sc._read_bytes(TRY_ACT_FN, len(TRY_ACT_HEAD))
    return bool(raw) and bytes(raw) == TRY_ACT_HEAD


def _clients():
    return [w for w in win.enumerate_windows(title_contains="Angels Online")
            if "_MIDAGEONL_" in w.class_name]


def survey(pid_filter, lines):
    """純讀：每台的角色名／場景／附近 NPC／TryAct 位元組對不對。"""
    first = True
    found = {}
    for w in _clients():
        if pid_filter and w.pid != pid_filter:
            continue
        sc = MemoryScanner()
        sc.open(w.pid)
        if first:
            locate.warm(sc)
            first = False
        acct = charname.account_from_title(w.title)
        try:
            who = charname.read_character_name(sc, acct) or "?"
        except Exception:                                   # noqa: BLE001
            who = "?"
        sid = scene.current_id(sc)
        # ⚠ 場景實體表裡「玩家自己」那一格就是 bag.player_entity()（＝pf），
        #   word 座標在 +0xC6；pf+8 那個位址讀 +0xC6 全是 0（實測 2026-09-03）。
        me_ent = bag.player_entity(sc) or 0
        head = (f"=== pid {w.pid}  {acct}  角色={who}  "
                f"{scene.scene_name(sid)}({sid})  TryAct位元組="
                f"{'對' if _head_ok(sc) else '✘不對'}")
        lines.append(head)
        print(head)
        if not me_ent:
            lines.append("    讀不到玩家物件")
            continue
        lines.append(f"    我在 {_tile(sc, me_ent)}")
        for d, num, name, ent, eid in _nearby_npcs(sc, me_ent):
            lines.append(f"    {d:6.1f} 格  編號 {num:<6} {name:<16} "
                         f"實體 {ent:#x}  eid {eid}")
        found[w.pid] = (who, sc)
    return found


def go(pid: int, npc_id: int, lines) -> int:
    """對編號 npc_id 那隻送一發 TryAct(eid, 3)，看對話框開不開。"""
    w = next((x for x in _clients() if x.pid == pid), None)
    if w is None:
        print(f"找不到 pid {pid}")
        return 2
    sc = MemoryScanner()
    sc.open(pid)
    locate.warm(sc)
    if not _head_ok(sc):
        print("✘ TryAct 開頭位元組跟磁碟檔對不上（改版？）—— 拒絕呼叫")
        return 3
    found = supply.find_npc(sc, npc_id)
    if not found:
        print(f"✘ 附近沒有編號 {npc_id} 的 NPC")
        return 4
    ent, _sel = found
    # ★ 送出前當場重驗這個實體還是它（CLAUDE.md 鐵則）
    if _u32(sc, ent + supply.OFF_NPC_NUM) != npc_id:
        print("✘ 那個實體已經不是那隻了")
        return 5
    eid = _u32(sc, ent + 0xBC)
    pf = move.pathfinder_this(sc)
    if not (eid and pf):
        print("✘ 讀不到 eid／玩家物件")
        return 6
    gap = supply._npc_gap(sc, npc_id)
    lock = _u32(sc, pf + 0x41A0)
    base = supply._dialog_token(sc)
    owner = object()          # ⚠ release 要用**同一個** owner，不然跳板不會還原
    mv = move.acquire(pid, injector.process_path(pid), owner)
    try:
        t0 = time.time()
        ret = mv.call_sync(TRY_ACT_FN, eid, KIND_TALK, ecx=pf, timeout=1.0)
        t_call = time.time() - t0
        opened_at = None
        walked = False
        while time.time() - t0 < 4.0:
            tok = supply._dialog_token(sc)
            if opened_at is None and tok not in (None, 0) and tok != base:
                opened_at = time.time() - t0
                break
            if entity.is_walking(sc, pf + 8):
                walked = True
            time.sleep(0.05)
        gap2 = supply._npc_gap(sc, npc_id)
    finally:
        move.release(pid, owner)
    msg = [f"pid {pid}  NPC {npc_id}  距離 {gap} → {gap2}  [pf+0x41A0]鎖={lock}",
           f"  TryAct 回傳 = {ret}（0＝已動手、1＝還沒到位在走過去、None＝排不進去）",
           f"  呼叫本身耗時 {t_call * 1000:.0f} ms",
           f"  對話框 baseline={base} → "
           + (f"★ {opened_at:.2f} 秒後開了" if opened_at is not None
              else "4 秒內沒變（沒開）"),
           f"  這 4 秒有沒有在走路：{'有' if walked else '沒有'}"]
    for m in msg:
        print(m)
    lines.extend(msg)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--go", type=int, default=0, help="NPC 編號")
    a = ap.parse_args()
    lines: list[str] = [time.strftime("%Y-%m-%d %H:%M:%S")]
    rc = 0
    if a.go:
        if not a.pid:
            print("--go 要配 --pid")
            return 1
        rc = go(a.pid, a.go, lines)
    else:
        survey(a.pid, lines)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
