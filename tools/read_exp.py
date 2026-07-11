"""驗證 / 迷你監控：自動找出所有天使之戀分身，讀出各自的經驗值。

用法（從專案根目錄執行）：
    py tools/read_exp.py            # 讀一次就結束
    py tools/read_exp.py --loop     # 每秒持續讀（Ctrl+C 結束）

它怎麼認出遊戲：對每個視窗程序試著解析 signatures.py 裡的 "exp" 路徑，
解得出來的就是天使之戀（多開時會同時列出每個分身）。這也示範了「同一條
指標路徑，套在每個獨立分身上各自解析」的多開作法。

驗證重點：重開遊戲 2~3 次，每次都跑這支，若經驗值都正確，這條路徑就穩定，
可以放心用；若某次讀到亂數或解析失敗，代表路徑不穩，回掃描分頁重找。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win
from app.core.game_reader import GameReader


def find_game_readers() -> list[tuple[int, str, GameReader]]:
    """回傳所有「exp 路徑解得出來」的程序 = 天使之戀分身。"""
    readers: list[tuple[int, str, GameReader]] = []
    seen: set[int] = set()
    for w in win.enumerate_windows():
        if w.pid in seen:
            continue
        seen.add(w.pid)
        reader = GameReader()
        try:
            reader.attach(w.pid)
        except Exception:
            continue
        if reader.resolve("exp") is not None:
            readers.append((w.pid, w.title, reader))
        else:
            reader.close()
    return readers


def main() -> int:
    readers = find_game_readers()
    if not readers:
        print(
            "找不到天使之戀。請確認：遊戲開著且已登入、angel.dat 已載入；"
            "若遊戲以系統管理員身分執行，請同樣以系統管理員身分跑本工具。"
        )
        return 1

    print(f"找到 {len(readers)} 個分身：")
    loop = "--loop" in sys.argv
    try:
        while True:
            for pid, title, reader in readers:
                exp = reader.read("exp")
                exp_text = "讀取失敗（路徑可能不穩）" if exp is None else str(exp)
                print(f"  PID {pid:<6} | {title[:24]:<24} | 經驗 = {exp_text}")
            if not loop:
                break
            print("-" * 48)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, _, reader in readers:
            reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
