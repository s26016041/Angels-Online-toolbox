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

遊戲裡的按鈕定義（`GAMEDATA/setting/BASE/WND04.XML`）：

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

from app.game import attack, entity

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
# ⚠⚠⚠ **2026-08-11 改版整組搬家：0xB8 → 0x54（−0x64）。**
#   四個欄位是**連在一起的**（能量／抽到的屬性／每次點數／12 格點數），
#   所以只留一個起點，其餘用 +4 推 —— 同一個位址不准記在兩個地方。
#
# ⚠ 這一組**沒有 AOB 錨可用**（晶化介面整個是 Lua 寫的，C++ 那邊只有封包
#   處理常式會碰這些欄位，找不到穩定的指令骨架），所以它不能像
#   `entity.OFF_TARGET`／`team.MEMBERS_OFF` 那樣自動跟上。
#   補償措施有兩道，**改版後它會大聲壞掉、不會安靜算錯**：
#     ① 讀取端逐欄驗證（見 read()）：抽到的屬性只准 −1 或 0~11、每次點數
#        要是 10 的倍數（平常 10、加倍成功 20）、能量與點數不准是天文數字
#        —— 驗不過一律回 None。
#     ② `tools/verify_offsets.py` 會對真遊戲把這四欄再驗一次（改版後跑
#        `/_patchCheck` 就會看到）。
#
# 值是怎麼定下來的（2026-08-11，五台交叉比對，不是推的）：
#   送一發同步請求 0x3F，看**哪一格會變**：北極狐 +0x54 由 0 → 10。
#   再比五台：+0x54 因人而異（10/10/0/2/2395）＝能量；+0x58 是 −1（還沒抽）
#   或 6/10（0~11 的屬性索引）；+0x5C 有資料的都是 10 ＝每次點數；
#   +0x60 起 12 格是各屬性累積點數（嵐狐 11042/17820/2182/74/…）。
OFF_ENERGY = 0x54
OFF_RESULT = OFF_ENERGY + 4       # 目前選中／剛抽到的屬性索引（0~11；−1 = 還沒抽）
OFF_PER_ROLL = OFF_ENERGY + 8     # 每次獲得的點數（平常 10；加倍成功後 20）
OFF_POINTS = OFF_ENERGY + 12      # 各屬性累積點數，12 格 int32
ATTR_COUNT = 12
# 合理性上限（見上面②）。能量與單一屬性點數都不可能到千萬。
MAX_ENERGY = 10_000_000
MAX_POINTS = 100_000_000
# 每次點數的合理上限。實測值只有 0（還沒同步）、10（平常）、20（加倍成功，
# 2026-08-18 嵐狐實機讀到）。上限放寬到 200 是**版面驗證的容忍度**——
# 就算遊戲哪天再疊倍也不會把合法狀態誤判成「欄位搬家」；不是說真會到 200。
MAX_PER_ROLL = 200

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
    """從狀態物件讀出能量晶化的所有欄位；讀不到／位址已失效回 None。

    ⚠⚠⚠ **從物件開頭一起讀，順便比對 vtable —— 這道驗證不能省。**
      狀態物件會搬家（換地圖、死亡重生、斷線重連、傳送），而搬家之後舊位址
      那塊記憶體**照樣讀得到**（遊戲拿去放別的東西了）。以前這裡只檢查
      `if not state`，所以位址一過期就會安靜地把別人的堆積當成能量欄位讀出來：
      畫面上的能量、抽到的屬性、12 格點數全是垃圾值，而且因為「讀到了」，
      呼叫端永遠不會判定要重新定位 —— 錯下去不會自己好。
      （CLAUDE.md 的鐵則：讀取端一律做合理性驗證，驗不過就退安全預設。）
    ★ vtable 在 +0、能量在 OFF_ENERGY，一次整塊讀回來就同時拿到，等於免費；
      而且所有欄位是**同一瞬間**的快照。vtable 值本身由 locate.warm() 依 AOB
      重新定位，改版也跟得上。
    ★ 回 None 的意思統一是「這份資料不可信」，呼叫端照原本的路重新定位
      （energy_tab 的 `_read()` 就會 forget_state → 下一輪重找）。

    ⚠⚠ **這四欄的偏移沒有 AOB 可以自動跟上**（晶化介面整個是 Lua 寫的，
      C++ 那邊找不到穩定的指令骨架當錨），所以底下**逐欄驗版面**：
      2026-08-11 那次改版它們整組搬了 −0x64，而舊偏移**照樣讀得到**
      （讀到的是別的成員）—— 沒有這道驗證就會安靜地顯示垃圾數字。
    """
    if not state:
        return None
    raw = scanner._read_bytes(state, OFF_POINTS + ATTR_COUNT * 4)
    if not raw:
        return None
    b = bytes(raw)
    if struct.unpack_from("<I", b, 0)[0] != entity.VT_STATE:
        return None                       # 位址過期了 —— 不准拿垃圾值當答案
    energy, result, per = struct.unpack_from("<Iii", b, OFF_ENERGY)
    pts = struct.unpack_from(f"<{ATTR_COUNT}i", b, OFF_POINTS)
    # --- 版面驗證（改版搬家就擋在這裡）---
    #   抽到的屬性：−1（還沒抽）或 0~11；每次點數：10 的倍數（0=還沒同步、
    #   10=平常、20=加倍成功）。
    #   ⚠⚠ 這欄以前寫成白名單 `per in (0, 10)`，結果 2026-08-18 嵐狐加倍
    #   成功後這欄變 20，被當成「版面搬家」整片打槍回 None —— 分頁顯示
    #   讀不到資料、自動晶化停掉、還連帶 forget_state 白白重掃。
    #   這條驗的是「這裡還是不是那個欄位」，不是業務值白名單，別再收緊。
    if not (result == -1 or 0 <= result < ATTR_COUNT):
        return None
    if not (0 <= per <= MAX_PER_ROLL and per % 10 == 0):
        return None
    if not 0 <= energy <= MAX_ENERGY:
        return None
    if any(not 0 <= p <= MAX_POINTS for p in pts):
        return None
    return EnergyState(
        energy=energy,
        result=None if result < 0 else result,
        per_roll=per,
        points=tuple(pts),
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

# ★ 泛用送包的種類碼與參數，函式沿用 attack.SELECT_FN。出處：使用者攔包
#   呼叫鏈 0x589B65 那層 (0x38, 1)（見檔頭「怎麼找到的」）。
ROLL_CODE = 0x38
# ⚠ 攔包實錄的第二參數就是 1（視窗定義帶進去的常數，不是猜的）。
ROLL_ARG = 1
# ★ 出處：同上攔包，加倍那包同一層是 (0x39, 0xFFFFFFFF)。
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

# --- 「自動分解全部」：**不寫死名單，讀遊戲自己的欄位** -----------------
#
# ⚠⚠ 這裡原本做成一份 461 筆的寫死表（從 GAMEDATA/setting 抽出來的
#   `assets/decomp_items.tsv`）。2026-08-08 把 `ItemData` 那張 Lua 綁定表
#   逐支反組譯之後發現**判斷需要的三個欄位全都在記憶體裡**，所以整份表
#   連同 `tools/build_decomp_items.py` 一起刪掉了 —— 照專案鐵則第一條，
#   遊戲已經載進記憶體的資料不准抄成寫死表（改版會安靜過期）。
#
#       getparam2()    → 範本 +0x10C   分解值　　✅五台 341/341 對帳資源包
#       gettype()      → 範本 +0x18    分類 46＝紙娃娃
#       gettimelimit() → 物品 +0x2E    時限（★使用者確認：有時限就拆不掉）
#
#   判斷整條搬進 `bag.Item.decomposable`（照抄客戶端 `OnSetPDItem`）。
#
# ★ 玩家的裝扮不會被掃到，靠的是**格號**：拆解只碰一般背包
#   （`bag.FIRST_SLOT`~`bag.LAST_SLOT` = 0x14~0xA9）。身上穿著的裝扮欄
#   （242~249，遊戲常數 DOLL_SLOT_BEGIN/END）連遊戲自己都不讓拆，
#   「紙娃娃隨身包」（251 起）更在範圍外。
#
# ⚠ 分解值**分不出**「充能小背包」跟「點裝」（87381 的 10 點跟 171 件點裝
#   一模一樣），所以「只拆小背包」那顆鈕還是走上面寫死的兩個編號白名單。
#   兩顆鈕的名單不同是刻意的。


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


def _slot_ok(item, all_items: bool) -> str:
    """這一格能不能送去拆？能就回 ""，不能就回**原因**（給人看的一句話）。

    兩種模式共用的四道關（前三道就是遊戲客戶端 `OnSetPDItem` 自己的判斷）：
      1. 格號落在**一般背包**（`bag.FIRST_SLOT`~`LAST_SLOT`）—— 身上穿的、
         裝扮欄、紙娃娃隨身包、倉庫全部擋在外面。
      2. 記憶體分類是紙娃娃（46）。
      3. 分解值 > 0。
      4. **沒有時限** —— 使用者 2026-08-08 確認「限時不能拆」。先擋下來，
         比送出去被丟掉、然後每 3 秒重送一次好。
    只拆小背包的模式再多一道：種類 ID 要在 `DECOMP_ITEMS` 白名單裡。

    ★ 第 2 道關也順便補上了 [[items-table-maintenance]] 點名的風險：萬一改版
      把 87138/87139 這兩個編號回收給別的東西用，光靠白名單擋不住，
      但那個「別的東西」幾乎不可能同時也是分解值 > 0 的紙娃娃。
    """
    from app.game import bag
    if not bag.FIRST_SLOT <= item.slot <= bag.LAST_SLOT:
        return f"格 {item.slot} 不在一般背包裡（{item.name}）"
    if not all_items and item.type_id not in DECOMP_ITEMS:
        return f"{item.name} 不是充能小背包"
    if not item.is_doll:
        return f"{item.name} 記憶體分類是 {item.kind}，不是紙娃娃"
    if item.decomp_value <= 0:
        return f"{item.name} 沒有分解值"
    if item.time_limit:
        return f"{item.name} 是限時道具，遊戲不讓拆"
    return ""


def gain_of(item, all_items: bool) -> int:
    """拆這一件會拿到幾點晶能。

    ★ 「全部」模式一律讀記憶體（範本 +0x10C）；只拆小背包的模式沿用寫死的
      白名單值，讀不到才退回記憶體 —— 那兩個編號是使用者親自指定的。
    """
    if all_items:
        return int(item.decomp_value)
    return DECOMP_ITEMS.get(item.type_id, int(item.decomp_value))


def decompose(mover, scanner, slot: int,
              all_items: bool = False) -> tuple[bool, str]:
    """拆解背包第 `slot` 格的東西（＝遊戲「拆解」鈕）。回 (送出了嗎, 原因)。

    all_items: False = 只認充能小背包白名單；True = 遊戲認可以拆的都拆。

    ⚠⚠ 送包前**當場重讀那一格**，過不了 `_slot_ok` 就拒送 ——
      格號是照先前讀到的背包挑的，背包若在這中間變動（撿東西、手動整理），
      同一個格號可能已經換成別的東西。把關放在最底層，呼叫端想錯用也錯不了。
      （鐵則出處見 memory 的 game-crash-root-causes：交給遊戲的東西當場重驗。）
    """
    from app.game import bag
    got = bag.items(scanner, slot, slot)
    if not got:
        return False, "那一格是空的（已經拆掉了？）"
    why = _slot_ok(got[0], all_items)
    if why:
        return False, why + "，拒送"
    if not _send(mover, DECOMP_CODE, int(slot)):
        return False, "指令槽忙碌"
    return True, ""


def decomposable(scanner, all_items: bool = False) -> list:
    """一般背包裡**現在可以拆**的東西（`bag.Item` 清單）。

    呼叫端（分頁）用這個列清單、算件數，跟送包時 `decompose_batch()` 的
    重驗走**同一條規則**（`_slot_ok`），不會出現「畫面說有 5 件、實際送 0 件」。
    """
    from app.game import bag
    return [it for it in bag.items(scanner) if not _slot_ok(it, all_items)]


def blocked_note(scanner, all_items: bool = False) -> str:
    """一件都沒中時，說明「有東西但被擋下來」的原因；沒事回 ""。

    ⚠ 失效模式只准兩種（大聲停用／安全退化），**不准安靜地不做事** ——
      所以「本來符合分類、卻因為時限被跳過」這種情況要講出來，不然使用者
      只會看到一個什麼都不拆的按鈕。
    """
    from app.game import bag
    limited = [it for it in bag.items(scanner)
               if it.time_limit and it.decomp_value > 0
               and (it.is_doll if all_items else
                    it.type_id in DECOMP_ITEMS)]
    if limited:
        names = "、".join(sorted({it.name for it in limited}))
        return f"⚠ 有 {len(limited)} 件是限時道具（{names}），遊戲不讓拆，已跳過"
    return ""


# 一拍最多拆幾顆。批次是連續 call_sync、整段抓著跳板的鎖（每顆正常一幀
# ~16ms、最壞 CALL_TIMEOUT 0.12s），設上限免得同一台的掛機指令被卡太久。
# 拆不完的下一拍（3 秒後）接著拆。前例：領在線獎勵 0x48 六格一次全送沒問題。
MAX_PER_TICK = 30


def decompose_batch(mover, scanner, slots,
                    all_items: bool = False) -> tuple[list[int], bool]:
    """一口氣拆解多格。回 (通過重驗、真的排送的格號, 整批都排進去了嗎)。

    all_items 同 `decompose()`。

    ⚠⚠ 跟單發一樣，送包前**重讀背包**逐格重驗 —— 沒過 `_slot_ok` 就整格
      跳過，呼叫端塞錯格號也送不出去。
    ⚠ 半路排不進去（指令槽忙碌）時**前面幾格已經送出了** —— 所以成敗
      不能看這裡的回傳，一律以「序號從背包消失」對帳。
    """
    from app.game import bag
    if not (mover and mover.active):
        return [], False
    # ★ 重讀的範圍就是一般背包（bag.items 的預設），裝扮／倉庫根本不會進來。
    fresh = {it.slot: it for it in bag.items(scanner)}
    good: list[int] = []
    for s in slots:
        it = fresh.get(int(s))
        if it is not None and not _slot_ok(it, all_items):
            good.append(int(s))
        if len(good) >= MAX_PER_TICK:
            break
    if not good:
        return [], True
    calls = tuple((attack.SELECT_FN, (DECOMP_CODE, s)) for s in good)
    return good, attack._send(mover, calls)
