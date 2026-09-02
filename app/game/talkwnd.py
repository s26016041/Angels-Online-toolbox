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
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    key = (getattr(scanner, "pid", 0), base)
    if key in _cache:
        return _cache[key]
    spot = None
    span = roulette._module_span(scanner, base)
    buf = roulette._read_image(scanner, base, span)
    if buf:
        i = buf.find(CMD_NAME)
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


def close_page(mover, scanner) -> tuple[bool, str]:
    """把現在這一頁「無異議對話」按掉（送 `messageclose`）。

    回 (叫下去了嗎, 說明)。⚠ 只保證叫下去了 —— 對話有沒有真的翻頁要看畫面
    （製作頁就是給人在旁邊看的），跑的時候靠「下一步做得成」當證據。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    spot = locate(scanner)
    if spot is None:
        return False, "⚠ 找不到 messageclose 的進入點（官方改寫了？）—— 已停用"
    world = _u32(scanner, spot.world_ptr)
    if not world or not 0x10000 < world < 0x7FFF0000:
        return False, "⚠ 讀不到世界物件"
    g = lua.globals_of(scanner, [WND_NAME]) or {}
    wnd = g.get(WND_NAME)
    if not wnd:
        # 讀不到就別亂送 —— 參數錯的話遊戲會拿去查別的視窗。
        return False, f"⚠ 讀不到 {WND_NAME}（對話視窗代號）"
    with mover.lock:
        ok = mover.call_sync(spot.close_fn, int(wnd), ecx=world,
                             timeout=CALL_TIMEOUT) is not None
    return (ok, "已送出「確定」") if ok else (False, "指令槽忙，等下一輪")
