"""物品的固定知識：經驗球的種類 ID → 名稱 / 容量上限。

★ 設計原則：只查這張寫死的表，不做任何執行時推測 ★
---------------------------------------------------
以前曾經加過三種「聰明」的做法，全部移除了：
  * 從遊戲的物品說明表現場解析上限
  * 玩家把滑鼠移到球上時，靠道具提示反查上限
  * 從商店列表用售價接合出 ID
它們的共同問題是**會猜錯而且不會報錯**。實際踩過一次：背包和商人視窗同時有值為 0
的球，靠「值」配對道具提示就把兩者搞混，導致 ID 對到錯的球種。

更根本的理由是使用場景：玩家通常**已經裝好球才開這支工具，而且不會再回遊戲操作**，
所以任何「需要玩家互動才學得到」的機制在產品上都等於沒用。表必須在出貨前就正確。

ID 從哪裡來
-----------
解包遊戲資料檔可以拿到完整的物品 ID 與詳細內容，那才是正確的來源。
本檔的表就是那份資料的抄本 —— **只有確定的才填，不確定的一律留空**。
留空只會讓畫面顯示「非經驗球」；填錯會顯示一個很有自信的錯誤百分比，那更糟。

版面
----
物品結構 +0x08 是種類 ID、+0xA0 是球的累積值（見 app/game/inventory.py）。
從 AOB 命中的「球值位址」回推種類 ID 的偏移就是 TYPE_OFFSET。
"""
from __future__ import annotations

from dataclasses import dataclass

# 種類 ID 相對「球值位址」的偏移（球值位址 = AOB 命中 + 0x80，見 app/game/aob.py）
TYPE_OFFSET = -0x98

# 認不出來時畫面上顯示的字樣
UNKNOWN_LABEL = "非經驗球"


@dataclass(frozen=True)
class BallType:
    """一種經驗球（裝在飾品欄、隨著打怪或用技能累積經驗的道具）。

    name       遊戲內名稱
    cap        最大累積值
    bonus      「額外再獲得 N% 經驗值儲存入經驗球內」
    family     技能 / 角色 / 寵物 —— 三個系列都裝飾品欄，只是累積的經驗種類不同
    min_level  裝備需求等級；0 = 不明
    type_id    記憶體裡的物品種類 ID；None = 還不確定，認不出來
    """

    name: str
    cap: int = 0
    bonus: int = 0
    family: str = "技能"
    min_level: int = 0
    type_id: int | None = None

    def pct(self, value: int) -> float | None:
        """目前累積的百分比；上限未知時回傳 None。"""
        if self.cap <= 0:
            return None
        return min(100.0, value / self.cap * 100.0)


# ---------------------------------------------------------------------------
# 目錄
#
# 名稱 / 加成 / 上限：抄自遊戲自己的物品說明表（原文如「裝備至飾品欄位後，使用技能
# 將可額外再獲得 35% 技能經驗值儲存入經驗球內，最大累積值 35,000。」）。
# 佐證：其中二階 35,000、三階 120,000 與實機讀到的值完全吻合。
#
# type_id：**只填確定的**。目前確定的五筆：
#   4936 / 4937  五個分身差異分析鎖定，並對照過使用者畫面數值
#   4935         商店記錄售價接合（一階技能經驗球 x1 售價 10），清空表後能自己學回來
#   5008         遊戲聊天訊息「獲得 二階迷你技能經驗球（數量 2）」+ 物品欄同時出現兩筆
#   5007         使用者把它裝在嵐狐的飾品欄右邊並告知品名，直接讀第 9 格的種類 ID
#
# ★ 最可靠的補 ID 方法就是 5007 那種：請使用者把某顆球裝到飾品欄某一邊、說出品名，
#   再直接讀那一格的種類 ID。沒有任何配對歧義（不像靠「值」去配道具提示會搞混）。
#
# 其餘 25 種等解包資料補上。曾經有過的推測（例如「5007-5009｜5162-5164｜5347-5349
# 三組連號分別對應技能/角色/寵物的迷你三階」）**刻意不填** —— 那只是一兩次的零星
# 命中加上連號假設，而且 5162 還有另一個候選 79768。
#
# 三個系列都裝在飾品欄，差別在累積哪一種經驗：
#   技能經驗球 —— 用技能時累積技能經驗
#   角色經驗球 —— 打怪時累積角色經驗（右鍵使用給予經驗後球會消失）
#   寵物經驗球 —— 打怪時累積，給召喚中的寵物
# （「經驗寶石 / 鑽石 / 彩鑽」是直接用在寵物身上的道具，不進飾品欄，不收錄。）
# ---------------------------------------------------------------------------

SKILL_BALLS = (
    BallType("一階迷你技能經驗球", cap=1_600,    bonus=10, type_id=5007),
    BallType("二階迷你技能經驗球", cap=10_000,   bonus=10, type_id=5008),
    BallType("三階迷你技能經驗球", cap=30_000,   bonus=10),
    BallType("一階技能經驗球",     cap=5_000,    bonus=30, type_id=4935),
    BallType("二階技能經驗球",     cap=35_000,   bonus=35, min_level=60, type_id=4936),
    BallType("三階技能經驗球",     cap=120_000,  bonus=40, min_level=80, type_id=4937),
    BallType("新手技能經驗球",     cap=50_000,   bonus=30),
    BallType("普通技能經驗球",     cap=100_000,  bonus=30),
    BallType("優質技能經驗球",     cap=150_000,  bonus=35),
    BallType("精選技能經驗球",     cap=300_000,  bonus=35),
)

CHAR_BALLS = (
    BallType("一階迷你角色經驗球", cap=100_000,      bonus=10, family="角色"),
    BallType("二階迷你角色經驗球", cap=1_000_000,    bonus=10, family="角色"),
    BallType("三階迷你角色經驗球", cap=5_000_000,    bonus=10, family="角色"),
    BallType("四階迷你角色經驗球", cap=10_000_000,   bonus=10, family="角色"),
    BallType("一階角色經驗球",     cap=400_000,      bonus=30, family="角色"),
    BallType("二階角色經驗球",     cap=4_000_000,    bonus=30, family="角色"),
    BallType("三階角色經驗球",     cap=20_000_000,   bonus=30, family="角色"),
    BallType("四階角色經驗球",     cap=40_000_000,   bonus=30, family="角色"),
    BallType("新手經驗球",         cap=100_000,      bonus=25, family="角色"),
    BallType("普通經驗球",         cap=18_000_000,   bonus=20, family="角色"),
    BallType("優質經驗球",         cap=300_000_000,  bonus=15, family="角色"),
    BallType("精選經驗球",         cap=500_000_000,  bonus=5,  family="角色"),
    BallType("測試-經驗球",        cap=400_000,      bonus=35, family="角色"),  # 測試道具
)

PET_BALLS = (
    BallType("一階迷你寵物經驗球", cap=50_000,     bonus=10, family="寵物"),
    BallType("二階迷你寵物經驗球", cap=500_000,    bonus=10, family="寵物"),
    BallType("三階迷你寵物經驗球", cap=2_000_000,  bonus=10, family="寵物"),
    BallType("一階寵物經驗球",     cap=200_000,    bonus=30, family="寵物"),
    BallType("二階寵物經驗球",     cap=2_000_000,  bonus=30, family="寵物"),
    BallType("三階寵物經驗球",     cap=8_000_000,  bonus=30, family="寵物"),
)

ALL_BALLS = SKILL_BALLS + CHAR_BALLS + PET_BALLS

# 實際查表用的：只有已知 ID 的才進得來
SKILL_EXP_BALLS: dict[int, BallType] = {
    b.type_id: b for b in ALL_BALLS if b.type_id is not None
}

# AOB 特徵不只命中球，也會命中一堆別的物品（實測看過 3516～3531、4870、4942～4963、
# 81379、87303 …）。所以拿到候選位址後一定要用種類 ID 過濾，別靠數值大小猜。


def ball_type(type_id: int | None) -> BallType | None:
    """種類 ID → 球的定義。不在表裡就回 None（畫面會顯示 UNKNOWN_LABEL）。"""
    if type_id is None:
        return None
    return SKILL_EXP_BALLS.get(type_id)


def is_ball(type_id: int | None) -> bool:
    return ball_type(type_id) is not None


# ⚠ 球的種類 ID 每輪都要重讀，不能快取 —— 玩家換飾品時遊戲會把那塊記憶體挪去
#   放別的物品，位址還在、值也還讀得到，但已經是另一顆球了（現由
#   watcher._ball() 每輪走 inventory._walk 重讀）。
