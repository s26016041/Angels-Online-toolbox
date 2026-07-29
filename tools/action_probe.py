"""動作差異偵測器：找出「攻擊 / 移動 / 放技能」這些動作對應的遊戲函式位址。

自訂選怪外掛的第一步 —— 你得先知道「玩家點怪攻擊時，遊戲內部呼叫的是哪個函式」。
找到它，就能反過來主動呼叫它來自動打怪，也能順著它挖出「當前目標」和整張怪物清單。

原理
----
send 是全遊戲唯一的封包出口（wrapper @0x71174c）。每次 send，injector 的 stub 會記下
當時的呼叫堆疊；其中落在 angel.dat 程式碼範圍(0x401000~0x7D0000)的返回位址 = 呼叫鏈，
最上層那幾個就是「攻擊 / 移動 / 放技能」等高階邏輯的呼叫點。

不同動作送不同封包 → 呼叫鏈不同。於是用「差異法」：
  1. 基線期：角色完全不動幾秒，記下這段期間所有呼叫鏈位址的出現率（心跳、位置回報等雜訊）。
  2. 動作期：只反覆做「一件事」（狂點同一隻怪攻擊），記下呼叫鏈位址出現率。
  3. 相減：動作期高頻、但基線期（與其他已測動作）低頻的位址 = 這個動作獨有的函式呼叫點。

跨動作也會累積比對：測完「攻擊」再測「移動」，兩者互為對方的對照組，交叉排除後
更精準。位址無 ASLR、固定，可直接拿去 Ghidra/IDA 開磁碟上的 angel.dat 對照
（位址減 0x400000 = 檔案內 RVA）。純讀寫記憶體、不附加除錯器 → 不觸發反作弊。

用法（在專案根目錄、遊戲已登入進場後執行）：
    py tools/action_probe.py --action attack       # 測「攻擊」
    py tools/action_probe.py --action move          # 測「移動」
    py tools/action_probe.py --action skill1         # 測某個技能
    py tools/action_probe.py --summary               # 只看目前累積結果的交叉比對
    py tools/action_probe.py --action attack --reset  # 清掉舊資料重來

每測一種動作，結果會累積存到 scratchpad/action_probe.json，跑越多種、交叉排除越乾淨。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 主控台是 cp950，中文輸出要先把 stdout 轉成 utf-8（見記憶：env-run-commands）。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.core import injector, window as win

MODULE_BASE = 0x400000  # angel.dat 固定基底（無 ASLR）
STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scratchpad",
    "action_probe.json",
)


# ---------------------------------------------------------------------------
# 找遊戲行程
# ---------------------------------------------------------------------------
def find_angel_pid() -> tuple[int, str] | None:
    """找出 angel.dat 的行程。回傳 (pid, 視窗標題)；找不到回傳 None。"""
    seen: set[int] = set()
    for w in win.enumerate_windows():
        if w.pid in seen:
            continue
        seen.add(w.pid)
        path = injector.process_path(w.pid)
        if path and os.path.basename(path).lower() == "angel.dat":
            return w.pid, w.title
    return None


# ---------------------------------------------------------------------------
# 背景擷取一段時間內的所有呼叫鏈位址
# ---------------------------------------------------------------------------
class _Collector:
    """在背景執行緒持續讀新封包，把呼叫鏈位址累加進 Counter。"""

    def __init__(self, cap: injector.SendCapture) -> None:
        self._cap = cap
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.addr_hits: Counter[int] = Counter()  # 位址 -> 出現在幾個封包
        self.packets = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            for pkt in self._cap.read_new():
                self.packets += 1
                # 一個封包內同一位址只算一次，避免深堆疊重複位址灌水。
                for a in set(pkt.call_chain):
                    self.addr_hits[a] += 1
            time.sleep(0.03)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def rates(self) -> dict[int, float]:
        """位址 -> 出現率（在多少比例的封包裡出現過）。"""
        if self.packets == 0:
            return {}
        return {a: c / self.packets for a, c in self.addr_hits.items()}


def _capture_phase(cap: injector.SendCapture, prompt: str) -> _Collector:
    """印出提示，等使用者按 Enter 開始，再按 Enter 結束；回傳這段期間的收集結果。"""
    input(f"\n>>> {prompt}\n    準備好後按 Enter 開始擷取…")
    cap.read_new()  # 清掉開始前的殘留
    col = _Collector(cap)
    col.start()
    input("    擷取中… 做動作，做完按 Enter 結束。")
    col.stop()
    print(f"    這段共擷取到 {col.packets} 個送出封包。")
    return col


# ---------------------------------------------------------------------------
# 結果存取
# ---------------------------------------------------------------------------
def load_store() -> dict:
    if os.path.exists(STORE):
        try:
            with open(STORE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"baseline": {}, "actions": {}}


def save_store(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _merge_rate(old: dict, new: dict[int, float]) -> dict:
    """把新一輪的出現率併進舊資料，同位址取較大值（保守：曾高頻就算高頻）。"""
    out = dict(old)
    for a, r in new.items():
        key = str(a)
        out[key] = max(out.get(key, 0.0), r)
    return out


# ---------------------------------------------------------------------------
# 交叉比對：算出每個動作獨有的候選函式
# ---------------------------------------------------------------------------
def summarize(data: dict, focus: str | None = None, top: int = 15) -> None:
    baseline = {int(k): v for k, v in data.get("baseline", {}).items()}
    actions = data.get("actions", {})
    if not actions:
        print("\n（尚無任何動作資料。先跑 --action attack 之類擷取一種動作。）")
        return

    names = [focus] if focus and focus in actions else list(actions.keys())
    for name in names:
        this = {int(k): v for k, v in actions[name].items()}
        # 對照組 = 基線 + 其他所有動作，每個位址取對照組裡的最高出現率。
        ref: dict[int, float] = dict(baseline)
        for other, rates in actions.items():
            if other == name:
                continue
            for k, v in rates.items():
                ik = int(k)
                ref[ik] = max(ref.get(ik, 0.0), v)

        scored = []
        for addr, rate in this.items():
            distinct = rate - ref.get(addr, 0.0)  # 這動作有、對照組沒有 → 越大越獨有
            scored.append((distinct, rate, addr))
        scored.sort(reverse=True)

        print(f"\n=== 動作「{name}」的候選函式（越上面越可能是它專屬）===")
        print(f"{'獨有分數':>8} {'本動作出現率':>12} {'絕對位址':>12} {'angel.dat RVA':>16}")
        print("-" * 54)
        for distinct, rate, addr in scored[:top]:
            rva = addr - MODULE_BASE
            print(f"{distinct:>8.2f} {rate:>12.2f}   0x{addr:08X}   0x{rva:X}")
        print("提示：獨有分數接近 1.0 = 幾乎每次做這動作才出現、平常不出現，最可疑。")
        print("      拿 RVA 去 Ghidra/IDA 開 angel.dat 對照該位址的呼叫點。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="動作差異偵測器：找攻擊/移動/放技能函式")
    ap.add_argument("--action", help="要測的動作名稱（如 attack / move / skill1）")
    ap.add_argument("--summary", action="store_true", help="只印出目前累積結果的交叉比對")
    ap.add_argument("--reset", action="store_true", help="清掉舊的累積資料重來")
    ap.add_argument("--pid", type=int, help="直接指定遊戲 PID（省略則自動找 angel.dat）")
    args = ap.parse_args()

    if args.reset and os.path.exists(STORE):
        os.remove(STORE)
        print("已清除舊資料。")

    data = load_store()

    if args.summary and not args.action:
        summarize(data)
        return 0

    if not args.action:
        ap.print_help()
        print("\n請用 --action <名稱> 指定要測哪個動作，或 --summary 看結果。")
        return 1

    # --- 找行程並掛上 send 攔截 ---
    ok, hint = injector.available()
    if not ok:
        print(hint)
        return 1

    if args.pid:
        pid = args.pid
        exe = injector.process_path(pid)
    else:
        found = find_angel_pid()
        if not found:
            print("找不到 angel.dat。請確認遊戲已登入進場；若遊戲以系統管理員執行，本工具也要。")
            return 1
        pid, title = found
        exe = injector.process_path(pid)
        print(f"找到遊戲：PID {pid}｜{title}")

    if not exe:
        print(f"無法取得 PID {pid} 的執行檔路徑（權限不足？請用系統管理員執行）。")
        return 1

    cap = injector.SendCapture(pid, exe)
    try:
        cap.start()
    except Exception as e:
        print(f"掛上 send 攔截失敗：{e}")
        return 1
    print("已掛上 send 攔截。（結束時會自動還原，對遊戲透明。）")

    try:
        base_col = _capture_phase(
            cap,
            "第一段【基線】：角色完全不動、不點任何東西，站著就好。",
        )
        act_col = _capture_phase(
            cap,
            f"第二段【動作：{args.action}】：只反覆做這一件事（例如狂點同一隻怪攻擊）。",
        )
    finally:
        cap.stop()
        print("\n已還原 send 攔截。")

    if act_col.packets == 0:
        print("動作期間沒抓到任何送出封包 —— 這個動作可能不會即時送封包，或沒真的做到。")
        print("請確認：真的有做動作、且角色在遊戲內（非選單畫面）。")
        return 1

    # 併進累積資料並存檔。
    data["baseline"] = _merge_rate(data.get("baseline", {}), base_col.rates())
    data.setdefault("actions", {})
    data["actions"][args.action] = _merge_rate(
        data["actions"].get(args.action, {}), act_col.rates()
    )
    save_store(data)
    print(f"結果已存到 {STORE}")

    summarize(data, focus=args.action)
    print("\n下一步：再測其他動作（--action move / --action skill1…），交叉排除會更準。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
