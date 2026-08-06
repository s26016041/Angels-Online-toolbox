"""從外面呼叫遊戲裡的 Lua 函式。

    lua.call(mover, sc, "game.setrobotisrun", True)
    ok, val = lua.call(mover, sc, "game.getrobotvar_bool", 1502)

為什麼需要
----------
這個遊戲的**整套 UI 與官方外掛（天使守護精靈）都是 Lua 5.1 寫的**
（`script/game.so`、`autosupply.so`、`automall.so`）。它們**不送任何封包**
——「開始跑」只是客戶端內部的事。所以攔封包永遠看不到，只能直接叫 Lua。

繞過的死路（別再走，細節見記憶 ui-command-table）：
  ⛔ 重放擷取到的封包 —— 那三包是 `automallrequestmalldata`（跟伺服器要資料）
  ⛔ `0x5D3D97(0x1E, 1)` —— 是精靈跑起來之後自己送的
  ⛔ 直接寫設定旗標 —— 設定 ≠ 執行

怎麼做
------
    lua_State  L = [[CTX_PTR] + 8]
    lua_getfield(L, idx, "名字")   idx: -10002 = 全域表、-1 = 堆疊頂
    lua_pcall(L, 參數數, 回傳數, 0)   回傳 0 = 成功、2 = 執行期錯誤

參數**自己寫進 Lua 堆疊**（`lua_State + 8` 就是 top）：

    TValue = 16 bytes：值 8 + 型別 4 + 對齊 4
    型別：0 nil / 1 bool(int) / 3 數字(double) / 4 字串 / 5 表 / 6 函式

出錯時堆疊頂會留一個字串 —— 我們把它讀出來當診斷訊息。實際靠它解決過：
`autofight.lua:201: bad argument #1 to 'ischeck' (number expected, got boolean)`
—— 一看就知道那個參數要傳數字而不是布林。

⚠ 只支援數字與布林參數。字串要先在遊戲裡建 TString，做不到也用不到。
⚠ 位址全部由 `app/game/locate.py` 依 AOB 自動定位，改版位移會自己跟上。
"""
from __future__ import annotations

import struct

# ⚠ 這三個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
GETFIELD_FN = 0x006A4290     # lua_getfield(L, idx, k)
PCALL_FN = 0x006A4740        # lua_pcall(L, nargs, nresults, errfunc)
CTX_PTR = 0x00891010         # [CTX_PTR] + 8 = lua_State

GLOBALSINDEX = 0xFFFFD8EE    # -10002
TOPINDEX = 0xFFFFFFFF        # -1（堆疊頂）
OFF_TOP = 0x08               # lua_State 裡的 top
# Lua 5.1 的 lua_State：CommonHeader(6)+status(1)+對齊 → top 0x08、base 0x0C、
# l_G 0x10、ci 0x14、savedpc 0x18、**stack_last 0x1C**、stack 0x20。
# ⚠ 這個偏移是照 Lua 5.1 原始碼推的，**沒有像 OFF_TOP 那樣被實測驗證過**，
#   所以 `_room_for()` 只在讀出來的值「看起來合理」時才採用，
#   對不上就當作沒這道檢查（維持原本行為，不會因為推錯而讓功能失效）。
OFF_STACK_LAST = 0x1C
OFF_L_GT = 0x48              # lua_State 裡的全域表（拿來驗證版面）
TVALUE = 16

T_NIL, T_BOOL, T_NUMBER, T_STRING, T_TABLE, T_FUNCTION = 0, 1, 3, 4, 5, 6
OFF_TSTRING_LEN, OFF_TSTRING_DATA = 0x0C, 0x10

CALL_TIMEOUT = 0.5           # Lua 可能跑一小段，比送封包寬鬆一點


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def _sync(mover, fn: int, *args, ecx: int = 0):
    """呼叫遊戲函式一次。**不重試。**

    ⚠⚠ 曾經加過「排不進去就重試 3 次」，那是錯的方向：問題是**呼叫太密**，
      不是重試不夠。加了重試之後連打十幾輪，客戶端的訊息迴圈直接卡死
      （白狐實測）—— 每一次 `lua.call` 都會動遊戲的 Lua 堆疊，硬塞只會更糟。
      正確做法是**讓呼叫端少叫**（設定值快取起來，見 robot.potion_slots）。
    ★ 讀失敗回 None，呼叫端要當「不知道」處理，**不可以當成「沒有」**。
    """
    return mover.call_sync(fn, *args, ecx=ecx, timeout=CALL_TIMEOUT)


def state(scanner) -> int | None:
    """目前的 lua_State；讀不到或版面對不上回 None。

    ★ 用「全域表必須是 table」當健康檢查 —— 改版把結構改了就會擋在這裡，
      不會拿著錯的偏移去寫遊戲的記憶體。
    """
    ctx = _u32(scanner, CTX_PTR)
    if not 0x10000 < ctx < 0x7FFF0000:
        return None
    L = _u32(scanner, ctx + 8)
    if not 0x10000 < L < 0x7FFF0000:
        return None
    if _u32(scanner, L + OFF_L_GT + 8) != T_TABLE:
        return None
    return L


def _room_for(scanner, L: int, top: int, n: int) -> bool:
    """從 top 再推 n 個 TValue 還在堆疊裡面嗎？

    ⚠ 我們是直接把 TValue 寫進遊戲的 Lua 堆疊陣列，正常應該先叫
      `lua_checkstack`。沒得叫的話至少要自己看一下 `stack_last`，
      不然堆疊剛好很滿的時候就是**寫爆遊戲的堆積**。

    ★ `stack_last` 的偏移是推的（見 OFF_STACK_LAST），所以先做合理性檢查：
      它必須在 top 之上、而且距離不能大得離譜。看起來不對就回 True
      （＝不擋），維持跟以前一樣的行為 —— 寧可不擋，也不要因為偏移推錯
      而讓整個 Lua 功能失效。
    """
    last = _u32(scanner, L + OFF_STACK_LAST)
    if not (top < last < top + 0x100000):
        return True                       # 值不合理 → 這道檢查不算數
    return top + n * TVALUE <= last


def _restore_top(mover, scanner, L: int, base_top: int) -> None:
    """把 Lua 堆疊還原到我們動手之前的高度 —— ★★ **只降不升**。

    ⚠⚠⚠ 以前是無條件 `write(L+top, base_top)`，那是**延遲當機的來源**。

    `base_top` 是我們開始那一刻抄下來的，而中間每一次跳板呼叫回到遊戲的
    訊息迴圈時，**遊戲自己的 UI Lua 也在跑同一個 L**（計時器、視窗回呼）。
    所以還原的時候，遊戲的 top 可能已經比 base_top **低**了（它自己的框架
    跑完收掉了）。這時把 top 寫回較高的 base_top，等於**把堆疊頂拉高、
    蓋住一堆已經沒人維護的舊 TValue**。

    Lua 5.1 的 GC 標記階段會把 `L->stack` 到 `L->top` 之間**每一格**都當成
    活的物件去掃 —— 那些舊格子裡的型別標記說「我是可回收物件」，指標卻指向
    早就被釋放的 GCObject。於是 GC 在**不知道多久以後**的某一輪掃到它，
    直接讀爛記憶體。這正是「掛很久之後才當、而且完全看不出跟什麼有關」。

    只降不升就沒有這個問題：
      · top 比我們高 → 那是我們留下的東西，降回去（跟以前一樣）
      · top 比我們低 → 那是遊戲自己收掉的，**不要碰**
    """
    now = _u32(scanner, L + OFF_TOP)
    if now > base_top:
        mover.write(L + OFF_TOP, struct.pack("<I", base_top))


def _tvalue(v) -> bytes:
    if isinstance(v, bool):
        return struct.pack("<iiii", 1 if v else 0, 0, T_BOOL, 0)
    return struct.pack("<dii", float(v), T_NUMBER, 0)


def _read_value(scanner, at: int):
    """讀堆疊上的一個 TValue，轉成 Python 值（認不出來就回型別代號）。"""
    tt = struct.unpack("<i", bytes(scanner._read_bytes(at + 8, 4)))[0]
    if tt == T_NIL:
        return None
    if tt == T_BOOL:
        return bool(_u32(scanner, at))
    if tt == T_NUMBER:
        return struct.unpack("<d", bytes(scanner._read_bytes(at, 8)))[0]
    if tt == T_STRING:
        ts = _u32(scanner, at)
        n = min(_u32(scanner, ts + OFF_TSTRING_LEN), 400)
        raw = scanner._read_bytes(ts + OFF_TSTRING_DATA, n)
        if not raw:
            return ""
        # ⚠ 遊戲裡的 Lua 字串是 **UTF-8**，不是 big5。用 big5 解會變亂碼
        #   （「天使趴趴GO」被解成「憭拐蝙頞渲韌GO」才發現的）。
        #   big5 留作退路：舊資料檔偶爾還是 big5。
        for enc in ("utf-8", "big5"):
            try:
                return bytes(raw).decode(enc)
            except UnicodeDecodeError:
                continue
        return bytes(raw).decode("utf-8", errors="replace")
    return f"<型別 {tt}>"


def get_global(mover, scanner, name: str):
    """讀一個 Lua 全域的值（數字／布林／字串），讀不到回 None。

    ★ 拿來讀遊戲自己的常數（視窗代號、控制項 id…），這樣就不必在我們這邊
      寫死 —— 官方改號碼我們會自動跟上。
    """
    if not (mover and mover.active):
        return None
    L = state(scanner)
    if L is None:
        return None
    buf = mover.scratch()
    if not buf:
        return None
    with mover.lock:
        base_top = _u32(scanner, L + OFF_TOP)
        try:
            mover.write(buf, name.encode("ascii") + b"\0")
            if _sync(mover, GETFIELD_FN, L, GLOBALSINDEX, buf) is None:
                return None
            top = _u32(scanner, L + OFF_TOP)
            return _read_value(scanner, top - TVALUE) if top > base_top else None
        finally:
            _restore_top(mover, scanner, L, base_top)


def call(mover, scanner, path: str, *args) -> tuple[bool, object]:
    """呼叫 `path`（例如 "game.setrobotisrun" 或 "CreateRobotWindow"）。

    回傳 (成功嗎, 值)。失敗時值是 Lua 的錯誤訊息字串（可以直接顯示給人看）。

    ⚠ 全程抓著 mover 的鎖：中間夾雜別的呼叫會弄亂 Lua 堆疊。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    L = state(scanner)
    if L is None:
        return False, "找不到 lua_State（遊戲改版？）"
    buf = mover.scratch()
    if not buf:
        return False, "沒有暫存區"

    parts = path.split(".")
    with mover.lock:
        base_top = _u32(scanner, L + OFF_TOP)
        try:
            mover.write(buf, parts[0].encode("ascii") + b"\0")
            if _sync(mover, GETFIELD_FN, L, GLOBALSINDEX, buf) is None:
                return False, "呼叫排不進去"
            for part in parts[1:]:
                mover.write(buf, part.encode("ascii") + b"\0")
                if _sync(mover, GETFIELD_FN, L, TOPINDEX, buf) is None:
                    return False, "呼叫排不進去"

            top = _u32(scanner, L + OFF_TOP)
            tt = struct.unpack(
                "<i", bytes(scanner._read_bytes(top - TVALUE + 8, 4)))[0]
            if tt != T_FUNCTION:
                return False, f"{path} 不是函式（型別 {tt}）"

            if not _room_for(scanner, L, top, len(args)):
                return False, "Lua 堆疊快滿了，這次不推參數"
            for v in args:
                mover.write(top, _tvalue(v))
                top += TVALUE
            mover.write(L + OFF_TOP, struct.pack("<I", top))

            rc = _sync(mover, PCALL_FN, L, len(args), 1, 0)
            if rc is None:
                return False, "呼叫排不進去"
            now = _u32(scanner, L + OFF_TOP)
            val = _read_value(scanner, now - TVALUE) if now > base_top else None
            return (rc == 0), val
        finally:
            # ★ 不管成功失敗都把堆疊還原 —— 留東西在上面會慢慢漲爆
            #   ⚠ 只降不升，理由見 _restore_top（這是延遲當機的來源）
            _restore_top(mover, scanner, L, base_top)
