"""死亡後點「回標記點」復活：跟換分流同一招，泛用送包一個呼叫搞定。

    revive.to_mark(mover)     # 等同在死亡視窗點「回標記點」

原理
----
死亡視窗的按鈕跟切分流、選定怪物一樣，走遊戲的**泛用送包函式**：

    0x5D3D97(0x0C, 目標實體ID)    ← 選定怪物（attack.select 一直在用的）
    0x5D3D97(0x47, 分流編號)      ← 切換分流（channel.switch）
    0x5D3D97(0x02, 0)             ← 回標記點復活   ★ 就差一個種類碼

`0x5D3D97` 就是 `attack.SELECT_FN`。cdecl、兩個整數、**沒有 this** ——
掛機每次選怪都在叫它，沒有新的崩潰風險。

## 怎麼找到的（2026-08-07）

使用者死亡時點「回標記點」，攔到 5 包。第 1 包的呼叫鏈（解碼手法見
[[generic-send-fn]]：`0x589B65` 那一層的參數就是 `0x5D3D97` 收到的東西）：

    0x5D3DC6　…                    ← 0x5D3D97 內部（建封包）
    0x589B65　參數 (2, 0, …)       ← 0x5D3D97(0x02, 0)

`0x589B27` 那個處理常式是通用的 UI 送包指令，代碼是視窗定義帶進去的常數。
後面 4 包（`0x5C05F5`／`0x5D6020`／`0x5D606F`／`0x602F26` 建的）是客戶端
收到「復活成功」後自己送的進場流程 —— 不用、也不該由我們重放。

⚠ 參數 0 只驗證過「回標記點」這一種（攔到什麼就原樣重送）。死亡視窗的
  其他選項沒攔過 —— 「代碼有規律不代表參數有規律」，別拿這支去猜。
⚠ 人活著的時候不要送：沒驗證過伺服器會怎麼處理，呼叫端要自己確認死了才叫。
"""
from __future__ import annotations

from app.game import attack

# ★ 泛用送包的「復活選擇」種類碼。出處：使用者死亡點「回標記點」的攔包
#   呼叫鏈 0x589B65 那層 (2, 0)（見檔頭「怎麼找到的」）。
REVIVE_CODE = 0x02
MARK_PARAM = 0          # 「回標記點」（攔包原樣；其他值未驗證，不要亂試）


def to_mark(mover) -> bool:
    """點「回標記點」。排不進指令槽回 False（呼叫端下一拍重試就好）。

    mover: 已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）。
    """
    if not (mover and mover.active):
        return False
    return attack._send(
        mover, ((attack.SELECT_FN, (REVIVE_CODE, MARK_PARAM)),))


def close_window(mover, scanner) -> tuple[bool, str]:
    """關掉死亡選擇視窗。復活之後才叫 —— 還死著就別關，人要自己選。

    ## 為什麼要另外做這件事

    `OnOkDeadWnd` 是**送包＋關窗**兩件事，我們只送了包，所以視窗會留在
    畫面上（使用者 2026-08-07 回報：「功能正常但彈出視窗還是沒有不見」）。

    ## 為什麼是叫 `CloseDeadWnd`

    遊戲自己就有這支 Lua，而且**不用參數、自帶防呆**（bytecode 純讀倒出來）：

        function CloseDeadWnd()
          if WND_DEAD_OPTION ~= 0 then
            window.destroy(WND_DEAD_OPTION); WND_DEAD_OPTION = 0
          end
        end

    比自己 `destroy(parent(find(…)))` 安全得多：沒有參數可以傳錯，視窗
    已經關了再叫也只是空轉。`ShowDeadOptionWindow` 也確認
    `WND_DEAD_OPTION = window.create(380, …)` 就是那個視窗本身，
    不會誤殺別的介面。

    ⚠ 從外部呼叫 Lua 有堆疊競爭的破口（見 [[game-crash-root-causes]]），
      所以**一次死亡只叫一次**，而且失敗就算了 —— 關不掉只是視窗留著，
      不影響已經復活的角色。同樣的取捨見 `exchange.close_window()`。
    """
    from app.game import lua                    # 避免循環相依

    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not lua.get_global(mover, scanner, "WND_DEAD_OPTION"):
        return False, "死亡視窗沒開著"          # 已經關了，不必叫
    ok, val = lua.call(mover, scanner, "CloseDeadWnd")
    return (True, "") if ok else (False, str(val))
