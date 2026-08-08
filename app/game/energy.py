"""能量晶化：呼叫遊戲自己的泛用送包函式。

    energy.roll(mover)      # 按一次「能量晶化」

原理跟 `channel.py` 一樣 —— 同一個泛用送包函式，只差種類碼：

    0x5D3D97(0x0C, 目標實體ID)    ← 選定怪物（attack.select）
    0x5D3D97(0x47, 分流編號)      ← 切換分流（channel.switch）
    0x5D3D97(0x38, 1)             ← 能量晶化      ★
    0x5D3D97(0x39, -1)            ← 我要晶能加倍  ★
    0x5D3D97(0x3F, 0)             ← 開晶能視窗＝跟伺服器要晶化資料  ★
    0x5D3D97(0x37, 背包格號)      ← 拆解（把充能小背包分解成晶能）★

`0x5D3D97` 就是 `attack.SELECT_FN`（會被 `locate.warm()` 自動重新定位）。
cdecl、兩個整數、**沒有 this**，加解密與送出都由客戶端自己做。

## 怎麼找到的

使用者提供按下遊戲裡「能量晶化」按鈕時的封包擷取，呼叫鏈：

    0x5D3DC6　參數 (1, …)
    0x589B65　參數 (0x38, 1, …)      ← 這一層才是重點

反組譯 `0x589B60` 那道 `call` → 打到 `0x5D3D97`，`push ebx; push edi`
＝參數 `(0x38, 1)`。**看呼叫鏈上的參數，不要看封包內容**（內文是加密的）。
加倍那一包同樣位置是 `(0x39, 0xFFFFFFFF)`。

`0x589B27` 那個處理常式是**通用的 UI 送包指令**：從指令參數取出第 1、2 個
token 當代碼與參數再送。所以 `(0x38, 1)` 是視窗定義帶進去的常數。

遊戲裡的按鈕定義（`SETTING/BASE/WND04.XML`）：

    id=24653 「能量晶化」    <OnCommand>OnClickEnergyRoll</OnCommand>
    id=24654 「我要晶能加倍」<OnCommand>OnClickEnergyDouble</OnCommand>
    id=24655 「存入晶能」    <OnCommand>OnClickEnergySave</OnCommand>

提示文字：「每 1 點能量可進行 1 次能量晶化。按下能量晶化，隨機選取屬性。」

## ⚠⚠ 「代碼猜得到、參數猜不到」—— 這一題差點又栽進去

加倍的代碼確實是 `0x38` 的下一個 `0x39`，看起來很好猜。**但第二個參數是 `-1`
不是 `1`。** 如果照著晶化的樣子推成 `(0x39, 1)` 就會送出一個意思不同的指令。
所以還是等使用者擷取了才動手 —— 結果證明是對的。

✅ 「存入晶能」（`OnClickEnergySave`）**不需要做**（使用者確認）——
  按下一次「能量晶化」時，上一次抽到的點數就會自動記進去了，
  這也跟實測相符：每按一次晶化，`+0xC4 + 前一次索引*4` 那一格就 +10。
  ⚠ 所以真的要做的話也不要填 `0x3A`（不要因為前兩個是 0x38/0x39 就推）。

⚠ 這兩支都只是「按一下那顆按鈕」。晶化要花能量、結果隨機，**要不要按、按幾次
  是使用者的決定**，所以這裡不做任何自動連按。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import attack

# --- 狀態物件裡的能量晶化欄位（用 entity.locate_state() 拿基準） ---------
#
# 使用者給了兩台的絕對位址（嵐狐 0x32C990D8、黑狐 0x32BFC0D8），兩台都等於
# 狀態物件 +0xB8 → 所以是固定偏移，不必寫死位址。
#
#     +0xB8  能量（還能按幾次晶化）
#     +0xBC  目前選中／剛抽到的屬性索引（0~11；0xFFFFFFFF = 還沒抽）
#     +0xC0  每次獲得的點數（實測固定 10）
#     +0xC4  各屬性累積點數，12 格 int32，順序同 ATTR_NAMES
#
# ★ 索引與格子的對應是**實測驗出來的**（嵐狐按 3 次）：加分的格子永遠是
#   「按之前 +0xBC 的值」對應的那一格 —— 3→+0xD0、6→+0xDC、5→+0xD8，
#   三次都等於 +0xC4 + 索引*4。
#   也就是說：按下晶化 = 把 +10 記進「原本顯示的那個屬性」，再隨機選一個新的
#   放進 +0xBC。所以**「這次抽到什麼」要讀按下去之後的 +0xBC**。
OFF_ENERGY = 0xB8
OFF_RESULT = 0xBC
OFF_PER_ROLL = 0xC0
OFF_POINTS = 0xC4
ATTR_COUNT = 12

# 屬性名稱。**優先從記憶體讀**（見 attr_names()），這份只是讀不到時的後備。
# 順序是從記憶體裡那一排連續字串抄的，跟遊戲畫面上的排列一致。
FALLBACK_NAMES = ("最大HP", "最大MP", "攻擊力", "魔攻", "防禦力", "魔防",
                  "精準", "靈敏", "移動速度", "最大負重", "採集速度",
                  "製作速度")


@dataclass(frozen=True)
class EnergyState:
    """一次讀齊的能量晶化狀態。"""

    energy: int                 # 還能按幾次
    result: int | None          # 目前選中的屬性索引；None = 還沒抽
    per_roll: int               # 每次獲得幾點
    points: tuple[int, ...]     # 各屬性累積點數，順序同 ATTR_NAMES

    def result_name(self, names=FALLBACK_NAMES) -> str:
        if self.result is None or not (0 <= self.result < len(names)):
            return "—"
        return names[self.result]


def read(scanner, state: int) -> EnergyState | None:
    """從狀態物件讀出能量晶化的所有欄位；讀不到回 None。"""
    if not state:
        return None
    raw = scanner._read_bytes(state + OFF_ENERGY,
                              (OFF_POINTS - OFF_ENERGY) + ATTR_COUNT * 4)
    if not raw:
        return None
    b = bytes(raw)
    energy, result, per = struct.unpack_from("<III", b, 0)
    pts = struct.unpack_from(f"<{ATTR_COUNT}I", b, OFF_POINTS - OFF_ENERGY)
    return EnergyState(
        energy=energy,
        result=None if result >= ATTR_COUNT else result,
        per_roll=per,
        points=pts,
    )


_names_cache: tuple[str, ...] | None = None


def attr_names(scanner) -> tuple[str, ...]:
    """從記憶體讀那 12 個屬性名（客戶端自己載進去的），讀不到才用後備清單。

    ★ 12 個名字在記憶體裡是**一排連續的 null 結尾字串**，順序就是畫面上的
      排列。用開頭幾個名字當錨點找到那一排，再往下切 12 個。
      這樣改版換字會自動跟上（見 memory 的 prefer-memory-over-files）。

    ⚠ **錨點一定要夠長**（踩過）：只用前兩個名字 `最大HP\\0最大MP\\0` 會撞到
      另一張道具說明字串表，抓回來變成「(永久)／增加／減少／每%d秒…」。
      現在用前四個當錨點，並且**驗證**切出來的 12 個都不含格式符 `%`。
      驗不過就用後備清單 —— 顯示錯的名字比顯示寫死的名字糟。
    """
    # ★ 掃一遍全記憶體要 ~40ms，而這些字串一個 session 不會變（也不會因為
    #   換分身而不同 —— 五台實測完全一樣），所以只做一次。
    global _names_cache
    if _names_cache is not None:
        return _names_cache

    anchor = b"".join(n.encode("utf-8") + b"\x00" for n in FALLBACK_NAMES[:4])
    try:
        for base, size in scanner._iter_regions(writable_only=False):
            if base + size > 0xFFFFFFFF:
                continue
            raw = scanner._read_region(base, size)
            if not raw:
                continue
            # ⚠ 只轉一次（以前寫了兩次 bytes(raw)，那時對 bytes 是免費的，
            #   現在 _read_region 回 memoryview，每次都是真的整段複製）。
            raw = bytes(raw)
            i = raw.find(anchor)
            if i < 0:
                continue
            blob = raw[i:i + 0x200].split(b"\x00")
            out = []
            for part in blob:
                if not part:
                    break
                try:
                    out.append(part.decode("utf-8"))
                except UnicodeDecodeError:
                    break
                if len(out) == ATTR_COUNT:
                    if _names_ok(out):
                        _names_cache = tuple(out)
                        return _names_cache
                    break                      # 抓到別張表了，換下一處
    except Exception:                          # noqa: BLE001
        pass
    # ⚠ 讀不到時**不要快取**後備清單 —— 可能只是這一刻讀不到（剛啟動、
    #   還在載入），下次應該再試一次讀真的。
    return FALLBACK_NAMES


def _names_ok(names) -> bool:
    """切出來的名字看起來像不像屬性名（不是道具說明那種帶格式符的句子）。"""
    return (len(names) == ATTR_COUNT
            and len(set(names)) == ATTR_COUNT
            and all(n and len(n) <= 8 and "%" not in n for n in names))

# 泛用送包函式的種類碼與參數。函式本身沿用 attack.SELECT_FN。
ROLL_CODE = 0x38
ROLL_ARG = 1
DOUBLE_CODE = 0x39
# ⚠ 是 -1（擷取到的是 0xFFFFFFFF），**不是 1**。跳板寫入時會 & 0xFFFFFFFF，
#   所以這裡放 Python 的 -1 就好。
DOUBLE_ARG = -1
# ★「開晶能視窗」的請求包（2026-08-08 使用者擷取，0x589B65 那層 = (0x3F, 0)）。
#   晶化資料是「用到才同步」：剛上線時伺服器不會推，狀態物件 +0xB8 那段全 0，
#   遊戲裡打開晶能視窗那一刻客戶端才送這包去要，回包後欄位才有數字。
#   跟兌換的 0x49 開商店同一種設計（見 exchange.open_shop）。
#   ⚠ 送這包之後客戶端可能會自己把晶能視窗開出來（兌換那邊就是這樣），
#     使用者關掉即可，資料已經寫進記憶體不受影響。
SYNC_CODE = 0x3F
SYNC_ARG = 0
# ★「拆解」＝把充能小背包分解成晶能（2026-08-08，兩條證據互相印證）：
#   1. 使用者擷取：0x589B65 那層 = (0x37, 0x32) —— 0x32 是當時那顆的格號。
#   2. OnClickDecomp 的 Lua bytecode：
#        game.netcommand(55, game.finditemslot(gPDCompData.item))
#      55 = 0x37，第二個參數就是**背包格號**（跟 bag.Item.slot 同一套索引，
#      getcharitem/0x508B9A 都是取實體+0x2FC 那個 vector 的第 n 格）。
#   遊戲自己的檢查（OnSetPDItem）：gettype()==ITEMOBJ_TYPE_DOLL、
#   getparam2()>0（分解值）、沒時限、不在穿戴格 —— 我們用更嚴的白名單。
DECOMP_CODE = 0x37
# ⚠⚠ 只准分解這兩種（使用者明令「只能分解這兩個，不要分解錯東西」）。
#   鍵＝種類 ID（itemname 同一套），值＝分解值（拆一顆拿到幾點晶能，
#   寫在物品說明「可以提供 N 點分解值」）。
#   87381「充能-小背包(10)(活動)」**刻意不在名單** —— 使用者只點名這兩個。
DECOMP_ITEMS = {
    87138: 30,      # 充能-小背包(30)
    87139: 20,      # 充能-小背包(20)
}


def _send(mover, code: int, arg: int) -> bool:
    if not (mover and mover.active):
        return False
    return attack._send(mover, ((attack.SELECT_FN, (code, arg)),))


def roll(mover) -> bool:
    """送一次「能量晶化」。排不進指令槽時回 False。

    mover: 已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）。
    """
    return _send(mover, ROLL_CODE, ROLL_ARG)


def double(mover) -> bool:
    """送一次「我要晶能加倍」。排不進指令槽時回 False。"""
    return _send(mover, DOUBLE_CODE, DOUBLE_ARG)


def sync(mover) -> bool:
    """跟伺服器要一次晶化資料（＝遊戲裡打開晶能視窗送的那包）。

    送出後伺服器要一點時間回包，資料不會立刻出現 —— 讀取端照常輪詢即可。
    """
    return _send(mover, SYNC_CODE, SYNC_ARG)


def decompose(mover, scanner, slot: int) -> tuple[bool, str]:
    """拆解背包第 `slot` 格的充能小背包（＝遊戲「拆解」鈕）。回 (送出了嗎, 原因)。

    ⚠⚠ 送包前**當場重讀那一格**，不在 DECOMP_ITEMS 白名單裡就拒送 ——
      格號是照先前讀到的背包挑的，背包若在這中間變動（撿東西、手動整理），
      同一個格號可能已經換成別的東西。把關放在最底層，呼叫端想錯用也錯不了。
      （鐵則出處見 memory 的 game-crash-root-causes：交給遊戲的東西當場重驗。）
    """
    from app.game import bag
    got = bag.items(scanner, slot, slot)
    if not got:
        return False, "那一格是空的（已經拆掉了？）"
    if got[0].type_id not in DECOMP_ITEMS:
        return False, f"那一格不是充能小背包（{got[0].name}），拒送"
    if not _send(mover, DECOMP_CODE, int(slot)):
        return False, "指令槽忙碌"
    return True, ""
