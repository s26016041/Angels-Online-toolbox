"""活動限定地圖：**趴趴GO 到不了的圖，怎麼自己走回去**。

一般練功圖的回程是趴趴GO 送一包就到（`jumpmap.teleport`）。活動地圖不是：
它們不在趴趴GO 的傳送表裡，唯一的入口是**在某張城鎮圖上找一隻活動 NPC 講話、
在選單裡挑「帶我進去」**。補給一趟（天使之翼回城 → 存倉 → 修裝 → 買）跑完之後，
`supply.run_full_supply` 會發現「這張圖沒有回程傳送點」，就把人丟在城裡 ——
這支模組就是補上那一段。

★★★ 活動結束＝**把 `ROUTES` 裡那一筆刪掉就好**（整個檔留著沒差）。
  刪掉之後 `for_scene()` 一律回 None，所有呼叫端自動退回原本的行為
  （＝「沒有回○○的傳送點」大聲停下），不會有半條路走到一半沒人接。

## 目前登記的活動（2026-08-27）

「暴走穗海農場」（場景 441，2026-08-25 改版加的啤酒節活動）
  · 入口 NPC ＝ **啤酒節使者 14897**，站在**天使學園（41）(170, 90)**。
    出處 `GAMEDATA/setting/base/SP2026_08_25_NPC.XML`：
      `<npc id="14897" msgid="516843" map="41" x="170" y="90" .../>`
    ⚠ 這隻**不在 `map/MAP041.MPC` 裡**（`npc.xml` 標著 特殊活動="是"，
      是伺服器擺的），所以 `build_supply_merchants.py` 那條路抽不到它 ——
      座標只好抄這個 SP 檔。歷來活動 NPC 都擺在同一區（x 141~203、y 76~106，
      跟 .MPC 抽出來的邱比特(159,77)、天使班導師(179,92) 同一塊廣場），
      座標系一致。⚠ 座標只當「走近一點讓 NPC 串流進來」的錨，真正認人靠
      `supply.find_npc()` 讀實體 +0x1D8 的編號，走到旁邊就會用真座標修正。
  · 對話（`GAMEDATA/setting/base/spmsg.xml`，根對話編號 = SP 檔的 msgid 516843）：
      516843 第 4 項「關於「暴走穗海農場」(推薦Lv100+)」→ 2023144
      2023144 第 1 項「正好！帶我去「暴走穗海農場」吧！」  → 2023114
      2023114 第 1~5 項「暴走穗海農場分流1~5」            → 2023126~2023130
      2023126~2023130 各自送 `動作 37, 參數 =(441, 0..4, 1)`
      ＝ 場景 441 + 支流序號（見 `scene.split()`）。
    → 所以要按的是**第 4 項 → 第 1 項 → 第 (支流序號+1) 項**。
  · 等級門檻：2023114 掛著條件（不到 Lv100 就跳去 2023115 那句警告，
    訊息 517675「你的等級還沒達到 Lv100…」）→ 我們自己先擋，
    免得對著警告視窗一直重送。

## 封包實證（使用者 2026-08-27 19:27 手動走一遍的擷取：`封包/去暴走穗海農場.txt`）

14 包，用返回位址對回已定位的函式（當下 `locate.warm()` 的值）：

    #0  move.MOVE_FN(0x558F97)    走過去
    #1  attack.ACTION_FN(0x5D7F6E) 代號 0x07 內文 3   ＝面向 NPC（點 NPC 的副產品）
    #2  attack.THIRD_FN(0x558F0F)  代號 0x05 內文 8
    #3  0x5D6C5D 代號 0x0F 內文 10 ＝防外掛心跳（見 memory mall-and-ball-swap）
    #4  sell.TALK_FN(0x5D7FA7)     代號 0x0B 內文 3   ★ talkaction
    #5  team.ACTION_FN(0x5D29D5)   代號 0x18 內文 7
    #6  sell.TALK_FN                                  ★ talkaction
    #7  心跳 0x0F
    #8  sell.TALK_FN                                  ★ talkaction
    #9~#13 傳送之後的載圖收尾（0x143 / 0x02 / 0x03 / 0x15E / 0x16）

→ **整段互動就是「點 NPC」＋ 三個 talkaction（0x0B）**，跟上面從 spmsg.xml 推的
  「第4項 → 第1項 → 第(分流)項」完全一致，中間那幾包是心跳／面向，不必補送。
  所以這裡用的就是買/修/銀行那條已經實機驗過的路（`supply._engage_npc`），
  **沒有任何新位址、沒有新封包版面** —— 改版位移由 supply/jumpmap 的 AOB 吸收。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.game import jumpmap, player, scene, supply

# 走到 NPC 那張圖：趴趴GO 送出去 ≠ 到得了（memory jump-back-channel-fix）
JUMP_TRIES = 3
JUMP_WAIT = 12.0
# 選完分流之後等地圖真的變（載圖要幾秒）；等不到＝這一輪沒成功
ENTER_WAIT = 20.0
# 「靠近 → 點 → 沒開就換站位再點」最多幾輪（交給 supply._engage_npc 自己數）。
# ★ 至少要 4：第 1 輪是走近＋點，之後每輪才輪到 NUDGE_STEPS 那三階
#   （踩上他本格 → 穿過去 1.5 格 → 3 格），人牆就是靠那幾階擠進去的。
ENTER_TRIES = 5


@dataclass(frozen=True)
class EventMap:
    """一張活動地圖的進入方式。"""

    scene: int                     # 活動地圖的場景編號（低 16 位，不含支流序號）
    name: str                      # 顯示用（scene_name 也查得到，這裡留一份好讀）
    npc_id: int                    # 入口 NPC 編號（實體 +0x1D8）
    npc_name: str                  # 入口 NPC 名字（str_npc.xml，只給訊息用）
    npc_scene: int                 # NPC 站在哪張圖
    npc_pos: tuple[int, int]       # NPC 大概的格子座標（只當走近的錨點）
    menu_path: tuple[int, ...]     # 進到「選分流」那一頁之前要按第幾項（1 起算）
    subspaces: int                 # 選單上有幾條分流
    level_min: int = 0             # 等級門檻（0＝沒有）

    def label(self, sub: int = 0) -> str:
        return f"{self.name}分流{sub + 1}"


# ---------------------------------------------------------------------------
# ★★★ 活動結束就刪掉這一筆（連同上面檔頭那段說明）。
# ---------------------------------------------------------------------------
ROUTES: tuple[EventMap, ...] = (
    EventMap(
        scene=441,
        name="暴走穗海農場",
        npc_id=14897,
        npc_name="啤酒節使者",     # str_npc.xml 編號 1200014897
        npc_scene=41,              # 天使學園
        npc_pos=(170, 90),
        menu_path=(4, 1),          # 關於「暴走穗海農場」→ 帶我去
        subspaces=5,               # 分流1~5
        level_min=100,
    ),
)


def for_scene(scene_id: int | None) -> EventMap | None:
    """這個場景編號是不是登記過的活動地圖？不是就回 None（呼叫端照舊）。

    ⚠ 用 `scene.map_key` 比對 —— 傳進來的通常是帶支流序號的 raw 編號
      （暴走穗海農場分流2 ＝ 65977）。
    """
    key = scene.map_key(scene_id)
    if key is None:
        return None
    return next((r for r in ROUTES if scene.map_key(r.scene) == key), None)


def _here(scanner) -> int | None:
    """目前場景編號（不做 0.3 秒全掃備援，這裡每 0.1 秒就問一次）。"""
    return scene.current_id(scanner, allow_scan=False)


def _level(scanner) -> int | None:
    """角色等級；讀不到回 None（→ 不擋，安全退化成照樣去試）。

    ⚠ 走 `player.locate()`（捷徑失敗會全掃 ~0.2 秒）—— 這支只在背景執行緒被叫，
      而「剛換過地圖所以捷徑對不上」正是最需要它答對的時候。
    """
    try:
        base = player.locate(scanner)
        st = player.read(scanner, base) if base else None
        return None if st is None else int(st.level)
    except Exception:                                      # noqa: BLE001
        return None


def _fly_to_npc_map(mover, scanner, route: EventMap, note) -> tuple[bool, str]:
    """趴趴GO 到 NPC 站的那張城鎮圖。已經在那張圖就直接回 True。"""
    here = _here(scanner)
    if scene.same_map(here, route.npc_scene):
        return True, "已經在" + scene.scene_name(route.npc_scene)
    e = jumpmap.nearest(route.npc_scene, route.npc_pos[0], route.npc_pos[1])
    if e is None:
        return False, f"⚠ 趴趴GO 沒有去{scene.scene_name(route.npc_scene)}的傳送點"
    for _ in range(JUMP_TRIES):
        ok, _msg = jumpmap.teleport(mover, scanner, e.jump_id)
        note(f"飛去{scene.scene_name(route.npc_scene)}找{route.npc_name}…")
        if ok:
            t0 = time.time()
            while time.time() - t0 < JUMP_WAIT:
                if scene.same_map(_here(scanner), route.npc_scene):
                    time.sleep(1.0)            # 落地穩一下再開始走
                    return True, "已到" + scene.scene_name(route.npc_scene)
                time.sleep(0.3)
        else:
            time.sleep(1.0)                    # 送不出去（槽忙／正在重連）→ 再試
    return False, (f"⚠ 趴趴GO 送了 {JUMP_TRIES} 次都沒到"
                   f"{scene.scene_name(route.npc_scene)}")


def enter(mover, scanner, route: EventMap, sub: int = 0,
          say=None) -> tuple[bool, str]:
    """走去入口 NPC、講話、進活動地圖。回 (成功了嗎, 說明)。

    `sub` ＝ 想進第幾條分流的**支流序號（0 起算）**；選單上顯示成「分流 sub+1」。
    超出選單範圍就夾回去（伺服器只開這麼多條）。

    ⛔⛔ **不准自己先用地形圖走到 NPC 那一格**（2026-08-27 使用者實機回報：
      「人太多會走不到」）。第一版就是這樣寫的，而 `run_full_supply` 早就寫著
      同一個坑：「各步驟**不在這裡先用地形圖 _walk_to_npc**（它在 NPC 櫃檯區
      會誤判走不到）」。天使學園廣場整天圍滿人，那條路只會磨到逾時。
      → 走近／點擊／點不開換站位**全部交給 `supply.talk_to_npc`**，
        跟藥水雜貨商人同一套（0x54A520 自己走最後一段、不開就往他身上靠）。
    """
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if scene.same_map(_here(scanner), route.scene):
        return True, f"已經在{route.name}"

    sub = max(0, min(int(sub), route.subspaces - 1))
    lv = _level(scanner)
    if route.level_min and lv is not None and lv < route.level_min:
        # ⛔ 進不去就別開始：對著「等級不足」的警告一直重送選項只會空轉，
        #    而且中間人會被對話鎖住不能走。大聲說原因。
        return False, (f"⚠ 角色 Lv{lv} 進不了{route.name}"
                       f"（要 Lv{route.level_min} 以上）")

    ok, msg = _fly_to_npc_map(mover, scanner, route, note)
    if not ok:
        return False, msg

    # 對話碼：先走到「選分流」那一頁，最後一項才是分流本身。
    codes = [supply.talk_option(n) for n in route.menu_path]
    codes.append(supply.talk_option(sub + 1))
    note(f"走去找{route.npc_name}，選「{route.label(sub)}」…")
    if supply.talk_to_npc(mover, scanner, route.npc_id, route.npc_pos, codes,
                          lambda: scene.same_map(_here(scanner), route.scene),
                          ENTER_WAIT, tries=ENTER_TRIES):
        time.sleep(1.0)                        # 落地穩一下再交回去
        return True, f"已進到{scene.scene_name(_here(scanner))}"

    # ⚠ 失敗收尾一定要送「離開 NPC」：對話開著角色被伺服器鎖住不能走
    #   （見 supply.leave_npc），不送的話人會定在 NPC 旁邊動不了。
    supply.leave_npc(mover)
    # ★ 失敗原因分兩種講清楚（講錯方向會害人往錯的地方查）：
    #   看得到他＝對話沒吃到／等級不夠；看不到他＝活動下架或被移走。
    if supply.find_npc(scanner, route.npc_id) is None:
        return False, (f"⚠ 附近看不到{route.npc_name}（NPC {route.npc_id}）——"
                       f"活動結束了？還是他不在 {route.npc_pos} 了？"
                       "（位置出處：GAMEDATA/setting/base/SP*_NPC.XML）")
    return False, (f"⚠ 找到{route.npc_name}了，但點了 {ENTER_TRIES} 次都沒進到"
                   f"{route.name}（人還在{scene.scene_name(_here(scanner))}"
                   f"，離他 {supply._dist_to_npc(scanner, route.npc_id)} 格）")


def go_back(mover, scanner, scene_id: int, say=None) -> tuple[bool, str]:
    """回到 `scene_id` 那張活動地圖（含原來那條分流）。

    給 `supply.run_full_supply` 的回程那一步用：一般圖走趴趴GO，
    登記過的活動地圖走這裡。
    ★ 分流：回**原來記錄點那條**（`scene.subspace` 從記錄的編號拆出來），
      這樣巡邏點旁邊的怪、其他玩家的分布跟出發前一樣。
    """
    route = for_scene(scene_id)
    if route is None:
        return False, f"{scene.scene_name(scene_id)}不是登記過的活動地圖"
    return enter(mover, scanner, route, scene.subspace(scene_id), say=say)
