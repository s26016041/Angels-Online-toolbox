"""開程式時先把「慢的東西」一次讀完，之後切分頁就不會卡住。

## 為什麼要有這支

使用者回報：「每次第一次切到能量晶化／自動掛機都會卡一下」。
量出來的原因是每個分頁自己在 `reload_instances()` 裡做兩件慢事：

    charname.read_character_name()   全記憶體掃  一台 1.1~1.8 秒，五台 7.7 秒
    locate.warm()                    讀 6.4MB 模組，全部合計 0.5 秒

所以第一次切過去就是七、八秒的凍結，而且**每個分頁各卡一次**。

## 這支怎麼解

1. **改用玩家物件讀角色名**（`+0x1D4`）而不是全記憶體掃字串。
   實測五台結果完全一致，但 7.7 秒 → 3.4 秒。
2. **開程式時就做完**，配一個有進度條的視窗，讓等待變成看得見、只發生一次。
3. 結果快取起來，分頁改用 `name_of()`，切過去是瞬間的。

## 注意

* 快取的是 **pid → 角色名**。玩家關掉某台重開會換 pid，那台就會重算
  （見 [[memory-re-pitfalls]] 第 10 條：pid 是快照，不能當永久身分）。
* `refresh()` 可以重跑（分頁上的「重新整理」會用到）。
* 純讀記憶體。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core import charname
from app.core import window as win
from app.core.memory import MemoryScanner

GAME_TITLE = "Angels Online"
GAME_CLASS = "_MIDAGEONL_"

_names: dict[int, str] = {}


@dataclass(frozen=True)
class Client:
    """一個遊戲分身。`name` 是角色名，拿不到時退回帳號。"""

    pid: int
    hwnd: int
    title: str
    account: str
    name: str


def windows() -> list:
    """列出所有遊戲視窗（不碰記憶體，很快）。"""
    seen: set[int] = set()
    out = []
    for w in win.enumerate_windows(title_contains=GAME_TITLE):
        if GAME_CLASS not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append(w)
    return out


def _name_from_player(scanner, pid: int, account: str) -> str:
    """從玩家物件讀角色名。**不要用 charname.read_character_name()** ——
    那是全記憶體掃字串，一台就要 1.1~1.8 秒（實測），這裡只要 0.4~0.9 秒。
    """
    # 這裡才 import，避免 app.core 反過來相依 app.game（迴圈匯入）
    from app.game import entity, locate

    try:
        locate.warm(scanner)                  # 位址校正；全域只會真的做一次
        obj = entity.locate_player(scanner)
        if obj:
            raw = scanner._read_bytes(obj + entity.OFF_NAME, entity.NAME_MAX)
            if raw:
                got = bytes(raw).split(b"\x00")[0].decode("utf-8")
                if got:
                    return got
    except Exception:                          # noqa: BLE001
        pass
    return account


def refresh(progress=None) -> list[Client]:
    """重新掃一遍所有分身並更新快取。

    progress: 可選的回呼 `f(已完成, 總數, 現在在做誰)` —— 給進度視窗用。
    """
    wins = windows()
    out: list[Client] = []
    for i, w in enumerate(wins):
        acc = charname.account_from_title(w.title) or ""
        if progress:
            progress(i, len(wins), acc)
        name = _names.get(w.pid)
        if not name:
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
                name = _name_from_player(sc, w.pid, acc)
            except Exception:                  # noqa: BLE001
                name = acc
            finally:
                sc.close()
            _names[w.pid] = name
        out.append(Client(w.pid, w.hwnd, w.title, acc, name))
    if progress:
        progress(len(wins), len(wins), "")
    return out


def name_of(pid: int, scanner=None, account: str = "") -> str:
    """拿角色名。已經預讀過就是瞬間；沒有的話現場算一次並記起來。"""
    got = _names.get(pid)
    if got:
        return got
    if scanner is not None:
        got = _name_from_player(scanner, pid, account)
        _names[pid] = got
        return got
    return account


def forget(pid: int | None = None) -> None:
    """清掉快取（某一台或全部）。分身關掉重開、或想強制重算時用。"""
    if pid is None:
        _names.clear()
    else:
        _names.pop(pid, None)
