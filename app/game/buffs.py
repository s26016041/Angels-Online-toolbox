"""身上生效中的輔助技能（buff）—— 純讀，**含剩餘秒數**。

出處（2026-08-22 找到，五台實機對帳）
------------------------------------
官方「天使精靈」的自動輔助要判斷「這招我身上還有沒有」，那支判斷函式在
精靈指令表裡（`reports/robot_cmds.txt`：`cmd 0x1c0` → `0x554098`）：

    ecx = [0x96E618]                     世界管理員
    push [ecx+0x2A90]                    ← 自己的實體 ID
    call 0x5045DE                        ← 依 ID 找實體
    esi = [eax+0x420]                    ← **buff 容器**
    eax = [esi]                          ← begin()
    loop: cmp [eax+0x10], edi            ← **節點 +0x10 = 技能 ID**
          je → return 1
          call 0x507345                  ← ++it
          cmp eax, esi / jne loop
    return 0

⚠⚠ `+0x420` **不是鏈結串列，是 MSVC `std::map` 紅黑樹**（哨兵 `+0x0C` 低兩
  byte ＝ `0x0101` ＝ 顏色 1／哨兵旗標 1，跟 [[robot-var-tree]] 同一種版面）：

      +0x00 左　+0x04 父　+0x08 右　+0x0C 顏色　+0x0D 哨兵旗標
      +0x10 鍵（技能 ID）　+0x18 **到期時間（Unix 秒）**
      哨兵：+0x00 最小(begin)　+0x04 根　+0x08 最大

  沿 `+0x00` 一路走＝一直往左子走，**只會拿到 begin 那一個**（第一版就是這樣，
  五台各讀到 1 個 buff，看起來很合理但其實漏光了）。要走全部得用中序走訪。

★★ **剩餘時間也在裡面**（`+0x18`）—— 這推翻 [[skill-data-and-buff]] 記的
  「buff 剩餘時間找不到，11 條路全滅」：那 11 條都在找「每秒遞減的欄位」，
  但遊戲存的是**固定不動的到期戳記**，本來就不會遞減。
  實測（5 台 26 個 buff）：`0 < 到期−現在 ≤ 表定持續時間` **全中、零出界**；
  30 秒取樣剩餘線性 −1.00/秒；到期的瞬間節點就從樹上被移除。

⛔ 召喚物**不在**這棵樹上（`召喚噬魂怪Ⅰ` 781 在 magic.xml 裡沒有持續時間，
  它生的是實體）—— 那個問 `summon.read_pet_slot()`。

安全規則（CLAUDE.md）
--------------------
讀取端一律驗證，驗不過**回 None**（＝不知道），呼叫端要能退回自己記時間 ——
絕不把垃圾值當成「身上沒有」而白放一次，也不當成「還有」而漏放。
"""
from __future__ import annotations

import struct
import time

from app.game import bag, skills

# ⚠ 結構偏移（CLAUDE.md 允許寫死，大更新才會壞；出處＝上面的反組譯）。
#   ★ 全專案只有這裡認識這棵樹的版面，別在第二個地方再抄一份。
OFF_TREE = 0x420          # 實體 + 這裡 ＝ 哨兵（紅黑樹的環錨）
NODE_LEFT = 0x00
NODE_PARENT = 0x04        # 出處見上
NODE_RIGHT = 0x08         # 出處見上
NODE_KEY = 0x10           # 技能 ID（出處見上）
NODE_EXPIRE = 0x18        # 到期時間（Unix 秒）（出處見上）
NODE_SPAN = 0x1C          # ⚠ 讀取長度（一次讀到 +0x18 就夠），不是節點大小

MAX_NODES = 64            # 走訪上限（樹被改到時不會無限繞）
MAX_DEPTH = 32            # find 的下探上限（64 個節點的紅黑樹最深 ~12 層）
# 到期戳記的合理範圍：早於「60 秒前」或晚於 30 天後都當讀到垃圾。
#   ⚠ 上限要放得夠寬——變身類的持續時間是 86400 秒（1 天）。
STALE_BEFORE = 60.0
STALE_AFTER = 30 * 86400.0


def _ok(a) -> bool:
    return bool(a) and 0x10000 <= a <= 0x7FFF0000


def _u32(scanner, addr: int):
    if not _ok(addr):
        return None
    r = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(r))[0] if r and len(r) == 4 else None


def _node(scanner, addr: int):
    """讀一顆節點，回 (左, 父, 右, 鍵, 到期) —— 讀不到回 None。"""
    if not _ok(addr):
        return None
    r = scanner._read_bytes(addr, NODE_SPAN)
    if not r or len(r) < NODE_SPAN:
        return None
    b = bytes(r)
    return (struct.unpack_from("<I", b, NODE_LEFT)[0],
            struct.unpack_from("<I", b, NODE_PARENT)[0],
            struct.unpack_from("<I", b, NODE_RIGHT)[0],
            struct.unpack_from("<I", b, NODE_KEY)[0],
            struct.unpack_from("<I", b, NODE_EXPIRE)[0])


def _head(scanner, ent: int | None):
    """哨兵位址；讀不到／不像位址回 None。"""
    if ent is None:
        ent = bag.player_entity(scanner)
    if not _ok(ent):
        return None
    head = _u32(scanner, ent + OFF_TREE)
    return head if _ok(head) else None


def _sane(expire: int, skill_id: int, now: float) -> bool:
    """到期戳記合理嗎 —— 兩道交叉驗證，過不了就當讀到垃圾。

    ① 絕對範圍：不能早於 60 秒前、也不能晚於 30 天後。
    ② 跟技能表交叉比對：剩餘不可能超過**表定持續時間**（多給 60 秒餘裕，
       伺服器延長類的效果不至於差這麼多）。查不到表就只驗 ①。
    """
    left = expire - now
    if not (-STALE_BEFORE <= left <= STALE_AFTER):
        return False
    info = skills.of(skill_id)
    return not (info and left > float(info.secs) + 60.0)


def find(scanner, skill_id: int, ent: int | None = None) -> float | None:
    """那一招的**到期 Unix 秒**；不在身上回 `0.0`；**讀不到回 None**。

    ★ 走樹搜尋（O(log n)，2~4 次讀取），語意跟遊戲的 `std::map::find` 一樣：
      鍵就是技能 ID，五台實測中序走訪全是遞增，比較器是 `less`。
    ⚠ 找不到時**再做一次完整走訪確認**才敢說「身上沒有」——樹沒排序的話
      （改版換容器）搜尋會漏認，而「漏認」在呼叫端等於白放一次、無限重放。
      這一趟只在「準備施放」那一拍付出，值得。
    """
    if not skill_id:
        return None
    head = _head(scanner, ent)
    if head is None:
        return None
    now = time.time()
    cur = _u32(scanner, head + NODE_PARENT)          # 哨兵 +0x04 ＝ 根
    depth = 0
    while _ok(cur) and cur != head and depth < MAX_DEPTH:
        got = _node(scanner, cur)
        if got is None:
            return None
        left, _parent, right, key, expire = got
        if key == skill_id:
            return float(expire) if _sane(expire, key, now) else None
        cur = left if skill_id < key else right
        depth += 1
    # 樹搜尋說沒有 —— 再全走一次確認（順便擋「樹沒排序」這種改版意外）
    all_of = active(scanner, ent)
    if all_of is None:
        return None
    return all_of.get(skill_id, 0.0)


def active(scanner, ent: int | None = None) -> dict[int, float] | None:
    """身上**全部**生效中的效果：`{技能ID: 到期 Unix 秒}`；讀不到回 None。

    中序走訪整棵樹（++it ＝ 右子的最左，沒有右子就往上爬到「自己是左子」）。
    ⚠ 任何一顆節點驗不過就整包回 None —— 半套資料會讓呼叫端「安靜地做錯事」。
    """
    head = _head(scanner, ent)
    if head is None:
        return None
    cur = _u32(scanner, head + NODE_LEFT)            # 哨兵 +0x00 ＝ begin()
    if cur is None:
        return None
    now = time.time()
    out: dict[int, float] = {}
    seen: set[int] = set()
    while _ok(cur) and cur != head and len(out) < MAX_NODES:
        if cur in seen:
            return None                              # 繞圈＝樹正在被改
        seen.add(cur)
        got = _node(scanner, cur)
        if got is None:
            return None
        left, _parent, right, key, expire = got
        if not _sane(expire, key, now):
            return None
        out[key] = float(expire)
        if right != head and _ok(right):             # ++it
            cur = right
            for _ in range(MAX_DEPTH):
                nxt = _u32(scanner, cur + NODE_LEFT)
                if nxt is None or nxt == head or not _ok(nxt):
                    break
                cur = nxt
        else:
            for _ in range(MAX_DEPTH):
                parent = _u32(scanner, cur + NODE_PARENT)
                if parent is None or not _ok(parent) or parent == head:
                    cur = head                       # 爬到頂 ＝ end()
                    break
                if _u32(scanner, parent + NODE_RIGHT) != cur:
                    cur = parent                     # 自己是左子 → 父是後繼
                    break
                cur = parent
            else:
                return None                          # 爬不完＝版面不對
    return out if cur == head else None


def left_of(scanner, skill_id: int, ent: int | None = None) -> float | None:
    """那一招**還剩幾秒**；身上沒有回 `0.0`；**讀不到回 None**（≠ 沒有）。"""
    expire = find(scanner, skill_id, ent)
    if expire is None:
        return None
    if not expire:
        return 0.0
    return max(0.0, expire - time.time())


def listing(scanner, ent: int | None = None) -> list[tuple[int, str, float]]:
    """給介面看的：`[(技能ID, 名稱, 剩餘秒), ...]`，剩越少的排前面。

    讀不到就回空清單（介面顯示「讀不到」由呼叫端決定，這裡不猜）。
    """
    got = active(scanner, ent)
    if not got:
        return []
    now = time.time()
    rows = [(sid, skills.name_of(sid) or f"技能 {sid}", max(0.0, exp - now))
            for sid, exp in got.items()]
    rows.sort(key=lambda r: r[2])
    return rows
