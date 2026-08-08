"""自動登入：全程寫記憶體＋呼叫遊戲自己的函式，不占鍵盤滑鼠。

    login.sign_in(mover, scanner, "s26011034", "s26016041")   # 帳密登入
    login.enter_game(mover, scanner, slot=0)                   # 進入遊戲（第 1 個角色）

使用者硬要求：自動登入**全程背景、完全不可占用實體鍵鼠**（見 memory 的
auto-login-findings）。舊做法用 pyautogui 打字，那會搶走使用者當下的鍵盤，
而且背景滑鼠點擊在這遊戲根本無效。這裡改成兩件事都交給遊戲自己做。

## 一、帳密登入

登入按鈕的完整動作就是一支 **thiscall、零參數**的函式 `0x538959`：

    選中的伺服器 = 伺服器清單控制項(0xA92) 的選取索引   ← 沒選就直接 return
    if [0x890997] != 0 或 [0x8909B9] != 0:
        跳過讀 UI，直接用現成的全域帳密        ← ★ 我們要的就是這個分支
    else:
        帳號框(0xA8E).text → 0x890980（20 bytes）
        密碼框(0xA90).text → 0x890998（32 bytes）
    關掉舊連線 → [這+0x1038] = 0 → 建 socket → connect（非同步）

連線接上之後，**送登入包的是連線物件自己的 tick** `0x538EEB`：

    if [這+0x1038] == 1 且 socket 已連上:
        if [0x8909B9] != 0:  0x537C8C(帳號, token@0x890FCC)   代號 0x14
        elif [0x890997] != 0: 0x537C78(帳號, blob@0x890D30)    代號 0x13
        else:                 0x537C64(帳號, 密碼@0x890998)    代號 0x02  ★一般帳密
        [這+0x1038] += 1        ← 所以只會送一次

⚠⚠ **兩個旗標同時決定「跳不跳過 UI」和「送哪一種登入包」**，這是本檔最彆扭
   的地方：要讓 `0x538959` 不去讀（空的）輸入框，就得舉一支旗標；但旗標舉著
   的話 tick 就不會走一般帳密那條。所以做法是**舉起 → 呼叫 → 馬上放下**：

     1. 寫 0x890980 / 0x890998
     2. FLAG_TOKEN=0、FLAG_BLOB=1      （讓 0x538959 跳過讀 UI）
     3. call 0x538959(this)
     4. FLAG_BLOB=0                    （讓 tick 走一般帳密）

   中間有沒有可能被 tick 插隊？第 3 步回來時 socket 還在**三向交握**，
   tick 的前提「socket 已連上」至少要一個網路來回（毫秒級），而第 4 步在
   微秒內就做完了。理論上仍是競態，實務上差三個數量級。
   ⚠ 這條是靜態反組譯推出來的，**還沒實機驗證**。

`this` 怎麼來：登入畫面物件用 vtable 掃出來（`find_screen()`），
再交叉驗證 +0x10/+0x14/+0x18/+0x2C 四個副 vtable —— 五個指標同時對上，
不會誤認。進遊戲之後這個物件會被釋放，掃不到就是「現在不在登入畫面」。

## 二、選頻道（分流）—— 不能省的一步

登入成功後畫面會停在「請選擇分流遊戲世界」。⚠⚠ 點那一列**做的是兩件事**：

    ① `[0x890810] = 該列編號`（控制項 id 0x9E3，選取變更的訊息）
    ② `0x537353(登入畫面物件)`（同一個 id、**另一種訊息**，`0x539E40` 那個 call）
       → 跳出「與伺服器連線中」，跟伺服器要角色清單，畫面推進到選角色

只做 ① 的話畫面會一直停在分流清單，接著送「進入遊戲」就卡在「與伺服器連線中」
—— 這正是使用者回報的症狀。2026-08-07 在使用者開放的測試分身上實跑：
寫 `[0x890810]=2` 再 `0x537353(obj)` → 1.5 秒後截圖已是選角色畫面（白狐 LV80），
登入畫面物件隨即被釋放。

## 三、進入遊戲（頻道值也在這一包裡）

角色選好之後按「進入遊戲」送的是 `0x50F880(角色格號)`（stdcall，代號 6、
內文 0x25），走**登入連線**。它組包的內容是：

    [封包+2] = 角色格號（byte，參數帶進來的）
    [封包+3] = [0x890810]      ← ★ **頻道（分流），0 起算**
    [封包+4] = 0x890814 起 0x20 bytes ← ★ **保護密碼的 MD5**（十六進位小寫）

★★ 保護密碼**沒有自己的封包**：使用者提供「輸入保護密碼→按勾勾→登入完成」的
完整擷取，5 包全部是原本就知道的那組，一包都沒多。它是混在這一包裡的那 32 bytes。
證據：使用者的保護密碼是 `7777777`，而那 32 bytes 讀到
`dc0fa7df3d07904a09288bd2d2bb5f40` ＝ `md5("7777777")`。
程式碼也對得上：收到角色清單的處理常式（`0x50E58C` 附近）是「讀輸入框 →
strlen → `0x6C1A10`（MD5）→ 抄進 0x890814」，最後才 `call 0x5103E8`（進入遊戲）。

⚠ 這也是「送了進入遊戲卻沒反應」的真正原因之一：新開的分身那 32 bytes 是空的，
  伺服器當然不受理。有設保護密碼的帳號一定要先寫進去。

★★ **選頻道不是獨立的一包**，就是上面那個位元組 —— 所以「選頻道」對我們來說
只是**寫一個 byte**，不必碰那個頻道選擇畫面。三處交叉印證：

  1. 五台分身實測對照：雅典娜-2 → `1`、雅典娜-3 → `2`、雅典娜-5 → `4`。
  2. `0x591B0B`（Lua 的換頻道函式）拿 `[0x890810]` 當「目前頻道」比對，
     而且前面 `cmp ecx, 8` 把上限框在 8。
  3. 跟遊戲中換分流 `0x5D3D97(0x47, 分流−1)` 同樣是 0 起算（見 [[channel-switch]]）。

⚠ 使用者原本以為卡住的是「選伺服器」，實測是**選頻道 1~5**。伺服器
（雅典娜／維納斯／邱比特）在登入畫面就選好了，`0x538959` 用的是那個
下拉選單的選取值，我們沒有碰。

呼叫端在送之前還會先寫 `[0x890BE8] = 角色格號`，我們照做 —— 因為接下來那包
「進遊戲伺服器」(`0x5D5FB7`) 是讀這個全域的。

## 伺服器清單怎麼讀（純讀，不讀檔案）

`[[0x89096C]+0x500]` ~ `+0x504` 是一個等距陣列，記錄 0x178 bytes：

    +0x00 名稱（UTF-8 內嵌）   +0x40 ip 字串   +0x50 port
    +0x54 分流數   +0x58 伺服器編號   +0x5C 分流數（同一個值，兩格）

跟 [[channel-switch]] 的 `channel.count()` 讀的是同一份表，但那邊是**全記憶體
掃描**（0.3~1 秒）而且要靠視窗標題的伺服器名 —— 登入畫面根本還沒有標題，
所以這裡改成直接從陣列邊界走訪，微秒級而且登入畫面也讀得到。
`[0x890CA8]` 是登入時選中的伺服器索引（實測 2 = 雅典娜）。

★ 使用者擷取的「進入遊戲」有 5 包，但**我們只需要送第 1 包**：
    ① 0x50F880(格號)   代號 6    ← 只有這包是「按鈕按下去」送的
    ② 0x5D5FB7()       代號 2    ┐
    ③ 0x5D604F()       代號 3    │ 都在 0x5AA3xx 的**伺服器訊息處理常式**裡，
    ④ 0x602EC9(byte)   代號 0x15E│ 是客戶端收到伺服器回應後自己送的
    ⑤ 0x5D3D97(0x1E,1) 代號 0x16 ┘
  ⚠ 順帶更正 memory：④ 不是安全密碼 —— 它設的 Lua 全域是
    `WND_PET_EQUIP_SUIT_SELECT`（寵物裝備套裝），值是從伺服器封包讀出來的。

送出前會確認 `[0x89097C]`（登入連線編號）不是 0；是 0 代表根本還沒連上
登入伺服器，送了也只是丟進 `0x711130` 的空槽，直接擋下比較好交代。

相關：[[login-packet-chain]]、[[auto-login-findings]]、[[packet-opcode-table]]、
[[aob-auto-locate]]（下面每個位址都在 locate.SIGS，改版會自動重新定位）
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import subprocess

from app.core.memory import VALUE_TYPES

GAME_EXE = "angel.dat"      # 遊戲本體（副檔名不是 exe，但就是個一般執行檔）

# --- 函式（locate.warm() 會重新定位；定位失敗會被清成 0，Mover.call 擋下）---
LOGIN_FN = 0x00538959       # thiscall(this)：登入按鈕的完整動作
PICK_CHANNEL_FN = 0x00537353  # thiscall(this)：確定分流，畫面推進到選角色
ENTER_FN = 0x0050F880       # stdcall(角色格號)：進入遊戲
GETWIDGET_FN = 0x00624D17   # stdcall(視窗id, 控制項id)，ecx=UI管理者：取控制項

# --- 資料位址（定位失敗會保留下面寫死的值，讀錯只會「做不到」不會崩潰）---
VT_LOGIN = 0x007D6C94       # 登入畫面／登入連線物件的主 vtable
ACCOUNT = 0x00890980        # 帳號緩衝區，20 bytes
PASSWORD = 0x00890998       # 密碼緩衝區，32 bytes
FLAG_BLOB = 0x00890997      # ≠0 → 用 0x890D30 的 512-byte 憑證
FLAG_TOKEN = 0x008909B9     # ≠0 → 用 0x890FCC 的 token（啟動器社群登入）
CHAR_SLOT = 0x00890BE8      # 選中的角色格號（0 起算）
CONN_ID = 0x0089097C        # 登入連線編號；0 = 還沒連上
CHANNEL = 0x00890810        # 頻道／分流，**1 byte、0 起算**
PROTECT_HASH = 0x00890814   # 保護密碼的 MD5，32 個十六進位小寫字（不補 NUL）
PROTECT_LEN = 32            # ENTER_FN 就是從這裡 strncpy 0x20 bytes 進封包
APP_PTR = 0x0089096C        # 應用程式主物件；伺服器陣列掛在它 +0x500/+0x504
SERVER_INDEX = 0x00890CA8   # 登入時選中的伺服器索引（進陣列用）
EULA_OK = 0x00890FFC        # 「授權合約已同意」旗標，1 byte

# 登入畫面物件上的欄位
OBJ_UI_MGR = 0x0C           # UI 管理者（取控制項時當 this）
OBJ_WIN_ID = 0x1064         # 這個畫面的視窗 id（取控制項的第一個參數）

# 伺服器清單控制項。⚠ 這是 UI 定義檔給的編號、不是程式碼位址，沒得 AOB 定位；
# 改版動 UI 才會變，屆時 pick_server() 會「找不到控制項」而不是選錯。
SERVER_LIST_ID = 0xA92
ITEM_VEC_BEGIN = 0x150      # 控制項的項目向量（begin/end）
ITEM_VEC_END = 0x154
ITEM_SELECTED = 6           # 項目物件 +6：非 0 = 被選中
#   ↑ 遊戲自己的「取選取索引」就是掃這個向量、回傳第一個 +6 非 0 的索引
#     （0x624F9D）。實機驗證：三個項目只有索引 2 是 1，跟 SERVER_INDEX 讀到的
#     2（雅典娜）完全吻合。

# 伺服器記錄的版面（見檔頭）
SRV_BEGIN = 0x500
SRV_END = 0x504
SRV_STRIDE = 0x178
SRV_NAME = 0x00
SRV_PORT = 0x50
SRV_SUBSET_A = 0x54
SRV_ID = 0x58
SRV_SUBSET_B = 0x5C
MAX_SUBSET = 8              # 上限跟遊戲一致：0x591B03 的 `cmp ecx, 8`
MAX_SERVERS = 64            # 合理性上限，避免版面變了就跑一個天文數字的迴圈

# --- 角色清單 ---------------------------------------------------------------
# 一格 0xB7 bytes。CHAR_HAS 是遊戲自己拿來判斷「這一格有沒有角色」的欄位
# （`0x5103FB: cmp dword [格號*0xB7 + 0x8909DF], 0`），另外兩個欄位跟它固定相對：
#     −0x10  旗標，bit 0x40000000 立起來代表這格不能進（遊戲也是這樣擋）
#     +0x04  角色名（UTF-8，內嵌）★ 實機讀到「白狐」「Foxsw」對上畫面
CHAR_HAS = 0x008909DF
CHAR_STRIDE = 0xB7
CHAR_FLAG_OFF = -0x10
CHAR_NAME_OFF = 0x04
CHAR_BLOCKED = 0x40000000
MAX_SLOTS = 8

# 掃到 VT_LOGIN 之後還要對上的副 vtable（建構函式 0x5363A0 一口氣寫的那組）。
# ⚠ 只取建構函式**不會再改寫**的四個：+0x30/+0x34 後面會被覆蓋成別的值。
# ⚠ 存的是「相對 VT_LOGIN 的距離」，不是絕對位址 —— 這四個跟 VT_LOGIN 是
#   **同一個類別**的一組 vtable，編譯器連著擺在 .rdata；小改版讓 .rdata 位移時
#   整組一起動、距離不變，locate 把 VT_LOGIN 修好這組就跟著對。（以前寫死
#   絕對位址：VT_LOGIN 被 locate 修好、這四個還是舊值 → find_login_object
#   永遠對不上 → 自動登入在小改版後整個停用 —— 2026-08-08 體檢抓到的縫。）
#   距離會變的只有「這個類別本身的虛擬函式增減」那種底層改動 —— 那時掃不到
#   → 回 None → 大聲停用，跟以前一樣安全。
#   （8/04 實測「位移量不一致 +0x10/+0x18」講的是**不同類別之間**；
#   同類別連續的一組是同一塊，整塊一起移。）
# 2026-08-04 的絕對位址留當文件：VT_LOGIN=0x7D6C94，
#   +0x10→0x7D6CAC　+0x14→0x7D6CC4　+0x18→0x7D6CD0　+0x2C→0x7D6CDC
SUB_VTABLE_DELTAS = {0x10: 0x18, 0x14: 0x30, 0x18: 0x3C, 0x2C: 0x48}

ACCOUNT_LEN = 20            # strncpy 0x14
PASSWORD_LEN = 32           # strncpy 0x20

# 登入那一下要建 socket、關舊連線、動 UI，比純送包的函式重得多。
CALL_TIMEOUT = 1.0
ENTER_TIMEOUT = 0.3


def protect_hash(password: str) -> bytes:
    """保護密碼 → 遊戲放進封包的那 32 bytes（MD5 的十六進位小寫字串）。

    ★ 不是猜的：使用者提供保護密碼是 `7777777`，而遊戲記憶體那 32 bytes 讀到
      `dc0fa7df3d07904a09288bd2d2bb5f40`，正好等於 `md5("7777777")`。
      程式碼那邊也對得上 —— 收到角色清單的處理常式（`0x50E58C` 附近）是
      「讀輸入框 → strlen → 0x6C1A10（MD5）→ 把結果抄進 0x890814」。

    ⚠ 剛好 32 bytes，**不補結尾 NUL**：遊戲是 `strncpy(封包+4, 這裡, 0x20)`，
      抄滿 32 個字，補 NUL 反而會把最後一個字元擠掉。
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest().encode("ascii")


def _pad(text: str, size: int) -> bytes:
    """把字串補成固定長度的 C 字串；太長就截掉（遊戲自己也是 strncpy）。"""
    raw = text.encode("ascii", errors="ignore")[:size - 1]
    return raw + b"\x00" * (size - len(raw))


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def server_info(scanner) -> tuple[str, int] | None:
    """(目前選中的伺服器名, 它有幾個分流)；讀不到／版面對不上回 None。

    ★ 純讀、微秒級，登入畫面與遊戲中都讀得到 —— 陣列邊界在主物件裡，
      不必像 [[channel-switch]] 的 `channel.count()` 那樣全記憶體掃描，
      也不必靠視窗標題（登入畫面還沒有標題）。

    ⚠ 合理性條件全部是**結構性的**（不看特定數值），所以改版增減伺服器、
      改分流數都照樣成立：兩格分流數要一致且在範圍內、port 要像 port。
      對不上就回 None，讓呼叫端退回安全的預設，而不是拿一個錯的數字用。
    """
    app = _u32(scanner, APP_PTR)
    if not app:
        return None
    beg, end = _u32(scanner, app + SRV_BEGIN), _u32(scanner, app + SRV_END)
    if not beg or not end or end <= beg:
        return None
    count = (end - beg) // SRV_STRIDE
    if not (1 <= count <= MAX_SERVERS):
        return None
    idx = _u32(scanner, SERVER_INDEX)
    if idx is None or not (0 <= idx < count):
        return None
    raw = scanner._read_bytes(beg + idx * SRV_STRIDE, 0x60)
    if not raw:
        return None
    raw = bytes(raw)
    port, sub_a, _sid, sub_b = struct.unpack_from("<IIII", raw, SRV_PORT)
    if not (1024 <= port <= 65535):
        return None
    if sub_a != sub_b or not (1 <= sub_a <= MAX_SUBSET):
        return None
    name = raw[SRV_NAME:0x40].split(b"\x00")[0].decode("utf-8", "replace")
    return name, sub_a


def subset_count(scanner, game_dir: str = "", server_index: int | None = None) -> int:
    """某個伺服器有幾個分流。記憶體優先，讀不到才退回遊戲資料夾的 server.xml。

    ★ 使用者指定：「請不要用猜的，要用讀出來的」。所以兩條路都是實際去讀 ——
      記憶體那條連改版增減分流都會自動跟上；檔案那條是給「工具箱開著但遊戲
      還沒開」的情況（那時根本沒有記憶體可讀）。兩條都失敗才回 MAX_SUBSET。

    server_index: 要問哪一個伺服器（0 起算）；不給就用目前選中的那個。
    """
    got = servers(scanner, game_dir)
    if got:
        if server_index is None:
            info = server_info(scanner) if scanner is not None else None
            return info[1] if info else got[0][1]
        if 0 <= server_index < len(got):
            return got[server_index][1]
    return MAX_SUBSET


def character(scanner, slot: int) -> str | None:
    """第 slot 格（**0 起算**）的角色名；那一格沒角色／不能進就回 None。

    ⚠⚠ 這道檢查**不是裝飾**：遊戲自己的「進入遊戲」按鈕（`0x5103E8`）送包前
      就是先問這兩個欄位，不合格根本不送。我們少了它，使用者選到空的角色格
      時會把不存在的格號送給伺服器 —— **伺服器直接把連線切斷**（實際發生過）。
    """
    if not (0 <= slot < MAX_SLOTS) or not CHAR_HAS:
        return None
    base = CHAR_HAS + slot * CHAR_STRIDE
    has = _u32(scanner, base)
    flag = _u32(scanner, base + CHAR_FLAG_OFF)
    if not has or flag is None or (flag & CHAR_BLOCKED):
        return None
    raw = scanner._read_bytes(base + CHAR_NAME_OFF, 0x20)
    if not raw:
        return None
    name = bytes(raw).split(b"\x00")[0].decode("utf-8", "replace")
    return name or f"第 {slot + 1} 個角色"


def servers(scanner, game_dir: str = "") -> list[tuple[str, int]]:
    """全部伺服器 [(名稱, 分流數), …]，照遊戲陣列的順序（索引就是選伺服器要用的）。

    記憶體優先；遊戲沒開就退回遊戲資料夾的 server.xml。兩條都失敗回空清單。
    """
    out: list[tuple[str, int]] = []
    if scanner is not None:
        app = _u32(scanner, APP_PTR)
        beg = _u32(scanner, app + SRV_BEGIN) if app else None
        end = _u32(scanner, app + SRV_END) if app else None
        if beg and end and end > beg:
            count = (end - beg) // SRV_STRIDE
            if 1 <= count <= MAX_SERVERS:
                for i in range(count):
                    got = _read_server(scanner, beg + i * SRV_STRIDE)
                    if got is None:
                        out = []
                        break
                    out.append(got)
    if not out and game_dir:
        out = _servers_from_xml(os.path.join(game_dir, "server.xml"))
    return out


def _read_server(scanner, rec: int) -> tuple[str, int] | None:
    """一筆伺服器記錄；不像伺服器記錄就回 None（條件全是結構性的）。"""
    raw = scanner._read_bytes(rec, 0x60)
    if not raw:
        return None
    raw = bytes(raw)
    port, sub_a, _sid, sub_b = struct.unpack_from("<IIII", raw, SRV_PORT)
    if not (1024 <= port <= 65535):
        return None
    if sub_a != sub_b or not (1 <= sub_a <= MAX_SUBSET):
        return None
    name = raw[SRV_NAME:0x40].split(b"\x00")[0].decode("utf-8", "replace")
    return (name or f"伺服器 {_sid}", sub_a)


def _servers_from_xml(path: str) -> list[tuple[str, int]]:
    """server.xml 的退路。

    ⚠ 檔案裡 `<伺服器 名稱="3" … 分流="5">` 的「名稱」是**編號**，真正的字要去
      `<名稱 編號="3" 繁="邱比特(NEW)">` 那幾行查。順序也照檔案裡的順序 ——
      實機比對過，跟記憶體陣列的順序一致（邱比特、維納斯、雅典娜）。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return []
    names = {n: t for n, t in re.findall(
        r'<名稱\s+編號="(\d+)"\s+繁="([^"]*)"', raw)}
    out = []
    for sid, subset in re.findall(r'<伺服器\s+名稱="(\d+)"[^>]*?分流="(\d+)"', raw):
        n = int(subset)
        if 1 <= n <= MAX_SUBSET:
            out.append((names.get(sid, f"伺服器 {sid}"), n))
    return out


def pick_server(mover, scanner, index: int) -> str:
    """在登入畫面把伺服器選成第 index 個（**0 起算**，就是 servers() 的索引）。

    成功回空字串，失敗回原因。已經選在那個伺服器就直接回成功（不重寫）。

    做法是改伺服器清單控制項的「哪一項被選中」——遊戲的登入動作就是去問這個
    （`0x624F9D`：掃項目向量，回傳第一個「+6 非 0」的索引）。所以：

        取控制項 → 讀項目向量 → 目標那項 +6 寫 1、其餘寫 0 → **讀回來重算一次**

    ⚠⚠ 最後那道讀回來不是多餘的：選錯伺服器會登進完全不同的世界。
      驗不過就回錯誤、讓呼叫端停下來，**絕不**「寫了就當成功」。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    if not GETWIDGET_FN:
        return "取控制項的函式定位失敗（遊戲可能改版了）。"
    obj = find_screen(scanner)
    if obj is None:
        return "現在不在登入畫面。"
    mgr, win_id = _u32(scanner, obj + OBJ_UI_MGR), _u32(scanner, obj + OBJ_WIN_ID)
    if not mgr or not win_id:
        return "讀不到登入畫面的介面資料。"
    with mover.lock:
        widget = mover.call_sync(GETWIDGET_FN, win_id, SERVER_LIST_ID,
                                 ecx=mgr, timeout=CALL_TIMEOUT)
    if not widget:
        return "找不到伺服器清單（遊戲可能改版了）。"
    items = _list_items(scanner, widget)
    if not items:
        return "伺服器清單是空的。"
    if not (0 <= index < len(items)):
        return f"伺服器清單只有 {len(items)} 個，選不到第 {index + 1} 個。"
    if _selected_index(scanner, items) == index:
        return ""                       # 已經是它了，不必動
    for i, item in enumerate(items):
        if not mover.write(item + ITEM_SELECTED, bytes([1 if i == index else 0])):
            return "寫入伺服器選取狀態失敗。"
    got = _selected_index(scanner, items)
    if got != index:
        return f"伺服器沒切過去（想選第 {index + 1} 個，讀回來是 {got}）。"
    return ""


def _list_items(scanner, widget: int) -> list[int]:
    """清單控制項的項目物件位址；讀不到或數量不合理回空清單。"""
    beg = _u32(scanner, widget + ITEM_VEC_BEGIN)
    end = _u32(scanner, widget + ITEM_VEC_END)
    if not beg or not end or end < beg:
        return []
    n = (end - beg) // 4
    if not (1 <= n <= MAX_SERVERS):
        return []
    out = []
    for i in range(n):
        p = _u32(scanner, beg + i * 4)
        if not p:
            return []
        out.append(p)
    return out


def _selected_index(scanner, items: list[int]) -> int:
    """照遊戲自己的算法：第一個「+6 非 0」的索引；都沒有回 -1。"""
    for i, item in enumerate(items):
        raw = scanner._read_bytes(item + ITEM_SELECTED, 1)
        if raw and bytes(raw)[0]:
            return i
    return -1


def skip_eula(scanner) -> bool:
    """把「授權合約已同意」旗標寫起來，讓遊戲不要跳授權合約視窗。

    ★ 自己開 angel.dat（不經過登入機）時會多跳一次授權合約，而遊戲**不吃背景
      滑鼠點擊**，跳出來就沒辦法自己按掉。所以要趕在它檢查之前寫進去 ——
      實測遊戲啟動後約 13 秒才會檢查，第 1 秒就寫得進去，非常寬裕。

    ⚠ 只有一個 byte，而且遊戲自己看完合約也是寫同一個值，重複寫沒有副作用。
    """
    if not EULA_OK:
        return False
    try:
        scanner.write_value(EULA_OK, VALUE_TYPES["int8"], 1)
    except Exception:                                      # noqa: BLE001
        return False
    return True


def launch(game_dir: str):
    """自己把遊戲叫起來，**不經過登入機**。回傳 Popen 物件。

    ★ 登入機（start.exe）做完更新檢查之後，也只是用 `./angel.dat` 這個命令列
      把遊戲叫起來而已（五台執行中的分身命令列一模一樣）。而登入機那排
      START／EXIT 按鈕是它自己畫的、沒有視窗控制代碼，**背景點擊完全無效**
      （實測），要按到只能佔用使用者的實體滑鼠 —— 那是使用者明令禁止的。
      所以這裡直接開遊戲，命令列也照抄成 `./angel.dat`。
    ⚠ 代價：不會跑登入機的版本更新檢查。官方改版時要自己開一次登入機更新。
    """
    exe = os.path.join(game_dir, GAME_EXE)
    return subprocess.Popen(f"./{GAME_EXE}", executable=exe, cwd=game_dir)


def read_channel(scanner) -> int | None:
    """目前的頻道（**1 起算**，給介面顯示用）；讀不到回 None。"""
    raw = scanner._read_bytes(CHANNEL, 1)
    if not raw:
        return None
    got = bytes(raw)[0] + 1
    return got if 1 <= got <= MAX_SUBSET else None


def find_screen(scanner) -> int | None:
    """登入畫面物件的位址；不在登入畫面（已進遊戲）就回 None。

    ★ 用 vtable 掃：主 vtable 在 +0，另外四個副 vtable 在固定偏移 ——
      五個指標同時對上才算數，誤判機率可以忽略。掃描約 0.6 秒。

    ⚠ VT_LOGIN 定位失敗時 locate 會保留寫死的值。那種情況下這裡多半掃不到，
      回 None（功能停用）而不是掃到別的東西。
    """
    if not VT_LOGIN:
        return None
    pat = struct.pack("<I", VT_LOGIN)
    # 副 vtable 的期望值從「當下的 VT_LOGIN」推：locate 重新定位後自動跟上。
    subs = {off: VT_LOGIN + d for off, d in SUB_VTABLE_DELTAS.items()}
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)
        i = raw.find(pat)
        while i >= 0:
            if all(i + off + 4 <= len(raw)
                   and struct.unpack_from("<I", raw, i + off)[0] == vt
                   for off, vt in subs.items()):
                return base + i
            i = raw.find(pat, i + 1)
    return None


def sign_in(mover, scanner, account: str, password: str,
            server_index: int | None = None) -> str:
    """帳密登入。成功回空字串，失敗回一句「為什麼不行」。

    mover:   已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）
    scanner: 同一個分身的 MemoryScanner（只用來掃登入畫面物件）

    ⚠ 前提是遊戲停在**登入畫面**、而且伺服器清單已經有選取（預設就選好第一個）。
      沒選的話 `0x538959` 自己會直接 return，我們這邊看起來就是「按了沒反應」。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    if not account or not password:
        return "請先填帳號與密碼。"
    obj = find_screen(scanner)
    if obj is None:
        return "找不到登入畫面 —— 這台分身可能已經進遊戲了。"
    # ⚠ 切伺服器一定要在登入動作**之前**：登入動作當場去問伺服器清單選了哪一個，
    #   選錯就會登進完全不同的世界。切不過去就整個停下來，不要硬登。
    if server_index is not None:
        err = pick_server(mover, scanner, server_index)
        if err:
            return f"切伺服器失敗：{err}"

    if not mover.write(ACCOUNT, _pad(account, ACCOUNT_LEN)):
        return "寫入帳號失敗。"
    if not mover.write(PASSWORD, _pad(password, PASSWORD_LEN)):
        return "寫入密碼失敗。"
    # ⚠ 順序有意義：先把 token 那條路關掉，再舉「跳過讀 UI」的旗標。
    #   反過來的話，中間那一瞬間兩支旗標都舉著 → tick 會挑 token。
    mover.write(FLAG_TOKEN, b"\x00")
    mover.write(FLAG_BLOB, b"\x01")
    try:
        ok = mover.call_sync(LOGIN_FN, ecx=obj, timeout=CALL_TIMEOUT) is not None
    finally:
        # ⚠⚠ 一定要放下，而且要在 finally 裡 —— 留著的話 tick 會拿
        #   0x890D30（空的 512-byte 憑證）去登入，帳密白寫。
        mover.write(FLAG_BLOB, b"\x00")
    if not ok:
        return "指令槽排不進去或逾時（遊戲主執行緒正忙），等一下再按。"
    return ""


def pick_channel(mover, scanner, channel: int) -> str:
    """在「請選擇分流遊戲世界」那一頁確定分流，把畫面推進到選角色。

    channel: 頻道／分流，**1 起算**。

    ⚠⚠ **這一步不能省**（2026-08-07 實機確認）：點分流清單那一列其實做**兩件事**
      —— 寫頻道位元組，以及叫 `0x537353` 跟伺服器要角色清單。只寫位元組的話
      畫面會一直停在分流清單，接著送「進入遊戲」就卡在「與伺服器連線中」。

    實測：寫 `[0x890810]=2` 再 `0x537353(登入畫面物件)` → 1.5 秒後畫面就到
    選角色（截圖確認），登入畫面物件隨即被釋放（`find_screen()` 回 None）。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    err = _check_channel(scanner, channel)
    if err:
        return err
    obj = find_screen(scanner)
    if obj is None:
        return "現在不在分流選擇畫面（可能已經到選角色了，直接按「進入遊戲」）。"
    # ⚠ 順序照使用者點那一列時的兩個動作：先寫頻道，再確定。
    if not mover.write(CHANNEL, bytes([channel - 1])):
        return "寫入頻道失敗。"
    if mover.call_sync(PICK_CHANNEL_FN, ecx=obj, timeout=CALL_TIMEOUT) is None:
        return "指令槽排不進去或逾時（遊戲主執行緒正忙），等一下再按。"
    return ""


def _check_channel(scanner, channel: int) -> str:
    """頻道合理性；沒問題回空字串。"""
    if not (1 <= channel <= MAX_SUBSET):
        return f"頻道要在 1~{MAX_SUBSET} 之間。"
    # ⚠ 上限**照這台伺服器實際的分流數**再檢一次；讀不到就只用上面那道
    #   （寧可放行也不要因為讀不到就整個功能不能用）。
    info = server_info(scanner)
    if info is not None and channel > info[1]:
        return f"{info[0]} 只有 {info[1]} 個分流，選不到第 {channel} 個。"
    return ""


def enter_game(mover, scanner, slot: int, channel: int,
               protect: str = "") -> str:
    """選頻道 + 保護密碼 + 進入遊戲，一包送出。成功回空字串，失敗回原因。

    slot:    角色格號，**0 起算**（畫面上第 1 個角色 = 0）
    channel: 頻道／分流，**1 起算**（畫面上的「雅典娜-3」就填 3）
    protect: 保護密碼**明文**；沒設定就留空

    ⚠ 頻道與保護密碼都不是另一包 —— 都是這一包裡的欄位（見檔頭）。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    if slot < 0:
        return "角色格號不對。"
    err = _check_channel(scanner, channel)
    if err:
        return err
    if not _u32(scanner, CONN_ID):
        return "還沒連上登入伺服器 —— 請先「帳密登入」。"
    # ⚠⚠ 這道**一定要在送包之前**：送一個沒有角色的格號給伺服器，
    #   伺服器會直接把連線切斷（使用者實際踩到）。遊戲自己的按鈕也是先問這個。
    if character(scanner, slot) is None:
        return (f"第 {slot + 1} 格沒有角色（或還不能進）——"
                "角色清單可能還沒送到，等一下再試。")
    # ⚠⚠ 只寫 **1 個 byte**：0x890811 是別的東西（實測五台都是 1），
    #   寫 4 bytes 會把它一起清掉。
    if not mover.write(CHANNEL, bytes([channel - 1])):
        return "寫入頻道失敗。"
    # ⚠ 沒填保護密碼就**完全不動**這個欄位 —— 沒設保護密碼的帳號，遊戲根本不會
    #   跳那個輸入框、也不會寫這裡，寫個 MD5("") 進去反而是我們自己發明的值。
    if protect and not mover.write(PROTECT_HASH, protect_hash(protect)):
        return "寫入保護密碼失敗。"
    # ⚠ 先寫全域再送包，跟遊戲自己的呼叫端（0x51041F）同一個順序：
    #   下一包「進遊戲伺服器」是讀這個全域，不是讀封包。
    if not mover.write(CHAR_SLOT, struct.pack("<I", slot)):
        return "寫入角色格號失敗。"
    with mover.lock:
        if mover.call_sync(ENTER_FN, slot, timeout=ENTER_TIMEOUT) is None:
            return "指令槽排不進去或逾時，等一下再按。"
    return ""
