"""高階遊戲讀取器：用寫死的指標路徑，在任何一次啟動 / 任何多開分身上讀值。

這是給「監控軟體」用的門面：不碰掃描、不搜尋，只做「連上某個遊戲行程 → 用
app/game/signatures.py 裡登錄的具名座標讀值」。

單開：
    from app.core.game_reader import GameReader, find_instances
    reader = GameReader()
    reader.attach(pid)
    print(reader.read("exp"))          # 讀經驗值

多開（每個分身各一個讀取器，套同一條路徑）：
    for pid, title in find_instances(title_contains="天使"):
        r = GameReader()
        r.attach(pid)
        print(pid, title, r.read("exp"))

一個典型監控迴圈（每秒讀一次、算每小時經驗）就是在 read() 外面包一層時間差。
"""
from __future__ import annotations

from app.core import window as win
from app.core.memory import MemoryScanner
from app.game.signatures import SIGNATURES, Signature


def find_instances(
    title_contains: str | None = None,
) -> list[tuple[int, str]]:
    """列出符合條件的遊戲視窗，依 PID 去重，回傳 [(pid, title), ...]。

    多開時同一支遊戲會有多個行程 → 回傳多筆，每筆一個分身。
    （一個行程若有多個視窗，只取第一個標題、PID 只算一次。）
    """
    seen: dict[int, str] = {}
    for w in win.enumerate_windows(title_contains=title_contains):
        seen.setdefault(w.pid, w.title)
    return list(seen.items())


class GameReader:
    """連上單一遊戲行程，用具名座標讀 / 寫值。多開時每個分身各建一個。"""

    def __init__(self) -> None:
        self._scanner = MemoryScanner()

    # ------------------------------------------------------------------
    def attach(self, pid: int) -> None:
        """連上指定行程（內部會抓好當下的模組基底，供路徑解析）。"""
        self._scanner.open(pid)

    @property
    def attached(self) -> bool:
        return self._scanner.attached

    @property
    def pid(self) -> int | None:
        return self._scanner.pid

    def refresh_modules(self) -> None:
        """若懷疑模組有變動（極少見），可手動重抓模組基底。"""
        self._scanner.refresh_modules()

    def close(self) -> None:
        self._scanner.close()

    # ------------------------------------------------------------------
    def _sig(self, name: str) -> Signature:
        sigd = SIGNATURES.get(name)
        if sigd is None:
            raise KeyError(
                f"未定義的座標「{name}」。請先在 app/game/signatures.py 的 "
                "SIGNATURES 填入這條指標路徑。"
            )
        return sigd

    def resolve(self, name: str) -> int | None:
        """把具名座標解析成目前的真實位址（除錯用）；失敗回傳 None。"""
        return self._scanner.resolve_path(self._sig(name).path)

    def read(self, name: str):
        """讀取具名座標的值；解不出位址或讀取失敗時回傳 None。"""
        sigd = self._sig(name)
        return self._scanner.read_path(sigd.path, sigd.vt)

    def read_all(self) -> dict[str, object]:
        """一次讀出所有已登錄座標，回傳 {name: value or None}。"""
        out: dict[str, object] = {}
        for name, sigd in SIGNATURES.items():
            out[name] = self._scanner.read_path(sigd.path, sigd.vt)
        return out

    def write(self, name: str, value) -> None:
        """把具名座標寫成新值（需要寫入權限）。"""
        sigd = self._sig(name)
        addr = self._scanner.resolve_path(sigd.path)
        if addr is None:
            raise RuntimeError(f"座標「{name}」目前解不出位址（模組未載入或斷鏈）。")
        self._scanner.write_value(addr, sigd.vt, value)
