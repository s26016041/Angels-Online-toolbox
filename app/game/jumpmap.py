"""天使趴趴GO：**填一個編號就傳送**，不必開視窗、不必碰 UI。

    jumpmap.by_scene(7)                  → [Entry(13, '向日葵平原(LV12~23)入口'), …]
    jumpmap.teleport(mover, sc, 13)      → True（1 秒後人就到了）

✅ 實測（黑狐）：千夜魔宮(126) (20.5,23.5) → 送目的地 13 → **+1 秒**
   向日葵平原(7) (15.5,177.5)。落點跟表裡寫的 (15,177) 完全一致。

怎麼送
------
照 `0x5E751B` 尾巴那段原樣重做（那就是遊戲自己送傳送包的程式碼）：

    0x50DF6E(代號 0x151, 內文 6)  ecx = 16 bytes 暫存區 S   → 建封包
    資料 = [S+4]；[資料+2] = 跳地圖編號                      → 填目的地
    0x711130([連線], [S+0xC])                               → 送出

⚠⚠ **不要走 `game.gotojumpmap`**：那支吃的是「清單第幾列」，而那是樹狀清單的
  視覺列號 —— 會隨你切類別、展開、捲動而變。使用者四次實測的列號
  （6/7/9/11）對照 JumpMap.xml，只有一次對得上。
⚠⚠ **更不要去讀那個視窗的清單**：`getlistitemnum` / `getitemtitle` 對它是無效
  操作，實測 4 次有 4 次把客戶端的訊息迴圈弄卡死（要重開遊戲）。
  詳見 [[lua-engine]] 記的「碰資料安全、碰 UI 危險」。

表從哪來
--------
`assets/jumpmap.tsv`（120 筆），`tools/build_jumpmap.py` 從遊戲資源包的
`SETTING/base/JumpMap.xml` + `str_jumpmap.xml` 抽出來的。
⚠ 改版增減傳送點要重跑那支工具。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.paths import resource

DATA_FILE = "assets/jumpmap.tsv"

# ⚠ 這三個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
BUILD_FN = 0x0050DF6E        # 建封包(代號, 內文長度)，ecx = 暫存區
SEND_FN = 0x00711130         # 送出(連線, 封包)
CONN_PTR = 0x009B67D0        # [這裡] = 連線物件

OPCODE = 0x151               # 傳送封包
BODY = 6                     # 內文 = 代號(u16) + 跳地圖編號(u32)
SCRATCH_OFF = 0x100          # 相對 mover.scratch()；別跟 lua.py 的字串區撞到
CALL_TIMEOUT = 1.0


@dataclass(frozen=True)
class Entry:
    jump_id: int             # 跳地圖編號 —— 送封包用的就是這個
    scene_id: int            # 場景編號
    x: int                   # 落點格子座標
    y: int
    name: str                # 例：'向日葵平原(LV12~23)入口'

    def __str__(self) -> str:
        return self.name or f"跳地圖 {self.jump_id}"


_table: list[Entry] | None = None


def entries() -> list[Entry]:
    """整張傳送表（第一次用到才載入）。檔案缺了就回空清單。"""
    global _table
    if _table is None:
        out: list[Entry] = []
        try:
            with open(resource(DATA_FILE), encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) == 5:
                        out.append(Entry(int(p[0]), int(p[1]), int(p[2]),
                                         int(p[3]), p[4]))
        except Exception:                                  # noqa: BLE001
            out = []
        _table = out
    return _table


def by_scene(scene_id: int) -> list[Entry]:
    """某張地圖的所有傳送點（入口／重生點／副本進入點可能有好幾個）。"""
    return [e for e in entries() if e.scene_id == scene_id]


def nearest(scene_id: int, x: float | None = None,
            y: float | None = None) -> Entry | None:
    """那張地圖**離 (x,y) 最近**的傳送點。

    ★ 一張地圖常有「入口」和「重生點」兩個落點，位置可以差很遠。傳回原本
      掛機的地方附近那一個，接回自動戰鬥時才不用走一大段。
      不給座標就給第一個。
    """
    cand = by_scene(scene_id)
    if not cand:
        return None
    if x is None or y is None:
        return cand[0]
    return min(cand, key=lambda e: (e.x - x) ** 2 + (e.y - y) ** 2)


def get(jump_id: int) -> Entry | None:
    return next((e for e in entries() if e.jump_id == jump_id), None)


def search(text: str) -> list[Entry]:
    """用名字找（模糊比對）。"""
    key = text.strip()
    return [e for e in entries() if key in e.name] if key else []


def teleport(mover, scanner, jump_id: int) -> tuple[bool, str]:
    """傳送到某個跳地圖編號。回傳 (成功送出嗎, 說明)。

    ★ 純封包，不碰 UI —— 跟送攻擊、用道具同一個安全等級。
    ⚠ 只保證「封包送出去了」；到不到得看伺服器（等級不足之類會被拒絕）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    e = get(jump_id)
    if e is None:
        return False, f"傳送表裡沒有編號 {jump_id}"

    def u32(a: int) -> int:
        raw = scanner._read_bytes(a, 4)
        return struct.unpack("<I", bytes(raw))[0] if raw else 0

    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(BUILD_FN, OPCODE, BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去"
        data = u32(buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        mover.write(data + 2, struct.pack("<I", jump_id))
        if mover.call_sync(SEND_FN, u32(CONN_PTR), u32(buf + 0xC),
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去"
    return True, f"已送出傳送：{e}"
