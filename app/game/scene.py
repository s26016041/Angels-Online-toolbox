"""目前所在地圖（場景編號）：一條靜態指標直接讀，加上掃 vtable 的備援。

為什麼需要它
------------
巡邏點是「某張地圖上的某個座標」。座標本身（實體 +0xBC/+0xC0，見 entity.py）
在每張地圖都是從 0 開始算的格子，所以**光看座標分不出是哪張圖** —— 在 A 圖存的
點拿到 B 圖去跑，角色會往一個完全不相干的地方走。存巡邏點時必須一起記下場景
編號，執行前先比對「現在這張圖是不是當初那張」，不同就不要動。

怎麼讀
------
    [angel.dat + SCENE_PTR_RVA]  →  場景物件
    場景物件 + OFF_SCENE_ID      →  目前場景編號（int32）

那個靜態位址是遊戲自己的全域變數，指向場景物件；換地圖時**物件位址不變、只有
編號被改寫**（實測連換三張圖都是同一個位址）。解析一次約 1.5 ms，等於免費，
所以不必快取、每次要用時現讀就好。

⚠ 不要用「地圖名稱字串」找目前地圖 —— 記憶體裡沒有這種東西。搜中文地圖名只會
  搜到任務對話文字；`map\\xxx.mpc` 路徑則是 STAGE.XML 整份載入的 297 條全都在，
  跟現在在哪張圖無關。

⚠ 名稱不是唯一的：天使學園有 41 / 141 / 241 三個編號、前哨戰場有 47 / 48。
  所以巡邏點一律存**編號**，名稱只拿來顯示給人看。

驗證（2026-08-04）
------------------
* 五個分身各自解析成功、每台掃 vtable 也剛好命中 1 筆，兩條路結果完全一致。
* 黑狐連換四張圖（亂石海岸 → 隱月沼澤 → 蘑菇森林 → 火焚沙漠 → 沉沒遺跡），
  每次都跟畫面相符；用 50 ms 取樣盯著換圖的瞬間，沒有出現空指標或暫態垃圾值。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

GAME_MODULE = "angel.dat"

# ⚠⚠ 以下三個位址**遊戲改版就會位移**（見 memory 的 game-patch-relocate）。
#    症狀是 current() 開始回 None（結構檢查擋下來了，不會顯示錯的地圖）。
#    重新定位的方法就是當初找到它的方法，見本檔最後的「怎麼重新定位」。
SCENE_PTR_RVA = 0x490948   # 全域指標所在（絕對位址 0x00890948）
# ⚠ 場景物件的 vtable（掃描備援＋讀值驗證用）；改版會位移，重定位見檔尾。
VTABLE_RVA = 0x3DCF3C      # 場景物件的 vtable 值（絕對 0x007DCF3C）
OFF_SCENE_ID = 0x1190      # 物件內：目前場景編號


@dataclass(frozen=True)
class Scene:
    """目前所在場景。`name` 只供顯示，比對一律用 `key`。"""

    id: int
    obj: int

    @property
    def name(self) -> str:
        return scene_name(self.id)

    @property
    def key(self) -> int:
        """比對用的「同一張圖」代號（分流會收斂到同一個值）。"""
        return map_key(self.id)

    def __str__(self) -> str:
        return f"{self.name}（場景 {self.id}）"


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw[:4]))[0]


def vtable_value(scanner) -> int | None:
    """場景物件的 vtable 指標值（模組基底 + RVA）。"""
    base = scanner.module_base(GAME_MODULE)
    return (base + VTABLE_RVA) if base else None


def _scene_of(scanner, obj: int, vtable: int) -> Scene | None:
    """確認 obj 真的是場景物件（開頭是那個 vtable），是的話讀出編號。

    只做結構檢查，不檢查編號落在哪個範圍 —— 改版新增地圖時編號會是表裡沒有的
    數字，那種情況要照樣讀得到（顯示成「場景 123」），不能當成壞掉。
    """
    if _u32(scanner, obj) != vtable:
        return None
    sid = _u32(scanner, obj + OFF_SCENE_ID)
    return None if sid is None else Scene(id=sid, obj=obj)


def current(scanner, allow_scan: bool = True) -> Scene | None:
    """目前所在場景；讀不到（改版位移、遊戲還沒進到地圖）時回 None。

    ⚠ `allow_scan=False` 時只走靜態指標（< 1.5 ms），**不做 0.25 秒的全掃備援**。
      每一拍都會呼叫的地方一定要用這個 —— farm_tab 的心跳是 10 ms 一拍，
      在那裡掃一次就等於整個畫面卡住。備援請自己隔久一點呼叫 `locate()`
      並把 `Scene.obj` 快取起來（見 `read_at()`）。
    """
    vtable = vtable_value(scanner)
    if vtable is None:
        return None
    obj = _u32(scanner, scanner.module_base(GAME_MODULE) + SCENE_PTR_RVA)
    if obj:
        found = _scene_of(scanner, obj, vtable)
        if found is not None:
            return found
    return locate(scanner) if allow_scan else None


def current_id(scanner, allow_scan: bool = True) -> int | None:
    """只要編號的簡便版。"""
    s = current(scanner, allow_scan)
    return None if s is None else s.id


def read_at(scanner, obj: int) -> Scene | None:
    """從已知的場景物件位址直接讀（給快取了 `locate()` 結果的呼叫端）。

    每次都重新確認 vtable —— 位址會被回收再利用，不重驗就可能讀到別的東西。
    """
    vtable = vtable_value(scanner)
    return None if vtable is None else _scene_of(scanner, obj, vtable)


def locate(scanner) -> Scene | None:
    """備援：全記憶體掃 vtable 找場景物件（約 0.3 秒，實測每台剛好 1 筆）。

    靜態指標的偏移比 vtable 更容易被改版影響（它只是 .data 裡的一格），所以留
    這條慢路當保險。兩條都失效才是真的要重新定位。
    """
    vtable = vtable_value(scanner)
    if vtable is None:
        return None
    want = np.uint32(vtable)
    for base, size in scanner._iter_regions(writable_only=True):
        if base + size > 0xFFFFFFFF:      # WOW64 的 64 位元區，遊戲資料不在那
            continue
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        for i in np.flatnonzero(arr == want):
            found = _scene_of(scanner, base + int(i) * 4, vtable)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# 支流空間（＝遊戲介面寫的「分流1～5」，stage.xml 的「平行空間」）
# ---------------------------------------------------------------------------
# ★★★ 場景編號的**高 16 位是支流序號**，低 16 位才是 stage.xml 的場景編號：
#         raw = (支流序號 << 16) | 場景編號        支流序號 0 ＝「分流1」
#
# 兩份互相獨立的證據（2026-08-27）：
#   ① 遊戲自己的對話腳本 `GAMEDATA/setting/base/spmsg.xml` 對話 2023126~2023130
#      （啤酒節使者的「暴走穗海農場分流1~5」五個選項）送的是
#      `動作 37, 參數 = (441, 0..4, 1)` —— 場景 441 加一個 0 起算的支流序號；
#      `stage.xml` 場景 441 也寫著 `平行空間="20"`。
#   ② 實機讀值：使用者在「暴走穗海農場」上，`OFF_SCENE_ID` 讀到
#      **65977 = (1 << 16) | 441**（＝分流2）；同一拍另外四台在一般地圖
#      （123 人馬崗哨、126 千夜魔宮）高 16 位都是 0。
#      使用者最早回報的 197049 = (3 << 16) | 441 ＝分流4，也對得上。
#
# ⚠ 一般地圖的編號全都 < 1000（stage.xml 最大是 999），所以「> 0xFFFF ＝ 有支流」
#   不會誤判；高 16 位是 0 時整條路徑跟以前完全一樣。
SUBSPACE_SHIFT = 16
_SCENE_MASK = (1 << SUBSPACE_SHIFT) - 1


def split(scene_id: int | None) -> tuple[int | None, int]:
    """場景編號 → (真正的場景編號, 支流序號)。支流序號 0 ＝本流／分流1。"""
    if scene_id is None:
        return None, 0
    return scene_id & _SCENE_MASK, scene_id >> SUBSPACE_SHIFT


def base_id(scene_id: int | None) -> int | None:
    """把支流序號剝掉，只留 stage.xml 那個場景編號。"""
    return split(scene_id)[0]


def subspace(scene_id: int | None) -> int:
    """支流序號（0 起算）；一般地圖是 0。介面上的「分流N」＝ 這個值 + 1。"""
    return split(scene_id)[1]


def scene_name(scene_id: int | None) -> str:
    """編號 → 中文地圖名；表裡沒有（改版新增）就照樣顯示編號。

    有支流序號時照遊戲的講法接在後面（「暴走穗海農場分流2」，
    字串出處 `str_msg` 508269~508273）。
    """
    if scene_id is None:
        return "未知"
    base, sub = split(scene_id)
    name = SCENE_NAMES.get(base, f"場景 {base}")
    return name if sub == 0 else f"{name}分流{sub + 1}"


def map_key(scene_id: int | None) -> int | None:
    """把場景編號收斂成「同一張圖」的代號 —— **比對地圖時用這個，不要用編號**。

    同一張圖的不同分流是不同的場景編號（天使學園 = 41 / 141 / 241），但地形完全
    一樣（都是 `map\\map041.mpc`），所以巡邏點的座標可以互通、換分流不該擋。

    ★ 支流空間（暴走穗海農場分流1~5）也一樣：**同一個 .mpc、座標完全互通**，
      每次進去支流序號都不一定一樣（伺服器分配／使用者自己選），拿 raw 編號硬比
      會把「已經站在同一張圖」看成別張圖 —— 巡邏點、回程判定就全部失效
      （使用者回報的「記錄點變成場景 197049」就是這個）。
    """
    if scene_id is None:
        return None
    base = base_id(scene_id)
    return SAME_MAP_AS.get(base, base)


def same_map(a: int | None, b: int | None) -> bool:
    """兩個場景編號是不是同一張圖（分流算同一張）。任一邊未知就回 False。"""
    if a is None or b is None:
        return False
    return map_key(a) == map_key(b)


# 分流：編號不同、地圖檔相同 → 視為同一張圖。
# 從 STAGE.XML 的「地圖檔」欄位算出來的（scratchpad/map_alias.py），全部 15 筆。
# ⚠ 反過來**不能用名字比對**：同名但地圖檔不同的有 前哨戰場(47/48)、
#   魔廚餐道(73/74/75)、吞噬之間(76~79)、天際方陣、女神之塔、虛空幽徑、試煉之城
#   —— 那些是真的不同地圖，座標不通用。
SAME_MAP_AS: dict[int, int] = {
    141: 41, 241: 41,        # 天使學園
    142: 42, 242: 42,        # 東方操場
    143: 43, 243: 43,        # 西方操場
    144: 44, 244: 44,        # 試煉之殿
    266: 265, 267: 265, 268: 265, 269: 265,   # GVG 地圖 2~8
    270: 265, 271: 265, 272: 265,
}


# ---------------------------------------------------------------------------
# 場景編號 → 中文地圖名
#
# 抄自遊戲資料，**不是**執行時解析出來的：
#     GAMEDATA/setting/BASE/STAGE.XML             <場景 編號="105" 地圖檔="map\\map105.mpc"/>
#     GAMEDATA/setting/BIG5/STRING/STR_STAGE.XML  <表格字串 編號="1290000105" 文字1="喧嘩雨林"/>
# 對照關係是「表格字串編號 = 1290000000 + 場景編號」（已驗證）。
#
# ⚠ 跟 items.py 一樣是**寫死的遊戲資料**：改版新增地圖時這裡不會自動跟上，
#   只會顯示成「場景 123」（不會顯示錯的名字）。
# ★ 更新方式（**不要手打**，CLAUDE.md 的鐵則）：
#       py tools\\build_scene_names.py            # 從 GAMEDATA 重抽、寫回這裡
#       py tools\\build_scene_names.py --check    # 只比對，過期回傳碼 1
#   那兩個 XML 在專案的 GAMEDATA/setting/ 底下，遊戲目錄那邊是打包在 .pak 裡的。
# ---------------------------------------------------------------------------
SCENE_NAMES: dict[int, str] = {
    2: "亂石海岸",
    3: "聖光城",
    4: "晨曦港灣",
    5: "櫻貝村",
    6: "穗海農場",
    7: "向日葵平原",
    8: "破碎之丘",
    9: "鏡湖南岸",
    11: "雷轟廢墟",
    12: "鏡湖北岸",
    13: "翡翠溪谷",
    14: "德古拉迷宮",
    15: "迷幻溼地",
    16: "荊棘荒原",
    17: "寂靜山谷",
    18: "蘑菇森林",
    19: "蕈林南方",
    20: "鬼霧林",
    21: "惡龍墳場",
    22: "秘密花園",
    23: "深邃密林",
    25: "蕈林北方",
    26: "永夜城",
    27: "神殿遺址",
    28: "巨石草原",
    29: "和風林",
    30: "隱月沼澤",
    31: "無底洞",
    32: "暗影通道",
    33: "火焚沙漠",
    34: "廢鐵村",
    35: "遺忘之窟",
    36: "哥布林峽谷",
    37: "希望之淚",
    38: "鐵輪堡",
    39: "仙人掌高原",
    41: "天使學園",
    42: "東方操場",
    43: "西方操場",
    44: "試煉之殿",
    47: "前哨戰場",
    48: "前哨戰場",
    50: "低等領地祕境",
    51: "新生引導聖殿",
    52: "聖光祕境",
    53: "百獸祕境",
    54: "鋼鐵祕境",
    55: "暗影祕境",
    57: "戰鬥聖殿",
    58: "畢業聖殿",
    59: "阿斯莫德巢穴",
    60: "違規者監獄",
    61: "噩夢之穴",
    62: "GM休息站",
    67: "悲鳴深淵",
    68: "熾熱通道",
    69: "熔岩石窟",
    70: "烈燄巨門",
    71: "地底廣場",
    72: "闇獄殿堂",
    73: "魔廚餐道",
    74: "魔廚餐道",
    75: "魔廚餐道",
    76: "吞噬之間",
    77: "吞噬之間",
    78: "吞噬之間",
    79: "吞噬之間",
    80: "天使教堂",
    84: "藍海陸棚",
    85: "幻彩珊瑚礁",
    86: "黃金海灘",
    87: "噗吉噗吉村",
    88: "棕櫚基地",
    89: "浪濤岩灣",
    90: "湛藍深洋",
    91: "沉沒遺跡",
    92: "晴空海岸",
    93: "幻惑之海",
    94: "怒海環礁",
    95: "靜謐海域",
    96: "惡礁峽谷",
    97: "邪靈古船",
    98: "遺落之地",
    99: "夏日沙灘",
    100: "中等領地祕境",
    101: "專家級遺落之地",
    102: "風林河谷",
    103: "隱流祕穴",
    104: "觀瀑營地",
    105: "喧嘩雨林",
    106: "古溜溜流域",
    107: "半獸部落",
    108: "溼熱林",
    109: "巨木梯道",
    110: "無限塔",
    111: "雲頂聖域",
    112: "聖域戰場",
    114: "報名檢錄處",
    120: "月牙峽谷",
    121: "荒漠賽道",
    122: "鬼影寨",
    123: "人馬崗哨",
    124: "遠古戰地",
    125: "迷幻沙都",
    126: "千夜魔宮",
    127: "莉維坦的寢室",
    128: "龍息之地",
    129: "隱者溼地",
    130: "花舞長廊",
    131: "光明殿",
    132: "神祕空間",
    141: "天使學園",
    142: "東方操場",
    143: "西方操場",
    144: "試煉之殿",
    159: "戰天使擂台",
    177: "天際方陣",
    178: "史萊姆晴空牧場",
    179: "試煉之城",
    180: "虛空幽徑",
    181: "騰雲樹海",
    182: "天使快樂村",
    183: "女神之塔",
    184: "天際方陣",
    185: "天際方陣",
    186: "女神之塔",
    187: "女神之塔",
    188: "試煉之城",
    189: "試煉之城",
    190: "虛空幽徑",
    191: "虛空幽徑",
    201: "鏡湖南岸戰場",
    207: "惡龍墳場戰場",
    208: "派對活動出口",
    241: "天使學園",
    242: "東方操場",
    243: "西方操場",
    244: "試煉之殿",
    265: "GVG 地圖1",
    266: "GVG 地圖2",
    267: "GVG 地圖3",
    268: "GVG 地圖4",
    269: "GVG 地圖5",
    270: "GVG 地圖6",
    271: "GVG 地圖7",
    272: "GVG 地圖8",
    283: "暴走夏日沙灘",
    441: "暴走穗海農場",
    501: "社團農場",
    502: "天使小學堂",
    999: "測試地圖",
}


# ---------------------------------------------------------------------------
# 怎麼重新定位（改版之後）
# ---------------------------------------------------------------------------
# 花了大約半小時、使用者配合換三次圖就收斂了，步驟：
#
# 1. 在 A 圖時全記憶體掃「值等於 A 的場景編號」的 4 位元組位址（675 MB 約 0.8 秒，
#    當時 9,289,591 個候選裡值為 2 的有 117,177 個）。
# 2. 請使用者換到 B 圖，把候選過濾成「現在等於 B 的編號」→ 只剩 7 個。
# 3. 再換到 C 圖過濾一次 → 剩 2 個。被刷掉的那 5 個是一整片位元組 0x1E 的圖資
#    緩衝，不是純量欄位。
# 4. 對存活的位址往前找 vtable（往前逐格看有沒有一個值指向 angel.dat 內、而且它
#    指的地方擺著一整排 angel.dat 的函式位址）→ 找到物件起點與 OFF_SCENE_ID。
# 5. 對**物件起點**（不是欄位位址）做指標掃描，深度 1 就找到那個全域指標。
#    ⚠ 直接對欄位位址掃是找不到的（深度 5 掃出來 40 條全是 +0x57、+0x3EE 這種
#      未對齊的巧合鏈）—— 要先往前找到物件起點再掃。
