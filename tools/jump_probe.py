r"""趴趴GO（0x151）到底被誰擋住 —— 純測伺服器驗不驗。

    py tools\jump_probe.py                     # 純讀：每台在哪、這張圖有什麼落點
    py tools\jump_probe.py --list 聖光          # 查編號（名字含這兩字的）
    py tools\jump_probe.py --who 白狐 --go 4    # ★ 送一發，盯 25 秒看到底到不到

為什麼值得測（2026-09-18 反組譯 angel.dat 0.0.3.0 得到的硬結論）
---------------------------------------------------------------
送 0x151 的那支（這一版在 `0x5E5839`，全檔**只有這一處** push 0x151）：

    視窗 = 0x5025F3([0x8CDB38]+4, "…")        ; 趴趴GO 視窗
    控制項 = 0x6461A0(視窗, 0x6EDC)            ; 那個清單
    列 = [ebp+8] - 0x6EC8 + 0x646493(控制項)   ; 你點的是第幾列
    if 列 < 0: return
    項目 = [[控制項+0x150] + 列*4]
    if !項目 or ![項目+0x8C]: return
    跳地圖編號 = [[項目+0x8C]]                  ; ← 清單自己綁上去的值
    0x50D1C9(0x151, 6)                        ; 建包
    [資料+2] = 跳地圖編號
    0x734FD0([0x9F32F8], 封包)                 ; 送出

⛔ **一行檢查都沒有**：沒驗道具、沒驗等級、沒驗你在哪張圖、沒驗冷卻、
   沒驗戰鬥中。客戶端在這裡只是個「把清單那格的數字塞進封包」的傳聲筒。
→ 所以「能不能亂飛」100% 取決於伺服器收到 0x151 之後驗多少，
   而那只能實測。這支就是拿來測的。

⚠ 封包內文只有 6 bytes ＝ 代號(u16) + **跳地圖編號(u32)**，
  沒有場景編號、沒有座標欄位 —— 就算伺服器完全不驗，能到的也只有
  官方表上那 120 個落點的那一格，不存在「飛到任意座標」。

怎麼讀結果
----------
  ✅ 換圖了        → 伺服器收了這一發（那個條件它沒驗）
  ✘ 25 秒沒反應   → 伺服器擋掉了（有驗）
  ⚠ 被踢／斷線    → 伺服器擋掉而且不高興（就別再試了）

⚠ 這支會**真的送封包**。一次只送一發、送完只是盯著看，不重試。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, injector, preload         # noqa: E402
from app.core.memory import MemoryScanner                # noqa: E402
from app.game import entity, jumpmap, locate, move, scene  # noqa: E402

WATCH = 25.0          # 送出後盯多久
POLL = 0.2


def _pos(sc) -> tuple[float, float] | None:
    pf = move.pathfinder_this(sc)
    return entity.player_pos(sc, pf + 8) if pf else None


def _hooked(sc, pid: int) -> bool | None:
    """這台現在有沒有被**別的行程**（工具箱）裝著跳板？讀不出來回 None。

    ⚠⚠ 跨行程的跳板會互拆（memory `mover-per-pid-conflict`）——
      工具箱正在驅動的那台**不可以**拿來跑這支，會把它的跳板拆掉。
      判法：PeekMessageA 的 IAT 指到模組外 ＝ 已經被 hook。
    """
    try:
        iat = injector._resolve_iat(injector.process_path(pid), "PeekMessageA")
        raw = sc._read_bytes(iat, 4)
        if not raw:
            return None
        cur = struct.unpack("<I", bytes(raw))[0]
        return cur < 0x70000000
    except Exception:                          # noqa: BLE001
        return None


def _name_of(w, sc) -> str:
    """角色名（讀不到就退回帳號）—— `--who` 兩種都比對得到。"""
    acct = charname.account_from_title(w.title)
    try:
        return preload.name_of(w.pid, sc, acct) or acct
    except Exception:                          # noqa: BLE001
        return acct


def _where(sc) -> tuple[int | None, str, tuple[float, float] | None]:
    sid = scene.current_id(sc)
    return sid, scene.scene_name(sid), _pos(sc)


def _survey() -> int:
    """純讀：每台在哪張圖、腳下座標、這張圖有哪些落點。"""
    wins = preload.windows()
    if not wins:
        print("沒有開著的分身")
        return 1
    first = True
    for w in wins:
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            if first:
                locate.warm(sc)
                first = False
            sid, name, pos = _where(sc)
            here = f"({pos[0]:.1f},{pos[1]:.1f})" if pos else "座標讀不到"
            who = _name_of(w, sc)
            hk = _hooked(sc, w.pid)
            tag = "★工具箱驅動中" if hk else ("空閒" if hk is False else "?")
            print(f"pid {w.pid:<6} {who:<10} {tag:<9} {name}（{sid}）{here}")
            for e in (jumpmap.by_scene(sid) if sid is not None else []):
                print(f"        └ 這張圖的落點：編號 {e.jump_id} {e} ({e.x},{e.y})")
        except Exception as exc:          # noqa: BLE001
            print(f"pid {w.pid}：讀不到（{exc}）")
        finally:
            sc.close()
    return 0


def _list(key: str) -> int:
    n = 0
    for jid in range(1, 1000):
        e = jumpmap.get(jid)
        if e is None or (key and key not in e.name):
            continue
        print(f"  {e.jump_id:>4}  {scene.scene_name(e.scene_id)}（{e.scene_id}）"
              f" ({e.x},{e.y})  {e.name}")
        n += 1
    print(f"（{n} 筆）")
    return 0


def _go(who: str, pid: int, jump_id: int) -> int:
    e = jumpmap.get(jump_id)
    if e is None:
        print(f"⛔ 表裡沒有編號 {jump_id}（`--list` 查得到的才送）")
        return 2
    wins = preload.windows()
    if pid:
        wins = [w for w in wins if w.pid == pid]
    elif who:
        keep = []
        for w in wins:
            sc0 = MemoryScanner()
            try:
                sc0.open(w.pid)
                if who in w.title or who in _name_of(w, sc0):
                    keep.append(w)
            except Exception:                  # noqa: BLE001
                pass
            finally:
                sc0.close()
        wins = keep
    if len(wins) != 1:
        print(f"⛔ --who/--pid 要**剛好**指到一台（現在 {len(wins)} 台）")
        return 2
    w = wins[0]

    sc = MemoryScanner()
    sc.open(w.pid)
    locate.warm(sc)
    if _hooked(sc, w.pid) is not False:
        print("⛔ 這台正被工具箱驅動（或判不出來）—— 跨行程跳板會互拆，不碰。")
        sc.close()
        return 3
    sid0, name0, pos0 = _where(sc)
    if sid0 is None:
        print("⛔ 讀不到目前場景（正在載圖？）—— 不送")
        sc.close()
        return 3
    same = sid0 == e.scene_id
    print(f"現在：{name0}（{sid0}）"
          + (f" ({pos0[0]:.1f},{pos0[1]:.1f})" if pos0 else " 座標讀不到"))
    print(f"要送：編號 {jump_id} → {e}（場景 {e.scene_id} 的 {e.x},{e.y}）"
          + ("　※ 同一張圖：看座標變不變" if same else ""))

    owner = object()
    mv = None
    try:
        mv = move.acquire(w.pid, injector.process_path(w.pid), owner)
        t0 = time.time()
        ok, msg = jumpmap.teleport(mv, sc, jump_id)
        print(f"送出：{msg}")
        if not ok:
            return 4
        landed = None
        while time.time() - t0 < WATCH:
            time.sleep(POLL)
            sid, name, pos = _where(sc)
            if sid is None:
                continue                       # 載圖中，場景會暫時讀不到
            moved = (same and pos and pos0
                     and abs(pos[0] - e.x) < 3 and abs(pos[1] - e.y) < 3)
            if (not same and sid != sid0) or moved:
                landed = (time.time() - t0, sid, name, pos)
                break
        if landed:
            dt, sid, name, pos = landed
            at = f"({pos[0]:.1f},{pos[1]:.1f})" if pos else "座標讀不到"
            print(f"✅ {dt:.1f} 秒後到了：{name}（{sid}）{at}"
                  f"　表裡寫 ({e.x},{e.y})")
            print("   → 伺服器**收了**這一發：這個條件它沒驗。")
        else:
            sid, name, pos = _where(sc)
            print(f"✘ {WATCH:.0f} 秒都沒動（還在 {name}（{sid}））")
            print("   → 伺服器**擋掉了**：這個條件它有驗（或封包被丟掉）。")
    except Exception as exc:                   # noqa: BLE001
        print(f"⛔ 出事：{exc}")
        return 5
    finally:
        if mv is not None:
            move.release(w.pid, owner)
        sc.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--who", default="", help="視窗標題含這個字串的那台")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--go", type=int, default=0, help="跳地圖編號（會真的送）")
    ap.add_argument("--list", dest="key", nargs="?", const="", default=None,
                    help="列出傳送表（可給關鍵字）")
    a = ap.parse_args()
    if a.go:
        return _go(a.who, a.pid, a.go)
    if a.key is not None:
        return _list(a.key)
    return _survey()


if __name__ == "__main__":
    raise SystemExit(main())
