"""逐位元驗證「調色盤換色」（base＋mask 兩組）：把調色盤餵給遊戲自己那支換色函式，跟我們算的比。

    py tools\\icon_mask_probe.py [--pid N] [--n 300]     （要開著遊戲；借跳板呼叫純函式，不動遊戲狀態）

遊戲畫調色盤圖示時走 `0x6825b0(dst, src, split, state)`（反組譯見 app/game/iconbias.py 檔頭）：
    state ＝ 0x20 bytes：slot0(base) lo/hi/bias/flag、slot1(mask) lo/hi/bias/flag
    調色盤 [0, split) 用 slot1、[split, 256) 用 slot0；bias 0 的那組照抄。
這裡**自己組 state**（不碰遊戲的全域 0xa02e18），輸入輸出都放跳板的 scratch 區，
呼叫的是同一支函式 → 結果逐位元一樣＝我們的 `iconbias.remap_palette` 跟遊戲一樣。
⚠ 呼叫前比對函式開頭位元組（沒登 AOB 的開發探針，改版就拒跑）。
結束碼：0 全對、1 有差、2 找不到遊戲／跳板／位元組不對。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
except Exception:                                          # noqa: BLE001
    pass

import numpy as np                                         # noqa: E402

from app.core import injector, preload                     # noqa: E402
from app.core.memory import MemoryScanner                  # noqa: E402
from app.game import iconbias, itemicon, locate, move      # noqa: E402

REMAP_PAL_FN = 0x006825B0          # 2026-09-06 這版；開頭位元組不對就拒跑
REMAP_PAL_HEAD = bytes.fromhex("558BEC8A451083EC0C53568B7514570FB6F8837E1800")
OFF_DST, OFF_SRC, OFF_STATE = 0x000, 0x200, 0x400        # 相對 mover.scratch()


def state_bytes(base, mask) -> bytes:
    """照 SetBias 的版面組 0x20 bytes（bias 0 的 slot 遊戲不看 lo/hi，填 0）。"""
    out = b""
    for bias, color, rng in (base, mask):
        if bias:
            lo, hi, flag = iconbias.window(color, rng)
        else:
            lo = hi = flag = 0
        out += struct.pack("<iiIi", lo, hi, int(bias) & 0xFFFFFFFF, flag)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--n", type=int, default=300, help="抽多少個有調色盤且帶參數的編號")
    a = ap.parse_args()
    wins = [w for w in preload.windows() if not a.pid or w.pid == a.pid]
    if not wins:
        print("找不到遊戲視窗")
        return 2
    pid = wins[0].pid
    sc = MemoryScanner()
    sc.open(pid)
    locate.warm(sc)
    head = sc._read_bytes(REMAP_PAL_FN, len(REMAP_PAL_HEAD))
    if not head or bytes(head) != REMAP_PAL_HEAD:
        print(f"✘ {REMAP_PAL_FN:#x} 開頭位元組跟反組譯時不一樣（改版？）—— 拒絕呼叫")
        return 2
    _z, idx = itemicon._open()
    picks = []
    for iid, e in sorted(idx.items()):
        if not (e[1] or e[4]):
            continue
        shp = itemicon._shp(e[0])
        if shp is None or shp.pal is None:
            continue
        picks.append((iid, e, shp))
        if len(picks) >= a.n:
            break
    if not picks:
        print("圖包裡沒有帶參數的調色盤圖示")
        return 2
    owner = object()
    mv = move.acquire(pid, injector.process_path(pid), owner)
    bad = 0
    n_mask = 0
    try:
        base_addr = mv.scratch()
        for iid, e, shp in picks:
            base, mask = (e[1], e[2], e[3]), (e[4], e[5], e[6])
            n_mask += bool(e[4])
            mv.write(base_addr + OFF_SRC, shp.pal.astype("<u2").tobytes())
            mv.write(base_addr + OFF_DST, b"\0" * 0x200)
            mv.write(base_addr + OFF_STATE, state_bytes(base, mask))
            ret = mv.call_sync(REMAP_PAL_FN, base_addr + OFF_DST, base_addr + OFF_SRC,
                               shp.split, base_addr + OFF_STATE, timeout=1.0)
            if ret is None:
                print(f"✘ {iid}：呼叫排不進去")
                bad += 1
                continue
            got = np.frombuffer(bytes(sc._read_bytes(base_addr + OFF_DST, 0x200)), dtype="<u2")
            mine = iconbias.remap_palette(shp.pal, shp.split, base, mask)
            diff = np.nonzero(got != mine)[0]
            if len(diff):
                bad += 1
                if bad <= 8:
                    print(f"✘ {iid} {e[0]} split={shp.split} base={base} mask={mask}：{len(diff)} 格不同，"
                          + ", ".join(f"[{i}] 遊戲 {int(got[i]):#06x} 我們 {int(mine[i]):#06x}"
                                      for i in diff[:4]))
    finally:
        move.release(pid, owner)
    print(f"pid {pid}　比了 {len(picks)} 個編號（{n_mask} 個有 mask）：{'全部逐位元一樣 ✔' if not bad else f'{bad} 個不一樣 ✘'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
