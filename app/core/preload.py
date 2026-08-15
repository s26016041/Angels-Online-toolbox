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
  ⚠ 反過來說：同一台**登出換角色**時 pid 不變，快取會一直回舊角色名 ——
  所以「重新偵測分身／重新整理」這類明確動作要用 `name_of(force=True)` 重掃。
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
# 狀態物件位址（能量晶化、寫目標都要用）。跟角色名同一次掃描拿到，不用多掃一遍。
# ⚠ 換地圖／重連時物件會搬家 —— 呼叫端讀到不合理的值時要 forget_state()。
_states: dict[int, int] = {}


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


def _scan_client(scanner, pid: int, account: str) -> str:
    """掃一次，把**角色名與狀態物件**一起拿回來並快取。回傳角色名。

    ★ 用 `entity.snapshot()` 而不是分別呼叫 `locate_player` / `locate_state`
      —— 它一遍讀取就同時比對三種 vtable，分開叫等於多掃好幾次
      （實測狀態物件單獨找要 ~320ms，這樣就省掉了）。

    ⚠ **不要用 charname.read_character_name()** ——
      那是全記憶體掃字串，一台就要 1.1~1.8 秒（實測）。
    """
    # 這裡才 import，避免 app.core 反過來相依 app.game（迴圈匯入）
    from app.game import entity, locate

    try:
        locate.warm(scanner)                  # 位址校正；全域只會真的做一次
        state, player, _ents, _r, _e = entity.snapshot(scanner)
        if state:
            _states[pid] = state
        if player:
            raw = scanner._read_bytes(player + entity.OFF_NAME,
                                      entity.NAME_MAX)
            if raw:
                got = bytes(raw).split(b"\x00")[0].decode("utf-8")
                if got:
                    return got
    except Exception:                          # noqa: BLE001
        pass
    return account


def state_of(pid: int, scanner=None, allow_scan: bool = True):
    """狀態物件位址。預讀過就是瞬間；沒有的話現場找一次並記起來。

    ⚠⚠⚠ **給了 scanner 就一定會先驗快取還是不是狀態物件**（一次 4-byte 讀取）。
      物件會搬家（換地圖、死亡重生、斷線重連、傳送），而舊位址那塊記憶體
      **照樣讀得到** —— 遊戲拿去放別的東西了。不驗就會把別人的堆積當成
      狀態物件交出去，呼叫端讀到的是垃圾值，而且因為「讀到了」永遠不會
      判定要重新定位。驗不過就當場丟掉快取，這一趟自然會重找。

    allow_scan=False：只用（驗過的）快取，**絕不做全記憶體掃描**。
      GUI 執行緒上每輪都要問的地方用這個，重找交給有節流的那條路。
    """
    from app.game import entity

    got = _states.get(pid)
    if got and scanner is not None and not entity.state_ok(scanner, got):
        _states.pop(pid, None)             # 搬家了 → 快取作廢，重新找
        got = None
    if got:
        return got
    if scanner is None or not allow_scan:
        return None
    got = entity.locate_state(scanner)
    if got:
        _states[pid] = got
    return got


def forget_state(pid: int) -> None:
    """狀態物件搬家了（換地圖、重連）—— 丟掉快取，下次重新找。"""
    _states.pop(pid, None)


def forget(pid: int) -> None:
    """同一個 pid 裡**換了人**（斷線重登、登出換帳號）—— 名字、狀態快取全作廢。

    ⚠ 不作廢的話，重建的分頁會用 `name_of()` 從快取撿回**上一個人**的名字，
      而名字一旦「像是解出來了」就不會再重讀（見 base_tab 的 _name_ok），
      舊名字就永遠黏在新帳號的分頁上。
    """
    _names.pop(pid, None)
    _states.pop(pid, None)


def _warm_libs() -> None:
    """把「第一次 import 要幾百毫秒」的第三方庫先載起來（keystone 組譯器 DLL）。

    不預熱的話，第一次按「開始掛機」裝跳板時會在 GUI 執行緒卡 0.3~1 秒
    （move.start() 檔內自己有註記）。這裡跑在預載的背景執行緒，卡也看不見。
    只 import、不裝任何東西；沒裝 keystone 也無妨，move.start() 會自己報錯。
    """
    try:
        import keystone                        # noqa: F401
    except Exception:                          # noqa: BLE001
        pass


def refresh(progress=None) -> list[Client]:
    """重新掃一遍所有分身並更新快取。

    progress: 可選的回呼 `f(已完成, 總數, 現在在做誰)` —— 給進度視窗用。
    """
    _warm_libs()
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
                name = _scan_client(sc, w.pid, acc)
            except Exception:                  # noqa: BLE001
                name = acc
            finally:
                sc.close()
            _names[w.pid] = name
        out.append(Client(w.pid, w.hwnd, w.title, acc, name))
    if progress:
        progress(len(wins), len(wins), "")
    return out


def name_of(pid: int, scanner=None, account: str = "",
            force: bool = False) -> str:
    """拿角色名。已經預讀過就是瞬間；沒有的話現場算一次並記起來。

    force=True 丟掉快取重掃 —— 同一台客戶端登出換角色時 pid 不變，
    不強制重掃就永遠是舊角色名。沒帶 scanner 時 force 無效（沒東西可掃）。
    """
    got = _names.get(pid)
    if got and not (force and scanner is not None):
        return got
    if scanner is not None:
        got = _scan_client(scanner, pid, account)
        _names[pid] = got
        return got
    return account


