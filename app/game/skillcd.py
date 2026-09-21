"""這一招**現在是不是在冷卻**（＝遊戲 UI 把技能圖示變黑的那個判斷）—— 純讀記憶體。

出處（2026-09-21 反組譯，使用者提示「CD 中 UI 技能會變成黑色，一定讀得到」）：

  快捷欄 UI 更新 `0x6024D7`（技能格分支 `0x6026DE`）對每一格呼叫
      自己實體->vtable[+0x3C](技能ID, 0, 0)          （玩家實體 vt 0x803BCC → 0x548CC5）
  回 0 就 `or [圖示+4], 1`（＝變黑）。0x548CC5 前半是武器／技能限制，後半轉給共用的
  `0x506E80`，跟冷卻有關的就是這一段：

      list = [實體+0x418]                 ; std::list 的哨兵節點
      0x505DD7(&技能ID) 找得到               → 不能放
      for node in list:
          [node+0x10] == 技能ID            → 不能放
          範本[+0x4C] & 0x800 且 範本[+0xE8] != 0
            且 node 那招的範本[+0xE8] 相同  → 不能放（同一組共 CD）
      ⚠ +0xE8 **不是** magic.xml 的「群組編號」：實機對表 743/764 讀到 300（＝「高權位」）、
        686→79、773→185。這裡照抄遊戲的比法（相等就算同組），不去解釋它的意義。

  ★ 節點的壽命＝那一招的**後置時間**（magic.xml）：實測 686（後置 2000）活 2.07 秒、
    773（後置 6000）活 6.04 秒、瞬移術Ⅳ 1400ms（見 memory skill-data-and-buff）。
    這遊戲沒有別的「冷卻時間」欄，技能 CD 就是這一段。

⚠ 已知盲區（同一份 memory）：邊走邊用封包施放時客戶端不長節點 → 這裡會回「沒在冷卻」。
  所以它**只能拿來決定「要不要多等一下」**（答錯＝照舊往下走，安全退化），
  ⛔ 不能當「這一發有沒有被受理」的確認 —— 那個一律用 castwatch 的施放廣播。
⚠ 清單是遊戲自己在改的，讀到一半可能抓到回收中的節點：走訪設上限、指標逐個驗，
  任何一步讀不到就回 None（＝不知道），呼叫端當成「沒在冷卻」。

`+0x418`／`+0x10`／`+0x4C`／`+0xE8` 都是結構偏移（大更新才會壞），出處如上。
"""
from __future__ import annotations

import struct

from app.game import skillcost

OFF_CD_LIST = 0x418           # 實體 +0x418：冷卻清單（std::list 哨兵）；出處 0x506EDA
NODE_SKILL = 0x10             # 節點 +0x10：技能 ID；出處 0x506F01
TMPL_FLAGS = 0x4C             # 範本 +0x4C：旗標（0x800＝同群組共 CD）；出處 0x506F0A
TMPL_GROUP = 0xE8             # 範本 +0xE8：共 CD 的分組值（≠ 群組編號，見檔頭）；出處 0x506EC5
FLAG_GROUP_CD = 0x800
MAX_NODES = 64                # 實測同時最多 1~2 顆；超過＝讀到垃圾


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def _sane(p: int | None) -> bool:
    return p is not None and 0x10000 <= p < 0x7FFF0000


def cooling(scanner, entity_addr: int, skill_id: int) -> bool | None:
    """`skill_id` 現在在冷卻嗎。True／False；讀不到回 None（＝不知道）。"""
    sid = int(skill_id or 0)
    if not sid or not _sane(entity_addr):
        return None
    head = _u32(scanner, entity_addr + OFF_CD_LIST)
    if not _sane(head):
        return None
    group = 0
    tmpl = skillcost.template(scanner, sid)
    if tmpl:
        flags = _u32(scanner, tmpl + TMPL_FLAGS)
        grp = _u32(scanner, tmpl + TMPL_GROUP)
        if flags is not None and grp and flags & FLAG_GROUP_CD:
            group = grp
    cur = _u32(scanner, head)
    for _ in range(MAX_NODES):
        if not _sane(cur):
            return None
        if cur == head:
            return False
        nid = _u32(scanner, cur + NODE_SKILL)
        if nid is None:
            return None
        if nid == sid:
            return True
        if group and nid != sid:
            t2 = skillcost.template(scanner, nid)
            if t2 and _u32(scanner, t2 + TMPL_GROUP) == group:
                return True
        cur = _u32(scanner, cur)
    return None                                    # 走不完＝清單讀壞了
