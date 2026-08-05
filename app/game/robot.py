"""官方外掛「天使守護精靈」的開關與設定 —— 全部透過遊戲自己的 Lua 呼叫。

    robot.is_run(sc)                    # 天使守護精靈開著嗎
    robot.begin_supply(mover, sc)               # ① 調好開關（等 SETUP_SETTLE 秒）
    robot.do_recall(mover, sc, 背包表頭)         # ② 回程 → 補給開跑
    robot.end_supply(mover, sc)         # 收尾（只關自動攻擊）

為什麼要用它
------------
使用者要的是「掛機時裝備壞了 → 回程 → 自動修裝買水 → 回來繼續」。
這整套官方精靈本來就會做，而且是**它自己的設定頁**（回城道具、要買什麼、
修完回不回戰場）在決定 —— 我們自己重做一份只會又多一套要維護的東西。

所以分工是：**打怪是我們的掛機，補給那一趟交給精靈。**

「精靈開著沒」＝ `RUN_FLAG`（0 = 開、-1 = 關），就是 `game.setrobotisrun`
寫的那個全域。**我們只讀不寫** —— 主開關由使用者自己在遊戲裡開。

⚠ 精靈的補給設定（要買什麼、回城道具放哪一格）**要使用者自己在遊戲裡設好**，
  我們只負責在對的時機湊齊觸發條件。
"""
from __future__ import annotations

import struct
import time

from app.game import itemname, lua

# ⚠ 這個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
RUN_FLAG = 0x009CFBA4        # 0 = 精靈執行中、-1 = 停止

RUN_ON, RUN_OFF = 0, -1

# ★★ 兩段等待都是使用者實測調出來的，**不要為了「快一點」去縮**。
#   ⓐ 開關調好之後要**等一下再回程**：太快送回程，精靈那邊還沒進入狀態，
#      整趟就不會啟動。使用者回報 2~4 秒比較穩，取 3。
SETUP_SETTLE = 3.0
#   ⓑ **回程送出之後**「自動攻擊」要留一段時間才能關：
#      立刻關（0 秒）→ 只到城裡就停住，不修裝也不走回去（實測過）
#      隔 5 秒關     → 可以（使用者確認）
#   ⚠ 這 5 秒是從**回程送出**開始算，不是從調設定開始算 —— 先前一度以為
#     5 秒不夠而改成 6，其實是那時計時的起點不對（設定和回程還沒拆成兩段）。
AF_HOLD_SECS = 5.0

# 精靈的變數代號（從遊戲的 Lua 全域常數讀出來的，不是猜的）
AF_IS_AUTO_FIGHT = 1001      # AF_BOL_ISAUTOFIGHT
# ⚠ 「攻擊指定敵人」（面板控制項 974）。**開著補給就不會觸發**（使用者實測）。
AF_ATTACK_TARGET_ONLY = 1006  # AF_BOL_ISATTACKTARGET
AS_BACK_NO_HP_ITEM = 1500    # 補 HP 物品用完自動回城
AS_BACK_NO_MP_ITEM = 1501    # 補 MP 物品用完自動回城
AS_BACK_BROKEN_EQ = 1502     # ★ 裝備損壞回城
AS_BACK_NO_SPACE = 1504      # 背包滿了回城
AS_IS_REPAIR = 1508          # ★ 回城後修理裝備
# ⚠ 1509 是「使用**標記傳送捲軸**回練功點」，不是「補給完回去戰鬥」。
#   （對照 SETTING/BASE/WND01.XML 控制項 10073 的 appdata。我一開始標錯過。）
#   關著的話精靈是**用走的**走回原練功點 —— 距離太遠或走不到就會停在城裡。
AS_USE_RETURN_SCROLL = 1509
AS_IS_BUY_ITEM = 1511        # 購買物品保持身上數量（⚠ 這**不是**回城觸發條件）

# ★★ 補血／補魔藥水設在「天使輔助精靈」那一頁，用 DATAID 存（也是 robot var）。
#   每一格是一對：`DATAID` 放**型別**、`DATAID + 10` 放**值**。
#     型別 1 = 技能（用技能補，不吃藥水）　型別 2 = 道具（值就是種類 ID）
#   實測：雪狐 HP1=道具 4836、MP1=道具 4837；白狐 HP1=**技能 64**、MP2=道具 4837。
#   （`SKILLITEM_VALUE_DIFF` 這個 Lua 常數就是那個 +10。）
SKILLITEM_VALUE_DIFF = 10
SKILLITEM_TYPE_SKILL = 1
SKILLITEM_TYPE_ITEM = 2
HP_ITEM_SLOTS = (2121, 2122, 2123)   # DATAID_RECOVER_HP1~3_SKILLITEM
MP_ITEM_SLOTS = (2124, 2125)         # DATAID_RECOVER_MP1~2_SKILLITEM

# 精靈的「原練功點」—— 補給完會走回這裡。座標是格子 × 32 的原始值。
AF_ORG_MAP = 1030
AF_ORG_X = 1025
AF_ORG_Y = 1026
TILE = 32

# 「補給那一趟」需要的三項；缺任何一項就等於白開
SUPPLY_NEEDED = (
    (AS_BACK_BROKEN_EQ, "裝備損壞回城"),
    (AS_IS_REPAIR, "修理裝備"),
)


def is_run(scanner) -> bool:
    """精靈現在在跑嗎？（純讀記憶體，成本趨近於零）"""
    raw = scanner._read_bytes(RUN_FLAG, 4)
    if not raw:
        return False
    return struct.unpack("<i", bytes(raw))[0] == RUN_ON


def _wnd(mover, scanner, name: str) -> int:
    """讀某個視窗的**執行期代號**；視窗沒開就是 0。

    ⚠⚠ **這些代號是執行期才配的，不能寫死也不能快取。**
      同一台黑狐先後讀到 `WND_AUTOROBOT` = 1281171713（面板開著）與 0（關著）、
      `WND_AUTOFIGHT` = 1282892244 與 1584955875。我一度把它快取起來，那是錯的。
    """
    v = lua.get_global(mover, scanner, name)
    return int(v) if isinstance(v, (int, float)) else 0


def _res_id(mover, scanner, name: str, fallback: int) -> int:
    """控制項 id（這種才是真常數）；讀不到就用備援值。"""
    v = lua.get_global(mover, scanner, name)
    return int(v) if isinstance(v, (int, float)) and v else fallback


def set_run(mover, scanner, on: bool) -> tuple[bool, object]:
    """開／關天使守護精靈。回傳 (旗標最後是不是想要的值, 說明)。

    ⚠⚠ **光叫 `game.setrobotisrun` 不夠**：那只寫內部旗標，畫面上
      「開啟天使守護精靈」那個勾選框**不會跟著變** —— 使用者看起來就是沒開。
      那個勾選框掛在精靈面板底下，面板沒建立時 `WND_AUTOROBOT` 是 0、
      根本碰不到，所以必要時要先 `CreateRobotWindow()` 把面板開出來。
    """
    cid = _res_id(mover, scanner, "AUTOROBOT_CHECK_RES_ID", 14219)
    wnd = _wnd(mover, scanner, "WND_AUTOROBOT")
    if not wnd:
        lua.call(mover, scanner, "CreateRobotWindow")
        wnd = _wnd(mover, scanner, "WND_AUTOROBOT")
    if wnd:
        lua.call(mover, scanner, "window.setcheck", wnd, cid, bool(on))
        lua.call(mover, scanner, "OnClickOpenCloseRobot", cid)
    lua.call(mover, scanner, "game.setrobotisrun", bool(on))

    good = is_run(scanner) == bool(on)
    checked = lua.call(mover, scanner, "window.ischeck", wnd, cid)[1] if wnd \
        else None
    if good and checked is not None and checked is not bool(on):
        return False, f"旗標對了但勾選框還是 {checked}"
    return good, ("" if good else "旗標沒變成想要的值")


def get_bool(mover, scanner, var_id: int) -> tuple[bool, object]:
    """讀精靈的一個布林設定。"""
    return lua.call(mover, scanner, "game.getrobotvar_bool", var_id)


def set_bool(mover, scanner, var_id: int, value: bool) -> tuple[bool, object]:
    """改精靈的一個布林設定。

    ⚠ 平常不要用 —— 那是使用者在遊戲裡設好的東西，我們不該偷改。
      留著是為了「缺哪一項就補哪一項」這種明確的情境。
    """
    return lua.call(mover, scanner, "game.setrobotvar_bool", var_id,
                    bool(value))


def begin_supply(mover, scanner) -> list[str]:
    """把精靈調成「會跑補給」的狀態。回傳「實際做了哪些事」給人看。

    ## 使用者實測出來的條件（缺一就不會觸發）

        ① 天使守護精靈主開關要開
        ② **「攻擊指定敵人」要關**  ← 開著就不會觸發
        ③ 按「設定」把搜尋中心點設在現在的位置
        ④ 最後才開「自動攻擊」

    做完等 `SETUP_SETTLE` 秒再回程（見 `do_recall`）。

    ★ **每一項都先查再決定動不動**：已經是對的就跳過（使用者要求）。
    """
    notes: list[str] = []

    if not is_run(scanner):                                          # ①
        ok, why = set_run(mover, scanner, True)
        notes.append("開了主開關" if ok else f"⚠ 主開關開不起來（{why}）")

    ok, cur = get_bool(mover, scanner, AF_ATTACK_TARGET_ONLY)        # ②
    if ok and cur is not False:
        set_bool(mover, scanner, AF_ATTACK_TARGET_ONLY, False)
        notes.append("關了攻擊指定敵人")

    set_org_spot(mover, scanner)                                     # ③
    notes.append("設了中心點")

    ok, cur = get_bool(mover, scanner, AF_IS_AUTO_FIGHT)             # ④
    if cur is not True:
        set_autofight(mover, scanner, True)
        notes.append("開了自動攻擊")
    return notes


def end_supply(mover, scanner) -> None:
    """收尾。**只做一件事：把「自動攻擊」關掉。**

    ⚠ 其餘狀態（主開關、攻擊指定敵人、中心點）**刻意不還原** ——
      使用者明確要求「結束不用幫忙回復原狀，但自動攻擊關閉是肯定的」。
      自動攻擊是唯一非關不可的，因為它一開著精靈就會跟我們的掛機搶怪。
    """
    set_autofight(mover, scanner, False)


def get_int(mover, scanner, var_id: int) -> int | None:
    """讀精靈的一個整數設定。"""
    ok, val = lua.call(mover, scanner, "game.getrobotvar_int", var_id)
    return int(val) if ok and isinstance(val, (int, float)) else None


SLOT_CACHE_SECS = 60.0        # 藥水設定多久重讀一次（見 potion_slots）
_slot_cache: dict[int, tuple[float, dict]] = {}


def potion_slots(mover, scanner, pid: int) -> dict:
    """讀「輔助精靈」那幾格藥水設成什麼，**結果快取 60 秒**。

    ⚠⚠ **一定要快取**：每次判斷要讀 10 個設定，每個都是一次 Lua 呼叫。
      掛機每 3 秒判斷一次就是每分鐘 200 次 —— 實測連打十幾輪就會把客戶端的
      訊息迴圈弄卡死（白狐）。設定是使用者偶爾才改的東西，60 秒重讀綽綽有餘。
    ★ 背包數量**不快取** —— 那是純記憶體讀取，不經過 Lua，成本趨近於零。

    回傳 {DATAID: (型別, 值)}；任何一個讀失敗就整份不快取（下次再試）。
    """
    now = time.time()
    hit = _slot_cache.get(pid)
    if hit and now - hit[0] < SLOT_CACHE_SECS:
        return hit[1]
    out, ok = {}, True
    for base in HP_ITEM_SLOTS + MP_ITEM_SLOTS:
        kind = get_int(mover, scanner, base)
        val = get_int(mover, scanner, base + SKILLITEM_VALUE_DIFF)
        if kind is None or val is None:
            ok = False
            break
        out[base] = (kind, val)
    if not ok:
        return hit[1] if hit else {}       # 讀失敗就沿用舊的，別當成「沒設」
    _slot_cache[pid] = (now, out)
    return out


def _potion_out(slots_info: dict, scanner, inv_head: int,
                slots) -> list[int] | None:
    """這一組（紅水或藍水）的藥水**全部歸零**了嗎？是的話回傳那些種類 ID。

    ⚠⚠ **要「全部」歸零才算，不是「任何一格」**：有人紅水配兩格，第一格
      用完但第二格還有，角色明明還補得到血 —— 那時把人拉回城是錯的。
    ★ 設成「技能」的格子代表**不靠藥水補**（白狐紅水1 就是技能 64），
      這一組只要有技能格就直接當「不缺」，不觸發。
    ★ 一格都沒設、或設定讀不到，就無從判斷，也不觸發。
    """
    from app.game import inventory                    # 避免循環相依

    items = []
    for base in slots:
        kind, tid = slots_info.get(base, (None, None))
        if kind == SKILLITEM_TYPE_SKILL:
            return None                    # 有技能可補，不算缺藥水
        if kind != SKILLITEM_TYPE_ITEM or not tid:
            continue
        items.append(tid)
    if not items:
        return None                        # 一格都沒設
    # ⚠ 一定要用 count_by_type：藥水會散成好幾疊（黑狐的 4837 分在 8 格）
    if any(inventory.count_by_type(scanner, inv_head, t) > 0 for t in items):
        return None                        # 還有一格有貨就不算用完
    return items


def potions_out(mover, scanner, inv_head: int, pid: int = 0,
                hp: bool = True, mp: bool = True) -> list[tuple[str, str]]:
    """哪幾組藥水見底了 → `[('HP', 'HP藥水（高效紅藥水(活動)）'), …]`。

    ★ **HP 和 MP 完全分開**（使用者要求）：勾 HP 就只看 HP 那一組全歸零，
      MP 那組有沒有水完全不影響，反之亦然。
    ★ 拆出來是因為**通知和觸發是兩回事**：使用者要求「水沒了也要通知」，
      所以通知會兩組都看，觸發只看勾起來的那組。
    """
    if not inv_head:
        return []
    want = ([(HP_ITEM_SLOTS, "HP")] if hp else []) + \
           ([(MP_ITEM_SLOTS, "MP")] if mp else [])
    if not want:
        return []
    info = potion_slots(mover, scanner, pid)
    out = []
    for slots, what in want:
        gone = _potion_out(info, scanner, inv_head, slots)
        if gone:
            names = "、".join(itemname.label(t) for t in gone)
            out.append((what, f"{what}藥水（{names}）"))
    return out


_UNSET = object()      # 「這個參數沒給」——用來跟「給了但值是 None」區分


def supply_needed(mover, scanner, inv_head: int, on_broken: bool,
                  on_hp: bool, on_mp: bool, pid: int = 0,
                  dura=_UNSET, dry=None) -> str | None:
    """**該回去補給了嗎？** 是的話回傳原因（可直接顯示），否則 None。

    dura / dry: 呼叫端**同一拍剛算過**的耐久與見底清單，給了就不重算。
        ⚠ 兩者都要是「這一拍」的值，不能是上一輪留下來的。
        掛機的裝備檢查本來就會先算耐久、再叫 `potions_out()` 通知，
        接著這裡又各算一次 —— 走一趟物品陣列要上百次記憶體讀取，
        而且是在 GUI 執行緒上，五台同時做就是看得見的頓一下。
        `dry` 是**兩組都算過**的完整清單，這裡只挑有勾的那幾組
        （`_potion_out` 各組獨立計算，挑出來跟重算完全一樣）。

    ⚠⚠ **要不要判斷由呼叫端的三個開關決定，不看遊戲裡精靈的回城勾選**
      （使用者要求：「不要管他遊戲裡面有沒有設定」）。這樣我們的觸發跟
      官方的觸發互相獨立，不會因為他在遊戲裡沒勾就變成不動。

    · `on_broken` → 武器耐久 0
    · `on_hp` → **HP 藥水那一組**全部歸零（見 `_potion_out`）
    · `on_mp` → **MP 藥水那一組**全部歸零
      ★ 兩組各判各的：勾 HP 就只看 HP 那三格，MP 有沒有水完全不影響。

    藥水是哪一種還是讀「天使輔助精靈」那頁設的（見 `HP_ITEM_SLOTS`），
    所以換藥水會自動跟著換，不必寫死。

    ⛔ 採買清單**不是**觸發條件：那是「進城順便補到幾個」，不是「該回城了」。
    ⛔ 還沒支援背包滿 —— `game.getbagsize()` 回 30，但物品陣列裡有 60 幾件，
      對不起來，還沒確定背包格的範圍。
    """
    from app.game import inventory                    # 避免循環相依

    if not inv_head:
        return None

    if on_broken:
        d = (inventory.durability(scanner, inv_head) if dura is _UNSET
             else dura)
        if d is not None and d[0] <= 0:
            return "武器損壞"

    if dry is None:
        dry = potions_out(mover, scanner, inv_head, pid, on_hp, on_mp)
    else:
        want = ({"HP"} if on_hp else set()) | ({"MP"} if on_mp else set())
        dry = [(w, text) for w, text in dry if w in want]
    if dry:
        return "、".join(d for _, d in dry) + "用完了"
    return None


def has_recall_item(scanner, inv_head: int) -> tuple[int, int] | None:
    """背包裡的回程道具 (格號, 剩幾個)；沒有回 None。"""
    from app.game import inventory, recall            # 避免循環相依

    if not inv_head:
        return None
    got = inventory.find_by_type(scanner, inv_head, recall.RECALL_ITEM)
    if not got:
        return None
    # ⚠ 數量要用總數，不是那一格的（可疊物品會散成好幾疊）
    return got[0], inventory.count_by_type(scanner, inv_head,
                                           recall.RECALL_ITEM)


def do_recall(mover, scanner, inv_head: int) -> tuple[bool, str]:
    """觸發的**第二段**：用掉回程道具。第一段是 `begin_supply()`。

    ## 為什麼要分兩段

    使用者實測：開關調好之後**要隔 2~4 秒再回程**才穩，太快送回程精靈那邊
    還沒進入狀態，整趟就不會啟動。而那幾秒**不能用 sleep 卡住畫面**，
    所以拆成兩段、由呼叫端用計時器接（`SETUP_SETTLE`）。

    ## 整套觸發條件（使用者實測的規律，缺一不可）

        ① 主開關開　② **「攻擊指定敵人」關**　③ 按「設定」記下中心點
        ④ 自動攻擊開　→ 等 `SETUP_SETTLE` 秒 → ⑤ 人在**非主城**時「回程」
        → 再等 `AF_HOLD_SECS` 秒才把自動攻擊關掉

    ⚠⚠ **順序不能反**：先回程後調開關完全沒反應（我一開始就是這樣寫）。
    ⚠ 遊戲**沒有**「立刻跑一趟補給」的指令（補給頁每個控制項都查過了，
      全是設定用的勾選框）。所以只能靠這個組合去湊條件。
    ⚠ **耐久是滿的就沒有補給條件**，精靈到城裡會沒事可做（黑狐 70/70
      實測兩次都停在城裡）。真正的觸發是裝備真的壞了。

    ## 實測

        雪狐   靜謐海域 耐久 31 → 和風林 → +1.1 分修好 → +1.5 分走回戰場
        北極狐 靜謐海域 耐久  9 → 聖光城 → +0.7 分修好 → +0.9 分走回戰場

    ⚠ 會用掉一個回程道具（種類 RECALL_ITEM）。
    """
    from app.game import recall                       # 避免循環相依

    got = has_recall_item(scanner, inv_head)
    if not got:
        return False, f"背包裡沒有{itemname.label(recall.RECALL_ITEM)}"
    slot, count = got
    if not is_run(scanner):
        return False, "天使精靈不在執行中（旗標沒變）"
    if not recall.use_item(mover, slot):
        return False, "回程道具送不出去"
    return True, (f"用了第 {slot} 格的{itemname.label(recall.RECALL_ITEM)}"
                  f"（剩 {max(count - 1, 0)} 個）")


def set_org_spot(mover, scanner) -> tuple[bool, object]:
    """按下面板上的「設定」鈕 —— 把**現在站的地方**記成搜尋中心／原練功點。

    就是 `SETTING/BASE/WND01.XML` 控制項 10047 綁的那支 Lua 全域，0 個參數。

    ⚠⚠ **不設會半路停住**：黑狐記的原地圖是 125、人卻在 122，回城之後精靈
      不知道要回哪，就杵在城裡不動。
    ⚠ **它會順手把大地圖叫出來**（使用者回報，實測確認 `WND_STAGEMAP`
      從無變成有）。這裡用 `OnCloseStageMapWnd()` 關回去，不必去送 Esc／Alt+M。
      ★ 使用者原本就開著地圖的話**不動它** —— 只收拾自己弄出來的東西。
    ⛔ 不要改用 `game.autofightsetorgpos()`：實測它更新座標但把地圖 ID
      寫成 0（北極狐 95 → 0），比不設還糟。
    """
    was_open = _wnd(mover, scanner, "WND_STAGEMAP")
    got = lua.call(mover, scanner, "OnPressSetSearchPoint")
    if not was_open and _wnd(mover, scanner, "WND_STAGEMAP"):
        lua.call(mover, scanner, "OnCloseStageMapWnd")
    return got


AUTOFIGHT_CHECK_ID = 960          # 面板上「自動攻擊」勾選框（XML 裡的固定 id）


def set_autofight(mover, scanner, on: bool) -> None:
    """開／關「自動攻擊」。面板開著的話連勾選框一起動，畫面才不會對不上。

    ⚠ 這裡**不叫** `OnCheckAutoFight` —— 那是舊版路徑（會去註冊一個計時器），
      而且被 `[0x8909BA]` 擋著。真正決定行為的是變數 `AF_BOL_ISAUTOFIGHT`。
    """
    wnd = _wnd(mover, scanner, "WND_AUTOFIGHT")
    if wnd:
        ok, exists = lua.call(mover, scanner, "window.isexist", wnd,
                              AUTOFIGHT_CHECK_ID)
        if ok and exists is True:
            lua.call(mover, scanner, "window.setcheck", wnd,
                     AUTOFIGHT_CHECK_ID, bool(on))
    set_bool(mover, scanner, AF_IS_AUTO_FIGHT, bool(on))


def autofight_off(mover, scanner) -> None:
    """把「自動攻擊」關掉，但**不動主開關** —— 補給流程會繼續跑完。

    觸發後隔 `AF_HOLD_SECS` 秒呼叫這個，就等於「全程自動攻擊是關的」，
    精靈不會跟我們的掛機搶怪。（`end_supply()` 做的也是同一件事。）
    """
    set_autofight(mover, scanner, False)


def missing_supply_settings(mover, scanner) -> list[str]:
    """補給那一趟缺了哪些必要設定（回傳中文項目名；全都設好就是空清單）。

    讀不到就當作沒問題 —— 寧可讓它跑，也不要因為讀失敗擋住功能。
    """
    out = []
    for var_id, label in SUPPLY_NEEDED:
        ok, val = get_bool(mover, scanner, var_id)
        if ok and val is False:
            out.append(label)
    return out
