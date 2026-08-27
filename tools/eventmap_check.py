"""活動地圖（暴走穗海農場）離線測試 —— 不碰遊戲、不碰 Qt。

    py tools\\eventmap_check.py      （全 PASS 印 OK，有 FAIL 結束碼 1）

驗兩件事：

① **支流空間的場景編號**（`scene.split` / `map_key` / `scene_name`）
   使用者回報「在新地圖記錄巡邏點會變成場景 197049」。真相是高 16 位＝支流序號：
   197049 = (3 << 16) | 441 ＝暴走穗海農場分流4。折不掉的話 `same_map` 一定回 False，
   巡邏點、回程補給、死亡回程全部失效。

② **回程路線的資料還對不對**（直接對 `GAMEDATA/`）
   `eventmap.ROUTES` 裡的 NPC 編號／座標／要按第幾項，全部是從遊戲資源包讀出來的。
   官方改一次活動選單，那幾個數字就會安靜地錯掉（人跑去天使學園亂點一通）。
   所以這裡**逐項回頭對 XML**：對不上就大聲 FAIL，而不是等實機把角色點到別的地圖。

⚠ 活動結束、`ROUTES` 清空之後，②那一段會自己跳過（印「沒有登記的活動地圖」），
  不會變成一直 FAIL 的雜訊。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import eventmap, jumpmap, scene, supply    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SETTING = ROOT / "GAMEDATA" / "setting"
STR_NPC_BASE = 1200000000
STR_STAGE_BASE = 1290000000
FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


def _read(rel: str) -> str:
    return (SETTING / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
print("① 支流空間的場景編號（高 16 位 = 分流）")
check("197049 拆成 (441, 3)", scene.split(197049) == (441, 3),
      f"實得 {scene.split(197049)}")
check("65977 拆成 (441, 1)", scene.split(65977) == (441, 1),
      f"實得 {scene.split(65977)}")
check("一般地圖不受影響", scene.split(123) == (123, 0) and scene.subspace(6) == 0)
check("None 不會炸", scene.split(None) == (None, 0)
      and scene.scene_name(None) == "未知")
check("分流之間算同一張圖", scene.same_map(65977, 197049))
check("分流跟本流算同一張圖", scene.same_map(65977, 441))
check("不同圖還是不同圖", not scene.same_map(65977, 123))
check("天使學園 41/141/241 仍然算同一張", scene.same_map(141, 41)
      and scene.same_map(241, 41))
check("名字帶分流", scene.scene_name(197049) == "暴走穗海農場分流4",
      f"實得 {scene.scene_name(197049)}")
check("本流不加分流字樣", scene.scene_name(441) == "暴走穗海農場",
      f"實得 {scene.scene_name(441)}")
check("表裡沒有的編號照樣顯示（不編故事）",
      scene.scene_name(9999) == "場景 9999")

print()
print("② 對話選單第 N 項 → talkaction 碼")
check("第 1 項 = 10（跟 TALK_BUY 同一個）",
      supply.talk_option(1) == supply.TALK_BUY == 10)
check("第 2 項 = 11（跟「我要用倉庫」同一個）",
      supply.talk_option(2) == supply.TALK_BANK_USE == 11)
check("第 4 項 = 13", supply.talk_option(4) == 13)

print()
print("③ 場景名表跟資源包一致（build_scene_names.py --check）")
try:
    ids = {int(m) for m in
           re.findall(r'<場景 編號="(\d+)"', _read("base/stage.xml"))}
    text = {int(s) - STR_STAGE_BASE: n for s, n in
            re.findall(r'編號="(\d+)" 文字1="([^"]*)"',
                       _read("big5/string/str_stage.xml"))}
    want = {i: text[i] for i in ids if i in text}
    bad = {i: (scene.SCENE_NAMES.get(i), want[i])
           for i in want if scene.SCENE_NAMES.get(i) != want[i]}
    check(f"{len(want)} 張圖的名字全對", not bad,
          f"對不上 {len(bad)} 筆：{list(bad.items())[:3]}　→ 跑 "
          "py tools\\build_scene_names.py")
except FileNotFoundError as exc:
    print(f"  － 跳過（沒有 GAMEDATA：{exc}）")

print()
if not eventmap.ROUTES:
    print("④ 沒有登記的活動地圖（活動結束了）—— 回程一律走趴趴GO，跳過")
else:
    print(f"④ 登記的活動地圖 {len(eventmap.ROUTES)} 筆：逐筆回頭對 GAMEDATA")
for route in eventmap.ROUTES:
    print(f"  ── {route.name}（場景 {route.scene}）")
    # 認得出自己這張圖（含任何一條分流）
    for sub in range(route.subspaces):
        raw = (sub << scene.SUBSPACE_SHIFT) | route.scene
        if eventmap.for_scene(raw) is not route:
            check(f"分流{sub + 1} 認得出來", False, f"raw={raw}")
            break
    else:
        check(f"{route.subspaces} 條分流的編號都認得出來", True)
    check("一般地圖不會被誤認成活動地圖",
          eventmap.for_scene(6) is None and eventmap.for_scene(123) is None)
    check("一般地圖照舊走趴趴GO（傳送表還在）",
          jumpmap.nearest(6, 10, 137) is not None)

    try:
        # NPC 名字（str_npc.xml）
        npcs = {int(s) - STR_NPC_BASE: n for s, n in
                re.findall(r'編號="(\d+)" 文字1="([^"]*)"',
                           _read("big5/string/str_npc.xml"))}
        check(f"NPC {route.npc_id} 就是「{route.npc_name}」",
              npcs.get(route.npc_id) == route.npc_name,
              f"資源包寫的是 {npcs.get(route.npc_id)!r}")
        # 擺放位置（SP*.XML 活動 NPC 佈置檔；這種 NPC 不在 map/*.MPC 裡）
        placed = []
        for f in (SETTING / "base").glob("[Ss][Pp]*.[Xx][Mm][Ll]"):
            for m in re.finditer(r'<npc id="(\d+)"[^>]*?map="(\d+)" '
                                 r'x="(\d+)" y="(\d+)"',
                                 f.read_text(encoding="utf-8")):
                if int(m.group(1)) == route.npc_id:
                    placed.append((int(m.group(2)), int(m.group(3)),
                                   int(m.group(4)), f.name))
        want = (route.npc_scene, route.npc_pos[0], route.npc_pos[1])
        check(f"{route.npc_name}擺在 {scene.scene_name(route.npc_scene)}"
              f" {route.npc_pos}",
              any(p[:3] == want for p in placed),
              f"資源包裡擺在 {[p[:3] for p in placed]}"
              if placed else "資源包裡完全找不到這隻（活動下架了？）")
        # 對話選單：根對話 → menu_path → 五條分流各自送 動作37(場景, 分流序號)
        spmsg = _read("base/spmsg.xml")
        # ⚠ `[^>]*?` 不准跨過 `>`：`<對話 …>` 底下還有 `<選項 …/>`，
        #   用 `.*?/>` 會停在第一個 `<選項/>` 上，整個節點就抓不到。
        nodes = {int(m.group(1)): (m.group(2) or "") for m in
                 re.finditer(r'<對話 編號="(\d+)"[^>]*?(?:/>|>(.*?)</對話>)',
                             spmsg, re.S)}
        # 根對話編號 = SP 檔的 msgid
        roots = [int(m.group(2)) for f in (SETTING / "base")
                 .glob("[Ss][Pp]*.[Xx][Mm][Ll]")
                 for m in re.finditer(r'<npc id="(\d+)"[^>]*?msgid="(\d+)"',
                                      f.read_text(encoding="utf-8"))
                 if int(m.group(1)) == route.npc_id]
        root = roots[0] if roots else None
        node = root
        trail = []
        for pick in route.menu_path:
            body = nodes.get(node, "")
            opts = re.findall(r'<選項 訊息="\d+" 下一句="(\d+)"/>', body)
            node = int(opts[pick - 1]) if len(opts) >= pick else None
            trail.append(node)
            if node is None:
                break
        check(f"選單路徑 {route.menu_path} 走得到「選分流」那一頁",
              node is not None, f"走到 {trail}（根對話 {root}）")
        if node is not None:
            body = nodes.get(node, "")
            opts = [int(o) for o in
                    re.findall(r'<選項 訊息="\d+" 下一句="(\d+)"/>', body)]
            check(f"那一頁剛好 {route.subspaces} 個分流選項",
                  len(opts) == route.subspaces, f"實得 {len(opts)} 個")
            got = []
            for i, nxt in enumerate(opts[:route.subspaces]):
                act = re.search(r'<動作 編號="(\d+)">\s*<參數 數值="(\d+)"/>'
                                r'\s*<參數 數值="(\d+)"/>',
                                nodes.get(nxt, ""))
                got.append(None if not act else
                           (int(act.group(1)), int(act.group(2)),
                            int(act.group(3))))
            want = [(37, route.scene, i) for i in range(route.subspaces)]
            check("每個分流選項都送「動作37(場景, 分流序號)」",
                  got == want, f"實得 {got}")
        # 等級門檻（進圖那一頁的條件；只提醒，不當硬性）
        check(f"有記等級門檻 Lv{route.level_min}", route.level_min > 0,
              "沒設＝進不去時只能等失敗訊息")
    except FileNotFoundError as exc:
        print(f"  － 跳過資源包比對（{exc}）")

print()
print("⑤ 掛機分頁的接線（offscreen Qt ＋ 假遊戲層，跑的是真的 farm_tab）")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import types                                            # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

APP = QApplication.instance() or QApplication([])
from app.tabs import farm_tab                           # noqa: E402


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


class InlineThread:
    """背景執行緒改成同步跑，測試才是決定性的（跟 train_check 同一招）。"""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)

    def is_alive(self):
        return False


class FakeSupply:
    """只換 I/O：記下每趟 run_full_supply 收到什麼，其他照真的跑。"""

    SHOP_TABLE = {4836: (2, 30)}

    def __init__(self):
        self.trips = []

    def shop_sells(self, tid):
        return True

    def run_full_supply(self, mv, sc, say=None, back_to=None, potions=None,
                        potion_only=False, ledger=None):
        self.trips.append({"back_to": back_to})
        if say:
            say("測試補給中…")
        return True, "測試補給完成"


SUPPLY = FakeSupply()
GOBACK: list[int] = []          # eventmap.go_back 被叫到時記下 scene_id
JUMPED: list[int] = []          # jumpmap.teleport 被叫到時記下 jump_id
farm_tab.supply = SUPPLY
farm_tab.threading = types.SimpleNamespace(Thread=InlineThread)
# ⚠ 只換「會碰到遊戲」的那兩支，其他（for_scene / ROUTES / nearest / label）
#   全部跑真的 —— 換掉整個模組就會變成在測替身（memory test-via-button）。
eventmap.go_back = (lambda mv, sc, sid, say=None:
                    (GOBACK.append(sid), (True, "已進到暴走穗海農場分流2"))[1])
jumpmap.teleport = (lambda mv, sc, jid:
                    (JUMPED.append(jid), (True, "已送出傳送"))[1])
# 背包／精靈那兩支讀的是真記憶體，這裡給決定性的答案（有翼、不買藥水）
farm_tab.robot.has_recall_item = lambda sc, inv: (5, 30)
farm_tab.robot.potion_buy_ids = lambda mv, sc, pid: None

EVENT_SPOT = (45.0, 16.0, (1 << scene.SUBSPACE_SHIFT) | 441)   # 分流2 的巡邏點
PLAIN_SPOT = (10.0, 137.0, 6)                                   # 穗海農場


def build_page():
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account="acct", char_name="小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.cur_scene = lambda: EVENT_SPOT[2]
    page.my_pos = lambda: (EVENT_SPOT[0], EVENT_SPOT[1])
    page.inv = 0x1000
    page._sync_castwatch = lambda: None
    page._keys.begin_learning = lambda: None
    page._buff.armed = True
    page._summon.armed = True
    page.notify = lambda msg: None
    page._drop_cached_addrs = lambda: None
    return page


if not eventmap.ROUTES:
    print("  － 沒有登記的活動地圖，跳過")
else:
    ROUTE = eventmap.ROUTES[0]

    page = build_page()
    page._spots = [EVENT_SPOT]
    page._test_supply()
    check("🧪 測試鈕真的開了一趟補給", len(SUPPLY.trips) == 1,
          f"實得 {SUPPLY.trips}")
    check("回程目標＝活動地圖那個巡邏點（含分流序號）",
          SUPPLY.trips and SUPPLY.trips[0]["back_to"] == EVENT_SPOT,
          f"實得 {SUPPLY.trips[0]['back_to'] if SUPPLY.trips else None}")
    check("跑完自己收工（_supply_tick 收得到結果）",
          page._supply_tick(0.1) is True and page._supply is False)

    page = build_page()
    page._death_scene = EVENT_SPOT[2]
    GOBACK.clear()
    page._death_event_return(ROUTE)
    check("死亡回程走活動 NPC（不是趴趴GO）", GOBACK == [EVENT_SPOT[2]],
          f"實得 {GOBACK}")
    check("結果收得回來、不會卡著",
          page._death_event_return(ROUTE) is True
          and page._death_ev_result is None)
    check("活動地圖的死亡回程逾時放寬",
          farm_tab.DEATH_EVENT_MAX > farm_tab.DEATH_WAIT_MAX)

    page = build_page()
    page._spots = [EVENT_SPOT]
    GOBACK.clear()
    page._fly_to_spot(0)
    check("巡邏點右鍵：活動地圖走 NPC 對話", GOBACK == [EVENT_SPOT[2]],
          f"實得 {GOBACK}")
    check("飛過去＝記錄點跟著改", page._home == EVENT_SPOT,
          f"實得 {page._home}")
    check("走完把結果收掉（_evgo_tick 不會永遠回 True）",
          page._evgo_tick() is False and page._evgo_result is None)

    page = build_page()
    page._spots = [PLAIN_SPOT]
    GOBACK.clear()
    JUMPED.clear()
    page._fly_to_spot(0)
    check("一般地圖的巡邏點**不會**被導去活動 NPC", GOBACK == [],
          f"實得 {GOBACK}")
    check("一般地圖照舊送趴趴GO",
          JUMPED == [jumpmap.nearest(PLAIN_SPOT[2], PLAIN_SPOT[0],
                                     PLAIN_SPOT[1]).jump_id],
          f"實得 {JUMPED}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
