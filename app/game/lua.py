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

TValue = 16 bytes：值 8 + 型別 4 + 對齊 4；`lua_State + 8` 就是 top。
型別：0 nil / 1 bool(int) / 3 數字(double) / 4 字串 / 5 表 / 6 函式

★★ 2026-08-16 起，整串操作（getfield 鏈→推參數→pcall→抄結果→還原 top）
   由**原子序列 stub** 在遊戲主執行緒上一口氣做完（一次跳板呼叫）——
   舊的「Python 分好幾步做、中間被遊戲自己的 Lua 插隊」是崩潰 dump 實證的
   當機來源，詳見 `_seq_stub_asm` 上面那段說明。

出錯時堆疊頂會留一個字串 —— 我們把它讀出來當診斷訊息。實際靠它解決過：
`autofight.lua:201: bad argument #1 to 'ischeck' (number expected, got boolean)`
—— 一看就知道那個參數要傳數字而不是布林。

⚠ 參數支援數字、布林與**字串**（字串走 `lua_pushstring`，見 PUSHSTRING_FN；
  2026-08-11 為「採集指定資源」那份字串清單加的）。
⚠ 位址全部由 `app/game/locate.py` 依 AOB 自動定位，改版位移會自己跟上。
"""
from __future__ import annotations

import struct

# ⚠ 這四個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
GETFIELD_FN = 0x006A4290     # lua_getfield(L, idx, k)
PCALL_FN = 0x006A4740        # lua_pcall(L, nargs, nresults, errfunc)
CTX_PTR = 0x00890FF0         # [CTX_PTR] + 8 = lua_State
# lua_pushstring(L, const char*) —— 2026-08-11 加，為了「採集指定資源」那份
# **字串**清單（`game.robotvar_add_stringlist`）。
# 出處：反組譯 `game.getrobotvar_string`(0x594529) 的收尾三行 ——
#     push eax（讀出來的字串）/ push esi（L＝第一個參數）/ call 0x6A4BC0
#     / pop ecx / pop ecx        ← cdecl、兩個參數、呼叫端清堆疊
# ★ 它會把字串**複製進 Lua 的字串池**，所以我們那塊暫存區之後可以隨便覆寫。
PUSHSTRING_FN = 0x006A4BC0

GLOBALSINDEX = 0xFFFFD8EE    # -10002
TOPINDEX = 0xFFFFFFFF        # -1（堆疊頂）
# ★ 出處：Lua 5.1 原始碼 lstate.h 的 lua_State 版面（CommonHeader＋status 對齊後
#   top 在 +0x08，x86）；遊戲用的就是原版 Lua 5.1（見 memory lua-engine）。
OFF_TOP = 0x08               # lua_State 裡的 top
# Lua 5.1 的 lua_State：CommonHeader(6)+status(1)+對齊 → top 0x08、base 0x0C、
# l_G 0x10、ci 0x14、savedpc 0x18、**stack_last 0x1C**、stack 0x20。
# ⚠ 這個偏移是照 Lua 5.1 原始碼推的，**沒有像 OFF_TOP 那樣被實測驗證過**，
#   所以 stub 的堆疊餘裕檢查只在讀出來的值「看起來合理」（stack_last > top）
#   時才擋，對不上就當作沒這道檢查（不會因為推錯而讓功能失效）。
OFF_STACK_LAST = 0x1C
# ★ 出處：Lua 5.1 lstate.h —— hook(+0x44) 之後的 l_gt（x86 版面）；讀取端拿它驗版面。
OFF_L_GT = 0x48              # lua_State 裡的全域表（拿來驗證版面）
# ★ 出處：Lua 5.1 lobject.h 的 TValue（Value union 8B 對齊＋tt → x86 共 16 bytes）。
TVALUE = 16

T_NIL, T_BOOL, T_NUMBER, T_STRING, T_TABLE, T_FUNCTION = 0, 1, 3, 4, 5, 6
OFF_TSTRING_LEN, OFF_TSTRING_DATA = 0x0C, 0x10

CALL_TIMEOUT = 0.5           # Lua 可能跑一小段，比送封包寬鬆一點

# ---------------------------------------------------------------------------
# ★★★ 原子序列 stub（2026-08-16）：整串 Lua 操作在**一次**跳板呼叫裡做完
# ---------------------------------------------------------------------------
# 為什麼非這樣不可：舊寫法是 Python 這邊「叫 getfield → 讀 top → 自己寫
# TValue → 寫回 top → 叫 pcall」，每一步之間遊戲主執行緒都在跑**自己的**
# UI／精靈 Lua（同一個 lua_State）。我們的裸寫入撞上它正在執行的那一刻，
# 堆疊就錯位 —— 崩潰 dump 實證（2026-08-16 解的 err*.dmp）：TValue 裡是
# 字串內容 'numb'、是程式碼位址、EIP 跳到垃圾 —— 全是 VM 拿到爛 TValue 的
# 死法。「只降不升」的還原只擋掉一半，**「最壞只回錯誤訊息」的舊評估是錯的**。
#
# 現在：把「getfield 鏈 → 驗是函式 → 推參數 → pcall → 抄結果 → 還原 top」
# 全部寫成一小段機器碼，放進跳板頁的第二段程式碼區（mover.aux_code），
# 用一次 call_sync 叫它 —— 整串都在遊戲主執行緒上一口氣做完，
# 中間**不可能**有別的 Lua 插隊，還原 top 也因此可以無條件做。
#
# ⚠ 跟舊版相同的既有風險（沒有變好也沒有變壞）：getfield 中途遇到 nil
#   會走 Lua 的錯誤路徑（longjmp）——但我們查的全域（game、視窗函式）
#   一直都存在，舊程式同樣沒防這條。
# ⚠ pcall 執行中若遊戲抽訊息重入跳板，_BUSY 閂會擋（見 move._stub_asm）。
#
# 參數區版面（P，放在 scratch + _SEQ_P_OFF）：
#   +0x00 L                +0x04 getfield_fn      +0x08 pcall_fn
#   +0x0C pushstring_fn    +0x10 n_names          +0x14 name_ptr[4]
#   +0x24 n_args           +0x28 do_pcall(0=只讀值) +0x2C rc_out
#   +0x30 結果 TValue(16B)  +0x40 結果字串長度      +0x44 結果字串內容(≤0xB8)
#   +0x100 參數×8，每個 20B：+0 kind(0=現成TValue/1=字串指標) +4 內容(16B)
# 字串池放在 scratch + _SEQ_STR_OFF（函式名 + 字串參數）。
# ⚠ scratch 區段分配：lua 名字緩衝 0x000、jumpmap 0x100、sell 0x140、
#   supply/exchange 0x180、team 0x1C0、produce 0x200；這裡佔 0x300~0x4A0（P）
#   與 0x500~0x7F8（字串池），別人要加新區段請避開。
_SEQ_P_OFF = 0x300
_SEQ_STR_OFF = 0x500
_SEQ_POOL_MAX = 0x2F0
_SEQ_STR_MAX = 0xB8          # 結果字串最多抄這麼多（錯誤訊息夠用了）
_RC_NOT_FUNCTION = 0xFFFFFFFE
_RC_NO_ROOM = 0xFFFFFFFD
_MAX_NAMES = 4
_MAX_ARGS = 8


def _seq_stub_asm() -> str:
    """原子序列 stub 的組譯原文（keystone、位置無關：位址全走暫存器）。

    暫存器約定：ebx=P、esi=L、edi=進場時抄下的 top（callee-saved，
    getfield/pcall 不會動它們）。呼叫前 `mov ebp,esp`、呼叫後 `mov esp,ebp`
    —— cdecl/stdcall 都安全（跟 move._stub_asm 同一招）。
    回傳值（eax→跳板 _RET→call_sync 回傳）＝pcall 的 rc；
    0xFFFFFFFE=查到的不是函式、0xFFFFFFFD=堆疊快滿不敢推。
    ⚠ keystone 把無前綴數字當十六進位，所以一律寫 0x。
    """
    return """
    mov ebx, dword ptr [esp+0x4]
    mov esi, dword ptr [ebx]
    mov edi, dword ptr [esi+0x8]
    mov dword ptr [ebx+0x2C], 0x0
    xor ecx, ecx
    names_loop:
    cmp ecx, dword ptr [ebx+0x10]
    jge names_done
    mov eax, 0xFFFFD8EE
    test ecx, ecx
    jz idx_ready
    mov eax, 0xFFFFFFFF
    idx_ready:
    mov edx, dword ptr [ebx+ecx*4+0x14]
    push ecx
    mov ebp, esp
    push edx
    push eax
    push esi
    mov eax, dword ptr [ebx+0x4]
    call eax
    mov esp, ebp
    pop ecx
    inc ecx
    jmp names_loop
    names_done:
    cmp dword ptr [ebx+0x28], 0x0
    je copy_result
    mov eax, dword ptr [esi+0x8]
    cmp eax, edi
    jbe fail_notfn
    cmp dword ptr [eax-0x8], 0x6
    jne fail_notfn
    mov edx, dword ptr [esi+0x1C]
    cmp edx, eax
    jbe room_ok
    mov ecx, dword ptr [ebx+0x24]
    shl ecx, 0x4
    add ecx, eax
    add ecx, 0x20
    cmp ecx, edx
    ja fail_room
    room_ok:
    xor ecx, ecx
    args_loop:
    cmp ecx, dword ptr [ebx+0x24]
    jge args_done
    imul edx, ecx, 0x14
    lea edx, [ebx+edx+0x100]
    cmp dword ptr [edx], 0x1
    je arg_string
    mov eax, dword ptr [esi+0x8]
    mov ebp, dword ptr [edx+0x4]
    mov dword ptr [eax], ebp
    mov ebp, dword ptr [edx+0x8]
    mov dword ptr [eax+0x4], ebp
    mov ebp, dword ptr [edx+0xC]
    mov dword ptr [eax+0x8], ebp
    mov ebp, dword ptr [edx+0x10]
    mov dword ptr [eax+0xC], ebp
    add eax, 0x10
    mov dword ptr [esi+0x8], eax
    jmp arg_next
    arg_string:
    mov edx, dword ptr [edx+0x4]
    push ecx
    mov ebp, esp
    push edx
    push esi
    mov eax, dword ptr [ebx+0xC]
    call eax
    mov esp, ebp
    pop ecx
    arg_next:
    inc ecx
    jmp args_loop
    args_done:
    mov ebp, esp
    push 0x0
    push 0x1
    push dword ptr [ebx+0x24]
    push esi
    mov eax, dword ptr [ebx+0x8]
    call eax
    mov esp, ebp
    mov dword ptr [ebx+0x2C], eax
    copy_result:
    mov eax, dword ptr [esi+0x8]
    cmp eax, edi
    jbe restore
    sub eax, 0x10
    mov edx, dword ptr [eax]
    mov dword ptr [ebx+0x30], edx
    mov edx, dword ptr [eax+0x4]
    mov dword ptr [ebx+0x34], edx
    mov edx, dword ptr [eax+0x8]
    mov dword ptr [ebx+0x38], edx
    mov edx, dword ptr [eax+0xC]
    mov dword ptr [ebx+0x3C], edx
    cmp dword ptr [eax+0x8], 0x4
    jne restore
    mov edx, dword ptr [eax]
    mov ecx, dword ptr [edx+0xC]
    cmp ecx, 0xB8
    jbe len_ok
    mov ecx, 0xB8
    len_ok:
    mov dword ptr [ebx+0x40], ecx
    push esi
    push edi
    lea esi, [edx+0x10]
    lea edi, [ebx+0x44]
    cld
    rep movsb
    pop edi
    pop esi
    restore:
    mov dword ptr [esi+0x8], edi
    mov eax, dword ptr [ebx+0x2C]
    ret
    fail_notfn:
    mov dword ptr [ebx+0x2C], 0xFFFFFFFE
    jmp restore
    fail_room:
    mov dword ptr [ebx+0x2C], 0xFFFFFFFD
    jmp restore
    """


def _ensure_stub(mover) -> int:
    """把原子序列 stub 裝進這份跳板的第二段程式碼區；回位址，裝不了回 0。

    程式碼位置無關（位址全走 P），所以每份跳板只要寫一次；
    寫失敗（遊戲沒了）回 0，呼叫端大聲失敗 —— **不退回舊的競態寫法**。
    快取跟著區塊位址走：跳板換了一塊記憶體就重寫一次。
    """
    base, size = mover.aux_code()
    if not base:
        return 0
    if getattr(mover, "_lua_seq_addr", 0) == base:
        return base
    with mover.lock:
        if getattr(mover, "_lua_seq_addr", 0) == base:
            return base
        try:
            import keystone
            ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
            ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
            shell, _n = ks.asm(_seq_stub_asm(), addr=base)
        except Exception:                                  # noqa: BLE001
            return 0
        if not shell or len(shell) > size:
            return 0
        if not mover.write(base, bytes(shell)):
            return 0
        mover._lua_seq_addr = base
        return base


def _seq_pack(L, parts, args, do_pcall, pool_base,
              getfield_fn, pcall_fn, pushstring_fn):
    """把（路徑, 參數）組成 stub 的字串池與參數區位元組。回 (池, P)；太長回 None。

    純函式 —— **離線模擬測試（scratchpad/lua_stub_sim.py）用的就是這一份**，
    版面改這裡測試就跟著測到，不會兩邊各寫一份然後假通過。
    """
    blob, name_ptrs = b"", []
    for part in parts:
        name_ptrs.append(pool_base + len(blob))
        blob += part.encode("ascii") + b"\0"
    entries = b""
    for v in args:
        if isinstance(v, str):
            # ★ 字串交給遊戲的 lua_pushstring 建 TString（stub 裡呼叫）。
            # ⚠⚠ UTF-8 不是 big5：寫 big5 遊戲比對不到，而且讀回來有 big5
            #   退路、看起來「一致」，對帳抓不到（2026-08-11 踩過）。
            entries += struct.pack("<II", 1, pool_base + len(blob)) + b"\0" * 12
            blob += v.encode("utf-8") + b"\0"
        else:
            entries += struct.pack("<I", 0) + _tvalue(v)
    if len(blob) > _SEQ_POOL_MAX:
        return None
    head = struct.pack("<4I", L, getfield_fn, pcall_fn, pushstring_fn)
    head += struct.pack("<I", len(parts))
    head += struct.pack("<4I", *(name_ptrs + [0] * (4 - len(name_ptrs))))
    head += struct.pack("<3I", len(args), 1 if do_pcall else 0, 0)
    head += b"\0" * 0x14                   # 結果 TValue + 字串長度清零
    return blob, head + b"\0" * (0x100 - len(head)) + entries


def _seq_run(mover, scanner, parts, args, do_pcall) -> tuple[int, object] | str:
    """組 P、跑一次 stub。成功回 (rc, 結果值)，失敗回原因字串。

    ⚠ 整段抓 mover.lock：P 與字串池是共用暫存區，寫參數→叫→讀結果
      不能被別的呼叫切開（跟舊版抓鎖的理由相同，只是窗口小很多）。
    """
    if not (mover and mover.active):
        return "跳板沒裝好"
    if not (GETFIELD_FN and PCALL_FN):
        return "Lua 函式定位失敗（遊戲改版？）—— 這個功能停用"
    if len(parts) > _MAX_NAMES or len(args) > _MAX_ARGS:
        return "路徑太深或參數太多"
    if any(isinstance(v, str) for v in args) and not PUSHSTRING_FN:
        return "pushstring 定位失敗（遊戲改版？）"
    L = state(scanner)
    if L is None:
        return "找不到 lua_State（遊戲改版？）"
    stub = _ensure_stub(mover)
    if not stub:
        return "Lua stub 裝不起來"
    scratch = mover.scratch()
    if not scratch:
        return "沒有暫存區"
    p, pool = scratch + _SEQ_P_OFF, scratch + _SEQ_STR_OFF
    packed = _seq_pack(L, parts, args, do_pcall, pool,
                       GETFIELD_FN, PCALL_FN, PUSHSTRING_FN or 0)
    if packed is None:
        return "字串太長"
    blob, data = packed

    with mover.lock:
        if blob and not mover.write(pool, blob):
            return "寫字串池失敗"
        if not mover.write(p, data):
            return "寫參數區失敗"
        rc = mover.call_sync(stub, p, timeout=CALL_TIMEOUT)
        if rc is None:
            return "呼叫排不進去"
        rc &= 0xFFFFFFFF
        val = _seq_result(scanner, p)
    return rc, val


def _seq_result(scanner, p: int):
    """讀 stub 抄回來的結果 TValue（字串內容 stub 已抄進 P，沒有 GC 競態）。"""
    raw = scanner._read_bytes(p + 0x30, 0x14)
    if not raw or len(raw) < 0x14:
        return None
    b = bytes(raw)
    tt = struct.unpack_from("<i", b, 8)[0]
    if tt == T_BOOL:
        return bool(struct.unpack_from("<I", b, 0)[0])
    if tt == T_NUMBER:
        return struct.unpack_from("<d", b, 0)[0]
    if tt == T_STRING:
        n = min(struct.unpack_from("<I", b, 16)[0], _SEQ_STR_MAX)
        s = scanner._read_bytes(p + 0x44, n) if n else b""
        if not s:
            return ""
        # ⚠ 遊戲裡的 Lua 字串是 UTF-8（big5 留作舊資料退路，跟 globals_of 同）。
        for enc in ("utf-8", "big5"):
            try:
                return bytes(s).decode(enc)
            except UnicodeDecodeError:
                continue
        return bytes(s).decode("utf-8", errors="replace")
    if tt == T_NIL:
        return None
    return f"<型別 {tt}>"


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


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


def globals_of(scanner, names) -> dict | None:
    """**純讀**遊戲 Lua 的全域常數（數字／布林）。讀不到回 None。

    不注入、不呼叫 Lua、不動堆疊 —— 只走全域表的雜湊節點陣列，
    跟 `tools/dump_lua_globals.py` 同一條路（那支已經用了很久）。

    用途：`WND_xxx` 這種「視窗開著沒有」的全域（開著是執行期代號），
    例如製作面板的 `WND_MAKE`。跟 `robot._wnd` 同一招。
    ⚠⚠ 「關著是 0」**不是每個 WND_ 都成立**——WND_MESSAGE 掃 5 台在線分身
      3 台沒在 NPC 對話也非 0（2026-08-19）、WND_AUTOSUPPLY 面板關著也非 0。
      新窗要當「開著沒」用之前先多台對照；不可靠就改邊沿偵測
      （記基準、看「值變了且非 0」，範本 supply._wait_dialog）。

    ⚠ 回傳的 dict **只含真的讀到的名字**；某個名字不在裡面代表
      「這個全域現在不存在」，跟「讀不到整張表」（回 None）是兩件事。
    """
    want = set(names)
    L = state(scanner)
    if L is None:
        return None
    tab = _u32(scanner, L + OFF_L_GT)
    if not 0x10000 < tab < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(tab + 7, 1)
    node = _u32(scanner, tab + 0x10)
    if not raw or not 0x10000 < node < 0x7FFF0000:
        return None
    lsize = raw[0]
    if lsize > 20:                       # 2^20 個節點已經荒謬，當版面對不上
        return None
    out: dict = {}
    for i in range(1 << lsize):
        # Node = 32 bytes：值 TValue(16) + 鍵(值 8 + tt 4 + next 4)
        blob = scanner._read_bytes(node + i * 32, 32)
        if not blob or len(blob) < 32:
            continue
        b = bytes(blob)
        if struct.unpack_from("<I", b, 24)[0] != T_STRING:
            continue
        ts = struct.unpack_from("<I", b, 16)[0]
        n = _u32(scanner, ts + OFF_TSTRING_LEN)
        if not 0 < n < 64:
            continue
        s = scanner._read_bytes(ts + OFF_TSTRING_DATA, n)
        if not s:
            continue
        try:
            name = bytes(s).decode("ascii")
        except UnicodeDecodeError:
            continue
        if name not in want:
            continue
        vtt = struct.unpack_from("<I", b, 8)[0]
        if vtt == T_NUMBER:
            v = struct.unpack_from("<d", b, 0)[0]
            out[name] = v if v != v or v in (float("inf"), float("-inf")) \
                else (int(v) if v == int(v) else v)
        elif vtt == T_BOOL:
            out[name] = bool(struct.unpack_from("<I", b, 0)[0])
        elif vtt == T_NIL:
            pass                         # nil ＝ 沒有這個全域
        else:
            out[name] = vtt              # 別的型別：至少讓呼叫端知道非 0
    return out


def _tvalue(v) -> bytes:
    if isinstance(v, bool):
        return struct.pack("<iiii", 1 if v else 0, 0, T_BOOL, 0)
    return struct.pack("<dii", float(v), T_NUMBER, 0)


def get_global(mover, scanner, name: str):
    """讀一個 Lua 全域的值（數字／布林／字串），讀不到回 None。

    ★ 拿來讀遊戲自己的常數（視窗代號、控制項 id…），這樣就不必在我們這邊
      寫死 —— 官方改號碼我們會自動跟上。
    ★ 2026-08-16 起走原子序列 stub（getfield＋抄結果＋還原 top 一口氣做完，
      見檔案中段的說明）；只要讀**數字／布林**的話 `globals_of` 純讀更便宜。
    """
    if not (mover and mover.active):
        return None
    got = _seq_run(mover, scanner, [name], (), do_pcall=False)
    if isinstance(got, str):
        return None
    _rc, val = got
    return val


def call(mover, scanner, path: str, *args) -> tuple[bool, object]:
    """呼叫 `path`（例如 "game.setrobotisrun" 或 "CreateRobotWindow"）。

    回傳 (成功嗎, 值)。失敗時值是 Lua 的錯誤訊息字串（可以直接顯示給人看）。

    ★ 2026-08-16 起整串（getfield 鏈→驗是函式→推參數→pcall→抄結果→還原
      top）在**一次**跳板呼叫裡由遊戲主執行緒一口氣做完（_seq_run）——
      舊寫法分好幾步做、中間被遊戲自己的 Lua 插隊，是崩潰 dump 實證的
      當機來源（詳見 _seq_stub_asm 上面那段說明）。
    ⚠⚠ 「排不進去就重試」仍然是錯的方向：問題是**呼叫太密**，不是重試
      不夠（白狐實測連打十幾輪把訊息迴圈弄卡死）。要少叫，不要硬塞。
    """
    got = _seq_run(mover, scanner, path.split("."), args, do_pcall=True)
    if isinstance(got, str):
        return False, got
    rc, val = got
    if rc == _RC_NOT_FUNCTION:
        return False, f"{path} 不是函式"
    if rc == _RC_NO_ROOM:
        return False, "Lua 堆疊快滿了，這次不推參數"
    return (rc == 0), val
