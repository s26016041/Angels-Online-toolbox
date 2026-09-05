r"""對話視窗：把「無異議對話」那一頁**按確定過掉**。

    talkwnd.close_page(mover, scanner)   → (送出去了嗎, 說明)

## 為什麼需要它（使用者 2026-09-02）

> 「比如跟 NPC 講話順序是這樣：無異議對話 → 選項1 → 無異議對話 → 結束」

沒有選項的那一頁**不是**按 talkaction 過的 —— 它有自己的封包。反組譯實證
（2026-09-02，UI 指令表 `messageclose` → 本體 `0x5D48C6`）：

    建包(代號 0x128, 內文 10)
      [內文+0] = word 0x125
      [內文+2] = [ [[世界+8] + 0x2A90] 查出來的物件 + 0x330 ]
      [內文+6] = [ 用參數查到的視窗物件 + 0xB0 ]
    送出

對照 `sell.TALK_FN`（talkaction）是 `建包(0x0B, 3)`、內文只有一個動作碼 ——
**兩件完全不同的事**。所以腳本要能分別記「送第 N 項」與「過掉這一頁」。

## 怎麼叫

⛔ 不自己重建那個封包（要湊兩個執行期物件欄位，改版一動就送垃圾）。
★ 叫遊戲自己那支：`thiscall(世界物件, 視窗代號)`，跟轉盤 `roulettestart`
  同一招（見 `roulette.py`）—— 位址從 **UI 指令表用字串當錨**推出來，
  官方改版位址跟著跑，零寫死。

參數是**視窗代號**：UI 腳本呼叫的是 `messageclose(WND_MESSAGE)`，那個值
就是 Lua 全域 `WND_MESSAGE`（`lua.globals_of` 讀得到）。
⚠⚠ `WND_MESSAGE` 非 0 **不代表對話開著**（[[lua-readonly-inspect]] 實測 5 台
  有 3 台沒在對話也非 0）——所以這支**不判斷開沒開**，由呼叫端決定何時叫；
  沒開著時叫下去，遊戲那支自己會因為查不到東西而不送（`test edi,edi; je`）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import lua, roulette

GAME_MODULE = "angel.dat"
# UI 指令表裡的名字（明文），拿來當錨 —— ⛔ 不寫死函式位址。
CMD_NAME = b"messageclose\x00"
# 指令本體很短（實測 41 bytes 到 ret）；多讀一點無妨。
CMD_BODY = 0x60
CALL_TIMEOUT = 1.0
# 對話視窗的 Lua 全域名（參數就是它的值）。
WND_NAME = "WND_MESSAGE"
# ★★ 這一頁有哪些選項 ＝ Lua 全域 `MESSAGE_OPTIONn` 非 0（2026-09-02 實測：
#   雪狐點瑪莫魯斯，對話一開 MESSAGE_OPTION1=5190、OPTION2=5191，3~6 是 0）。
#   有了它就不必把「過場」一格一格記進腳本 —— 沒有選項就自己按確定翻頁，
#   有選項才照腳本送（使用者 2026-09-02：「只要沒選項就幫我對話到結束
#   或出現選項」）。
OPT_NAME = "MESSAGE_OPTION%d"
OPT_MAX = 10
# 這一頁是不是「無異議對話」（沒有選項）。
TALK_NAME = "MESSAGE_IS_TALK"
# 這一則訊息的編號／頭像 —— 只拿來當「換頁了沒」的邊沿訊號。
MSG_NAME = "MESSAGE_MSG_ID"
FACE_NAME = "MESSAGE_FACE"

_cache: dict = {}


@dataclass(frozen=True)
class Spot:
    """從 UI 指令表推出來的兩個位址。"""

    cmd_fn: int          # messageclose 指令本體（只拿來報告出處）
    world_ptr: int       # [這裡] = 世界物件（thiscall 的 this）
    close_fn: int        # thiscall(世界物件, 視窗代號) = 送 0x128


def locate(scanner) -> Spot | None:
    """推位址；推不出來回 None（＝**大聲停用**，不亂叫別的函式）。"""
    return _locate_cmd(scanner, CMD_NAME, "")


def _locate_cmd(scanner, cmd_name: bytes, tag: str) -> Spot | None:
    """從 UI 指令表用**指令名字串**當錨，推出 (指令本體, 全域, 本體函式)。

    骨架：`mov ecx,[某個全域]` → **緊接著那個 call** 就是本體。
    `messageclose` 與 `ismessageend` 兩支長得一模一樣，所以共用這一支。
    """
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    key = (getattr(scanner, "pid", 0), base, tag)
    if key in _cache:
        return _cache[key]
    spot = None
    span = roulette._module_span(scanner, base)
    buf = roulette._read_image(scanner, base, span)
    if buf:
        i = buf.find(cmd_name)
        j = buf.find(struct.pack("<I", base + i)) if i >= 0 else -1
        if j >= 0:
            fn = struct.unpack_from("<I", buf, j + 4)[0]
            if base <= fn < base + span:
                body = buf[fn - base: fn - base + CMD_BODY]
                world = call = None
                # ⚠⚠ 錨要用**骨架**不是「第幾個 call」：這支指令只有 41 bytes，
                #   往後多讀的部分已經是**別的函式**，拿「最後一個 call」會抓到
                #   鄰居的（2026-09-02 第一版就這樣抓到 0x53D6C0）。
                #   骨架是：`mov ecx,[世界全域]` → **緊接著那個 call** 就是本體
                #   （前面那個 call 是「取參數」）；遇到 ret 就停。
                for k in range(len(body) - 6):
                    op = body[k]
                    if body[k:k + 2] == b"\x8b\x0d" and world is None:
                        world = struct.unpack_from("<I", body, k + 2)[0]
                    elif op == 0xE8 and world is not None and call is None:
                        call = fn + k + 5 + struct.unpack_from("<i", body,
                                                               k + 1)[0]
                    elif op == 0xC3 and call is not None:
                        break
                if (world and call and base <= world < base + span
                        and base <= call < base + span):
                    spot = Spot(cmd_fn=fn, world_ptr=world, close_fn=call)
    _cache[key] = spot
    return spot


# ★★★ 「對話視窗現在到底開著沒」的**硬訊號**（使用者 2026-09-02：
#   「對話後關視窗太慢了，不知道在等啥，請要明確知道有沒有視窗」）。
#   UI 指令 `ismessageend` 的骨架就是答案：
#       ecx = [視窗管理器全域]
#       eax = 依視窗代號查視窗物件      ← 查不到（0）＝**沒有這個視窗**
#   所以叫那支查一下就好，不必再用「值有沒有變」猜。
FIND_CMD = b"ismessageend" + bytes(1)


def find_spot(scanner) -> Spot | None:
    """推「查視窗」那一支的位址（跟 messageclose 同一個骨架，見 locate）。"""
    return _locate_cmd(scanner, FIND_CMD, "_find")


# ★ `ismessageend` 指令本體（0x53CC9F，2026-09-03 反組譯）：
#     wnd = 依代號查視窗(GetWindowById)；查不到 → 1（結束）
#     否則 → byte [wnd + 0x148]                 ← 伺服器隨這一頁送來的「最後一頁」旗標
#   OnUpdateMessage（Lua）就是拿它決定要不要顯示「結束」。
#   GetWindowById = `[管理器 + (代號 & 0x1FFF)*4 + 0x20]`，再驗 `[物件+0x10] == 代號`。
#   ⚠ 這幾個是結構偏移（允許寫死，出處如上），改版靠 patch-doctor 重驗。
MSG_END_OFF = 0x148
WND_SLOT_MASK = 0x1FFF      # 出處：上面 GetWindowById 那行反組譯（代號 & 0x1FFF）
WND_TABLE_OFF = 0x20        # 出處：上面 GetWindowById 那行反組譯
WND_ID_OFF = 0x10           # 出處：上面 GetWindowById 那行反組譯（驗 [物件+0x10] == 代號）


def _wnd_object(scanner) -> int | None:
    """對話視窗物件的位址（純讀，照 GetWindowById 走一遍）；沒有／讀不到回 None。"""
    spot = find_spot(scanner)
    if spot is None:
        return None
    mgr = _u32(scanner, spot.world_ptr)
    g = lua.globals_of(scanner, [WND_NAME]) or {}
    wnd = int(g.get(WND_NAME) or 0) & 0xFFFFFFFF
    if not mgr or not 0x10000 < mgr < 0x7FFF0000 or not wnd:
        return None
    obj = _u32(scanner, mgr + (wnd & WND_SLOT_MASK) * 4 + WND_TABLE_OFF)
    if not obj or not 0x10000 < obj < 0x7FFF0000:
        return None
    return obj if _u32(scanner, obj + WND_ID_OFF) == wnd else None


def message_ended(scanner) -> bool | None:
    """現在這一頁是不是**最後一頁**（＝遊戲 `ismessageend` 回的那個旗標）。

    純讀。**沒有視窗回 None**（不是 True）——呼叫端拿它分「按了確定之後要不要
    等下一頁」：False＝伺服器還有下一頁會來，要等；True＝按完就結束。
    """
    try:
        obj = _wnd_object(scanner)
        if not obj:
            return None
        raw = scanner._read_bytes(obj + MSG_END_OFF, 1)
        return bool(bytes(raw)[0]) if raw else None
    except Exception:                                      # noqa: BLE001
        return None


def window_open(mover, scanner) -> bool | None:
    """對話視窗**現在**開著嗎。**讀不到／叫不動回 None**（＝不知道）。

    ⚠ 回 None 千萬不要當成 False —— 「不知道」跟「沒有視窗」對呼叫端是
      完全不同的兩句話（[[bag-false-empty-guards]] 那條規矩）。
    """
    if not (mover and mover.active):
        return None
    spot = find_spot(scanner)
    if spot is None:
        return None
    mgr = _u32(scanner, spot.world_ptr)
    if not mgr or not 0x10000 < mgr < 0x7FFF0000:
        return None
    g = lua.globals_of(scanner, [WND_NAME]) or {}
    wnd = g.get(WND_NAME)
    if not wnd:
        return None
    with mover.lock:
        got = mover.call_sync(spot.close_fn, int(wnd), ecx=mgr,
                              timeout=CALL_TIMEOUT)
    if got is None:
        return None                      # 指令槽忙 → 不知道，不要亂判
    return bool(got)


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def options(scanner) -> set[int] | None:
    """這一頁有哪些選項（1 起算）。**讀不到整張 Lua 表回 None**，不是空集合。

    ⚠ 「讀不到」跟「這一頁沒有選項」是兩件事：前者不可以拿去當「純對話」
      而去按確定（那會把有選項的頁面亂按過去）。呼叫端要分開處理。

    ⚠⚠⚠ **這個值會留著上一次的殘留，不可以單獨當「現在這一頁有選項」用**
      （2026-09-02 實測：雪狐對話開著時 OPTION1/2 非 0 是對的，但同一刻
      **沒有在對話**的北極狐是 {1,2}、黑狐是 {1,2,3,4,5}）——跟
      [[lua-readonly-inspect]] 記的 `WND_xxx 非 0 ≠ 視窗開著` 完全同一個坑。
      要當「這一頁的選項」用，必須先有「新的一頁來了」的邊沿訊號
      （`MESSAGE_MSG_ID` 變了）再讀，否則就是拿舊資料做決定。
    """
    names = [OPT_NAME % i for i in range(1, OPT_MAX + 1)]
    g = lua.globals_of(scanner, names)
    if g is None:
        return None
    return {i for i in range(1, OPT_MAX + 1) if g.get(OPT_NAME % i)}


@dataclass(frozen=True)
class Page:
    """對話**最近一頁**的樣子。⚠ 是「最近一頁」不是「現在開著的那一頁」。"""

    is_talk: bool                # `MESSAGE_IS_TALK`（⚠ 不是「沒有選項」的意思）
    options: tuple               # 有哪些選項（1 起算）
    sig: tuple                   # 換頁偵測用的整包簽章

    @property
    def has_options(self) -> bool:
        """這一頁有選項嗎 ＝ **只看 `MESSAGE_OPTIONn`**。

        ⚠⚠⚠ 2026-09-02 翻案：一開始以為 `MESSAGE_IS_TALK=1` 就代表「這一頁
          沒有選項」（那時嵐狐剛好 IS_TALK=1、OPTION 全 0）。**錯的** ——
          後來實機讀到嵐狐 `is_talk=True` 而且 `options=(1,2)` **同時成立**。
          IS_TALK 是「這是 NPC 對話訊息」之類的類別旗標，不是選項旗標。
          拿它當「純對話」判會把有選項的頁面按確定過掉，然後說「對話結束了
          但腳本還有選項沒送到」（使用者當場回報的 bug）。
        """
        return bool(self.options)

    @property
    def is_plain(self) -> bool:
        """「無異議對話」＝這一頁沒有任何選項。"""
        return not self.options


def page(scanner) -> Page | None:
    """讀對話最近一頁的樣子；讀不到整張 Lua 表回 None。

    ★★ 判「有沒有選項」**只看 `MESSAGE_OPTIONn`**（見 `has_options`）——
      ⛔ 不要用 `MESSAGE_IS_TALK`，實機讀到過 IS_TALK=1 跟 OPTION 同時成立。
    ⚠⚠ 但這些值**對話關掉之後照樣留著**（嵐狐畫面上根本沒有對話框，
      `IS_TALK` 還是 1）——所以它描述的是「最近一頁」。要當「現在這一頁」
      用，呼叫端**必須先看到 `sig` 變了**（＝新的一頁來了）才採信，
      跟 [[lua-readonly-inspect]]「WND_xxx 非 0 ≠ 開著」是同一條紀律。
    """
    names = ([OPT_NAME % i for i in range(1, OPT_MAX + 1)]
             + [TALK_NAME, MSG_NAME, FACE_NAME, WND_NAME])
    g = lua.globals_of(scanner, names)
    if g is None:
        return None
    opts = tuple(i for i in range(1, OPT_MAX + 1) if g.get(OPT_NAME % i))
    return Page(is_talk=bool(g.get(TALK_NAME)),
                options=opts,
                sig=(bool(g.get(TALK_NAME)), opts, g.get(MSG_NAME),
                     g.get(FACE_NAME), g.get(WND_NAME)))


# ★ 關掉對話視窗本身的 Lua 函式（`reports/lua_allglobals_*.txt` 裡有這個全域）。
#   ⚠ `messageclose` 那個封包只是**告訴伺服器**「我按了確定」，畫面上的框
#     是客戶端 UI 自己收掉的 —— 只送封包會變成「帶著對話框到處跑」
#     （使用者 2026-09-02 回報）。Lua 開關視窗是安全操作（[[lua-engine]]）。
# ★ 對話視窗**確定鈕**的 Lua 處理式（倒 bytecode：messageclose ＋ window.destroy）。
CLOSE_BTN_FN = "OnMessageClose"
CLOSE_WND_FN = "DestroyMessageWnd"


def close_window(mover, scanner) -> bool:
    """把對話視窗**從畫面上收掉**。已經關著的話這支是空操作。

    ⚠ Lua 呼叫**不可以叫太密**（[[lua-engine]]：太密會把訊息迴圈弄卡死），
      所以只在「一段對話收尾」與「停機」時各叫一次。
    """
    if not (mover and mover.active):
        return False
    try:
        ok, _val = lua.call(mover, scanner, CLOSE_WND_FN)
        return bool(ok)
    except Exception:                                      # noqa: BLE001
        return False


def close_page(mover, scanner) -> tuple[bool, str]:
    """把現在這一頁「無異議對話」按掉 ＝ **跟遊戲的確定鈕一模一樣**。

    ★★★ 2026-09-03 實機（黑狐 遺落之地分流6）抓到「確定沒反應→再按→卡住」的
      真兇：對話視窗的確定鈕是 Lua `OnMessageClose(視窗代號)`，倒出來的 bytecode
      只做兩件事 ——
          game.messageclose(視窗代號)   ← 通知伺服器（我們以前只做這件）
          window.destroy(視窗代號)      ← **把視窗物件銷毀**（我們以前沒做）
      少了第二件，視窗物件一直留在管理器裡：`WND_MESSAGE` 查得到、遊戲自己的
      `ismessageend` 也回「還沒結束」、對話頁簽章一格不變 → 呼叫端只能一直
      「確定沒反應→再按一次」。所以這裡直接叫那顆按鈕的 Lua 處理式，
      跟滑鼠按下去走的是同一段程式；叫不動才退回舊路（C 函式）＋destroy。
    ⚠ Lua 呼叫不可以太密（[[lua-engine]]）：這支一頁只按一次，呼叫端有節流。
    回 (叫下去了嗎, 說明)。對話有沒有真的翻頁要看「視窗還在不在／簽章變了沒」。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    g = lua.globals_of(scanner, [WND_NAME]) or {}
    wnd = g.get(WND_NAME)
    if not wnd:
        # 讀不到就別亂送 —— 參數錯的話遊戲會拿去查別的視窗。
        return False, f"⚠ 讀不到 {WND_NAME}（對話視窗代號）"
    try:
        ok, _val = lua.call(mover, scanner, CLOSE_BTN_FN, int(wnd))
        if ok:
            return True, "已按「確定」（messageclose＋destroy）"
    except Exception:                                      # noqa: BLE001
        ok = False
    # 安全退化：Lua 叫不動 → 照舊送 messageclose（C 函式），再補 destroy。
    spot = locate(scanner)
    if spot is None:
        return False, "⚠ 找不到 messageclose 的進入點（官方改寫了？）—— 已停用"
    world = _u32(scanner, spot.world_ptr)
    if not world or not 0x10000 < world < 0x7FFF0000:
        return False, "⚠ 讀不到世界物件"
    with mover.lock:
        ok = mover.call_sync(spot.close_fn, int(wnd), ecx=world,
                             timeout=CALL_TIMEOUT) is not None
    if ok:
        close_window(mover, scanner)
    return (ok, "已送出「確定」(退化路)") if ok else (False, "指令槽忙，等下一輪")
