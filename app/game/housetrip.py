"""房屋製作：去**自己小屋的生產寵物房**、沒擺房子就自己擺、用屋內保險箱存東西。

自動生產分頁勾「房屋製作」時用（使用者 2026-09-25 定的流程，memory `house-location`）：

    房子沒展示 → 飛棕櫚基地 → 挑「可擺房屋格」（assets/house_zones.json）裡離看得到的
                 別人房子最遠、走得到的格 → 站上去按「展示」→ 沒擺上去就換格，試滿 2 分鐘
    → 趴趴GO 到房子那張圖 → 走到房子旁 → 進門 → 踩屋裡的傳送點 → 選單「要去哪裡呢？」
      選第 1 項「生產寵物房」→ 角色瞬移到製作間（**場景編號不變**，看位置跳了才算到）

保險箱（屋內「木色寶箱」）：點它 → 頁 7862 選「開啟保險箱」(11) → 頁 7868 選
「開啟個人倉庫」(10) 或「開啟社團倉庫」(11) → WND_BANK 開、CUR_BANK_TYPE 0/1。
（2026-09-25 雪狐實機全段走過，見 memory。）

★ 這裡的函式全部是**阻塞式**的（會 sleep），只能在背景執行緒叫 —— 分頁的 UI 拍子
  只負責輪詢結果（跟 supply.run_full_supply 同一套，見 produce_tab）。
★ 全程寫 `house_run.log`（%APPDATA%\\AngelsOnlineToolbox\\），出問題先看它。
  每一行：時間 角色 [場景] (座標) 內容。
★ 不換頻道（使用者：換頻遊戲會強制把房子收起來）。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from app.config import config
from app.game import (entity, house, jumpmap, lua, mapobj, move, navigate, produce,
                      scene, scenery, supply, talkwnd, terrain)
from app.paths import resource

RUNLOG_NAME = "house_run.log"
RUNLOG_MAX = 4 * 1024 * 1024     # 超過就砍掉重寫（紀錄不能無限長）

ZONE_FILE = "assets/house_zones.json"   # tools/build_house_zones.py 抽的（GAMEDATA .mpc）
PLACE_MAP = "棕櫚基地"            # ★ 使用者定：沒房子一律擺在棕櫚基地
PLACE_SECS = 120.0               # ★ 使用者定：擺不上去要一直換位置試滿 2 分鐘才放棄
PLACE_WAIT = 3.0                 # 送展示後等「展示中」變 1 的時間（實測 0.5 秒就變）
PLACE_GAP = 6.0                  # 優先挑離看得到的別人房子這麼遠的格（實測 4.1 格也擺得下）
PLACE_TRIED_R = 3                # 一格擺不上去，它周圍這麼多格也先不試

JUMP_WAIT = 15.0                 # 趴趴GO 後等換圖
STEP_TRIES = 3                   # 每一段（傳送／進門／踩傳送點／開保險箱）最多試幾次
WALK_SECS = 90.0                 # 走一段路的兜底
WALK_STALL = 12.0                # 這麼久沒有更靠近就當走不到
HOUSE_NEAR = 5.0                 # 離房子這麼近就送進門（分身總控實測可進）
HOUSE_GOAL_RELAX = 8             # 房子那格不可走時，往外找可站格的半徑
ENTER_WAIT = 8.0                 # 送進門後等場景變「房屋」
MENU_WAIT = 6.0                  # 踩上傳送點後等選單
WARP_WAIT = 6.0                  # 選「生產寵物房」後等瞬移
WARP_JUMP = 8.0                  # 位置一次跳超過這麼多格＝瞬移了
PAGE_WAIT = 4.0                  # 點保險箱／送選項後等下一頁
BANK_WAIT = 4.0                  # 選倉庫後等倉庫視窗
REACH = 2.5                      # 點物件前要離它多近（伺服器 ~3.7 格內才受理，同 produce_tab.BENCH_REACH）
CHEST_TRIES = 10                 # 找保險箱最多點幾個候選

# 對話頁的訊息編號（GAMEDATA big5/string/msg.xml；2026-09-25 實機讀到的 MESSAGE_MSG_ID）。
# ⚠ 對不上＝不是我們以為的那一頁 → 不送選項（安全退化），不是照送。
MENU_MSG = 7888                  # 「要去哪裡呢？」1 生產寵物房／2 戰鬥怪物房／3 離開小屋
MENU_WORKSHOP = 1
CHEST_MSG = 7862                 # 「你好！有任何物品都可以交給我保管喔！」1 關於／2 開啟保險箱／3 離開
CHEST_OPEN = 2
CHEST_PICK_MSG = 7868            # 「要開啟哪個倉庫？」1 個人／2 社團／3 離開
BANK_TYPE_SELF = 0               # 實測開個人倉 CUR_BANK_TYPE==0（公會倉是 guildbank.BANK_TYPE_GUILD==1）
BANK_TYPE_GUILD = 1

CFG_MARKS = "house.workshop_models"   # 製作間裡看得到的物件外觀（第一次走到時自己學）
CFG_CHEST = "house.chest_model"       # 保險箱的外觀（第一次開成功時自己學）
CFG_WHERE = "multi.houses"            # 上次看到的各角色小屋 {角色:[地圖,x,y]}（跟分身總控共用）
MARKS_MIN = 3                         # 至少看到這麼多個學過的外觀才算在製作間


# ---------------------------------------------------------------------------
# 執行紀錄
# ---------------------------------------------------------------------------
class RunLog:
    """house_run.log。開不了就算了（紀錄不能影響功能）。"""

    def __init__(self, who: str) -> None:
        self.who = who
        self._f = None
        try:
            from app.core.crashlog import log_dir
            p = log_dir() / RUNLOG_NAME
            if p.exists() and p.stat().st_size > RUNLOG_MAX:
                p.unlink()
            self._f = p.open("a", encoding="utf-8")
        except Exception:                                  # noqa: BLE001
            self._f = None

    def w(self, sc, text: str) -> None:
        if self._f is None:
            return
        try:
            sid = scene.current_id(sc) if sc is not None else None
            pos = _pos(sc) if sc is not None else None
            p = f"({pos[0]:.1f},{pos[1]:.1f})" if pos else "(?)"
            self._f.write(f"{time.strftime('%m-%d %H:%M:%S')} {self.who} "
                          f"[{sid}] {p} {text}\n")
            self._f.flush()
        except Exception:                                  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            if self._f is not None:
                self._f.close()
        except Exception:                                  # noqa: BLE001
            pass
        self._f = None


@dataclass
class Ctx:
    """一趟背景工作的環境。`cancel` 由分頁設 True（取消勾選／關分頁）。"""

    mover: object
    sc: object
    owner: str                   # 角色名＝屋主名
    log: RunLog
    say: object = None           # callable(str)：進度給狀態列
    cancel: bool = False
    maps: terrain.Cache = field(default_factory=terrain.Cache)

    def note(self, text: str) -> None:
        self.log.w(self.sc, text)
        if self.say is not None:
            try:
                self.say(text)
            except Exception:                              # noqa: BLE001
                pass

    def alive(self) -> bool:
        return not self.cancel


class Cancelled(Exception):
    pass


def _nap(ctx: Ctx, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if ctx.cancel:
            raise Cancelled()
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _pos(sc):
    try:
        pf = move.pathfinder_this(sc)
    except Exception:                                      # noqa: BLE001
        return None
    return entity.player_pos(sc, pf + 8) if pf else None


def _player(sc) -> int | None:
    try:
        pf = move.pathfinder_this(sc)
    except Exception:                                      # noqa: BLE001
        return None
    return pf + 8 if pf else None


_zones: dict | None = None


def zone_runs(sid: int | None) -> list | None:
    """這張圖的可擺房屋格（[[y,x0,x1],...]）；表沒有這張圖／讀不到 → None。"""
    global _zones
    if _zones is None:
        try:
            _zones = json.loads(resource(ZONE_FILE).read_text("utf-8"))
        except Exception:                                  # noqa: BLE001
            _zones = {}
    base = scene.base_id(sid)
    rec = _zones.get(str(base)) if base is not None else None
    return rec.get("runs") if rec else None


def _remember_where(owner: str, own: house.Own) -> None:
    raw = dict(config.get(CFG_WHERE, {}) or {})
    val = [own.map_name, own.x, own.y]
    if raw.get(owner) != val:
        raw[owner] = val
        config.set(CFG_WHERE, raw)
        config.save()


def _last_where(owner: str) -> house.Own | None:
    v = (config.get(CFG_WHERE, {}) or {}).get(owner)
    try:
        return house.Own(str(v[0]), int(v[1]), int(v[2])) if v else None
    except (TypeError, ValueError, IndexError):
        return None


def _forget_where(owner: str) -> None:
    raw = dict(config.get(CFG_WHERE, {}) or {})
    if raw.pop(owner, None) is not None:
        config.set(CFG_WHERE, raw)
        config.save()


def _page(sc):
    try:
        return talkwnd.page(sc)
    except Exception:                                      # noqa: BLE001
        return None


def _wait_page(ctx: Ctx, before, secs: float):
    """等「新的一頁」（簽章跟 before 不一樣、而且視窗真的在畫面上）。回那一頁或 None。"""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        _nap(ctx, 0.2)
        pg = _page(ctx.sc)
        if pg is None:
            continue
        if before is not None and pg.sig == before.sig:
            continue
        if talkwnd.window_visible(ctx.sc) is False:
            continue
        return pg
    return None


def _msg_of(pg) -> int | None:
    try:
        return int(pg.sig[2]) if pg is not None and pg.sig[2] is not None else None
    except (TypeError, ValueError):
        return None


def _settle(ctx: Ctx, secs: float = 3.0) -> None:
    """等角色停下來再點東西 —— 還在走的時候點，伺服器那邊的位置跟客戶端差一截，
    會被距離檢查擋掉而**完全沒反應**（2026-09-25 雪狐實錄：走到 2.3 格就點寶箱，沒開）。"""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        obj = _player(ctx.sc)
        if obj and not entity.is_walking(ctx.sc, obj):
            break
        _nap(ctx, 0.2)
    _nap(ctx, 0.5)


def _close_talk(ctx: Ctx) -> None:
    """把開著的對話框收掉＋送離開 NPC 包（對話開著角色會被鎖住不能走）。"""
    try:
        if talkwnd.window_visible(ctx.sc):
            talkwnd.close_window(ctx.mover, ctx.sc)
        supply.leave_npc(ctx.mover, ctx.sc)
    except Exception:                                      # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 走路
# ---------------------------------------------------------------------------
def walk(ctx: Ctx, gx: float, gy: float, arrive: float = 0.8,
         secs: float = WALK_SECS) -> tuple[bool, str]:
    """走到離 (gx, gy) arrive 格內。遠的走地形圖 A*，最後一段直走（不尋路）。

    ★ 目標是物件（製作台／保險箱）時那一格常常不能站 → 先換成**最近的可走格**
      當終點，離原目標 arrive 內就算到（直走撞不進物件那格，會原地站到逾時）。
    """
    tx, ty = gx, gy
    grid = ctx.maps.get(ctx.sc)
    if grid is not None and not grid.walkable(int(gx), int(gy)):
        spot = grid.nearest_open(int(gx), int(gy), 4)
        if spot is not None:
            tx, ty = spot[0] + 0.5, spot[1] + 0.5
    nav = navigate.Navigator(ctx.maps)
    t0 = time.monotonic()
    best, since = None, t0
    while True:
        if ctx.cancel:
            raise Cancelled()
        obj = _player(ctx.sc)
        me = entity.player_pos(ctx.sc, obj) if obj else None
        now = time.monotonic()
        if me is None:
            if now - t0 > secs:
                return False, "讀不到角色位置"
            _nap(ctx, 0.3)
            continue
        d = math.hypot(me[0] - gx, me[1] - gy)
        dt = math.hypot(me[0] - tx, me[1] - ty)
        if d <= arrive or dt <= 0.8:
            return True, f"到了（差 {d:.1f} 格）"
        if best is None or dt < best - 0.3:
            best, since = dt, now
        elif now - since > WALK_STALL:
            # 相鄰格遊戲不走（近距尋路回 0）→ 差不到 1.6 格就當到了
            if dt <= 1.6:
                return True, f"差 {dt:.1f} 格走不上去，就站這裡（離目標 {d:.1f} 格）"
            return False, f"{WALK_STALL:.0f} 秒沒有更靠近（還差 {d:.1f} 格）"
        if now - t0 > secs:
            return False, f"走了 {secs:.0f} 秒還沒到（還差 {d:.1f} 格）"
        if dt <= navigate.ARRIVE or nav.exhausted:
            if not entity.is_walking(ctx.sc, obj):
                ctx.mover.walk_exact(ctx.sc, obj, tx, ty)
        else:
            nav.step(ctx.sc, ctx.mover, obj, tx, ty)
            if nav.stuck and nav.stuck_reason == "grid":
                return False, f"地形圖說到不了（{nav.note}）"
        _nap(ctx, 0.4)


# ---------------------------------------------------------------------------
# 擺房子
# ---------------------------------------------------------------------------
def _goto_map(ctx: Ctx, sid: int, x: float | None, y: float | None,
              name: str) -> tuple[bool, str]:
    """趴趴GO 到那張圖（本來就在就不傳）。"""
    for n in range(1, STEP_TRIES + 1):
        if scene.same_map(scene.current_id(ctx.sc), sid):
            return True, "已在那張圖"
        e = jumpmap.nearest(sid, x, y, ctx.sc)
        if e is None:
            return False, f"{name}沒有趴趴GO 傳送點"
        ok, why = jumpmap.teleport(ctx.mover, ctx.sc, e.jump_id)
        ctx.note(f"趴趴GO → {name}（{e.name}）第 {n} 次：{'已送出' if ok else why}")
        end = time.monotonic() + (JUMP_WAIT if ok else 2.0)
        while time.monotonic() < end:
            _nap(ctx, 0.5)
            if scene.same_map(scene.current_id(ctx.sc), sid):
                _nap(ctx, 1.5)                  # 等落地、地形圖換好
                return True, "到了"
    return False, f"趴趴GO {STEP_TRIES} 次都沒到{name}（沒有翔宇聖翼？）"


def _zone_cells(runs, reach) -> list[tuple[int, int]]:
    out = []
    for y, x0, x1 in runs:
        for x in range(x0, x1 + 1):
            if (x, y) in reach:
                out.append((x, y))
    return out


def place(ctx: Ctx) -> tuple[bool, str]:
    """在棕櫚基地擺房子。回 (成功?, 說明)。試滿 PLACE_SECS 才放棄。"""
    sid = house.scene_of(PLACE_MAP)
    if sid is None:
        return False, f"場景表查不到「{PLACE_MAP}」"
    runs = zone_runs(sid)
    if not runs:
        return False, f"可擺房屋表沒有{PLACE_MAP}（重跑 py tools\\build_house_zones.py）"
    ok, why = _goto_map(ctx, sid, None, None, PLACE_MAP)
    if not ok:
        return False, why
    t0 = time.monotonic()
    tried: set[tuple[int, int]] = set()
    n = 0
    while time.monotonic() - t0 < PLACE_SECS:
        if ctx.cancel:
            raise Cancelled()
        if house.shown(ctx.sc):
            return True, "房子已經在展示中"
        if not scene.same_map(scene.current_id(ctx.sc), sid):
            ok, why = _goto_map(ctx, sid, None, None, PLACE_MAP)
            if not ok:
                return False, why
        me = _pos(ctx.sc)
        grid = ctx.maps.get(ctx.sc)
        if me is None or grid is None:
            _nap(ctx, 0.5)
            continue
        reach = grid.reachable(int(me[0]), int(me[1])) or {(int(me[0]), int(me[1]))}
        others = [(h.x, h.y) for h in (house.nearby(ctx.sc) or [])]
        best = None
        for x, y in _zone_cells(runs, reach):
            if (x, y) in tried:
                continue
            dh = min((math.hypot(x + 0.5 - a, y + 0.5 - b) for a, b in others),
                     default=99.0)
            dm = math.hypot(x + 0.5 - me[0], y + 0.5 - me[1])
            # 先挑夠遠的（離別人房子 ≥ PLACE_GAP），同一級裡挑離我近的
            key = (0 if dh >= PLACE_GAP else 1, dm if dh >= PLACE_GAP else -dh)
            if best is None or key < best[0]:
                best = (key, x, y, dh, dm)
        if best is None:
            return False, f"{PLACE_MAP}走得到的可擺格都試過了（{n} 格）"
        _k, x, y, dh, dm = best
        n += 1
        ctx.note(f"擺房子第 {n} 格：({x},{y}) 離我 {dm:.0f} 格、"
                 f"離最近的別人房子 {dh:.1f} 格（看得到 {len(others)} 棟）")
        ok, why = walk(ctx, x + 0.5, y + 0.5, arrive=0.8, secs=60.0)
        if not ok:
            ctx.note(f"　走不到 ({x},{y})：{why} → 換一格")
            _mark(tried, x, y)
            continue
        _nap(ctx, 0.6)                          # 站穩（伺服器看的是它那邊的位置）
        me = _pos(ctx.sc)
        ok, why = house.register(ctx.mover, ctx.sc)
        ctx.note(f"　站在 {me} 送展示：{why}")
        if not ok and house.shown(ctx.sc):
            return True, "房子已經在展示中"
        end = time.monotonic() + PLACE_WAIT
        while time.monotonic() < end:
            _nap(ctx, 0.3)
            if house.shown(ctx.sc):
                own = house.own(ctx.sc)
                if own is not None:
                    _remember_where(ctx.owner, own)
                return True, f"房子擺好了：{own or '（位置還沒讀到）'}"
        ctx.note(f"　({x},{y}) 沒擺上去（太靠近別人房子？）→ 換一格")
        _mark(tried, x, y)
    return False, f"試了 {PLACE_SECS:.0f} 秒、{n} 格都沒擺上去（沒裝備房屋？附近太擠？）"


def _mark(tried: set, x: int, y: int) -> None:
    for dx in range(-PLACE_TRIED_R, PLACE_TRIED_R + 1):
        for dy in range(-PLACE_TRIED_R, PLACE_TRIED_R + 1):
            tried.add((x + dx, y + dy))


# ---------------------------------------------------------------------------
# 進製作間
# ---------------------------------------------------------------------------
def _marks() -> set[int]:
    try:
        return {int(m) for m in (config.get(CFG_MARKS, []) or [])}
    except (TypeError, ValueError):
        return set()


def _visible_models(sc) -> set[int] | None:
    props = scenery.nearby(sc)
    if props is None:
        return None
    return {p.model for p in props if not mapobj.hidden(p.model)}


def in_workshop(sc) -> bool | None:
    """人在自己小屋的生產寵物房（製作間）嗎。None＝判不出來（還沒學過／讀不到）。

    ★ 製作間跟客廳是**同一個場景編號**（瞬移而已），所以看「製作間才有的物件」：
      第一次靠選單瞬移過去時把看得到的外觀學起來（CFG_MARKS），之後看到其中
      MARKS_MIN 個以上就算在。
    """
    if not house.inside(scene.current_id(sc)):
        return False
    marks = _marks()
    if not marks:
        return None
    seen = _visible_models(sc)
    if seen is None:
        return None
    return len(seen & marks) >= MARKS_MIN


def _learn_marks(ctx: Ctx, before: set[int] | None) -> None:
    seen = _visible_models(ctx.sc)
    if not seen:
        return
    marks = seen - (before or set())       # 扣掉客廳也看得到的（傳送點之類）
    if len(marks) >= MARKS_MIN:
        config.set(CFG_MARKS, sorted(marks))
        config.save()
        ctx.note(f"學到製作間的物件外觀 {len(marks)} 種："
                 + "、".join(mapobj.name_of(m) or str(m) for m in sorted(marks)[:8]))


def _portal(sc):
    me = _pos(sc)
    ts = [t for t in (house.things(sc) or []) if t.trigger]
    if me is None or not ts:
        return None
    return min(ts, key=lambda t: t.dist(me))


def _to_workshop(ctx: Ctx) -> tuple[bool, str]:
    """人在屋裡 → 踩傳送點 → 選「生產寵物房」→ 等瞬移。"""
    for n in range(1, STEP_TRIES + 1):
        if in_workshop(ctx.sc):
            return True, "已在生產寵物房"
        t = _portal(ctx.sc)
        if t is None:
            return False, "屋裡找不到傳送點"
        before_models = _visible_models(ctx.sc)
        pg0 = _page(ctx.sc)
        _close_talk(ctx)                       # 身上掛著別的對話會被鎖住不能走
        gx, gy = math.floor(t.x) + 0.5, math.floor(t.y) + 0.5
        ctx.note(f"踩傳送點（第 {n} 次）：({t.x:.1f},{t.y:.1f}) → 走到 ({gx},{gy})")
        obj = _player(ctx.sc)
        if obj:
            ctx.mover.walk_exact(ctx.sc, obj, gx, gy)
        pg = _wait_page(ctx, pg0, MENU_WAIT)
        if pg is None:
            ctx.note("　沒跳出選單 → 再走一次")
            ok, _why = walk(ctx, gx, gy, arrive=0.8, secs=15.0)
            continue
        msg = _msg_of(pg)
        if msg != MENU_MSG:
            # 不是客廳那個選單 → 多半本來就在製作間（踩到的是製作間的傳送點）
            ctx.note(f"　跳出的不是「要去哪裡呢？」（訊息 {msg}，選項 {pg.options}）→ 關掉")
            _close_talk(ctx)
            _nap(ctx, 0.8)
            if not _marks():
                _learn_marks(ctx, None)
            if in_workshop(ctx.sc):
                return True, "原本就在生產寵物房"
            return False, f"屋裡的傳送點跳出沒見過的選單（訊息 {msg}）"
        if MENU_WORKSHOP not in pg.options:
            _close_talk(ctx)
            return False, f"選單沒有第 {MENU_WORKSHOP} 項（{pg.options}）"
        p0 = _pos(ctx.sc)
        supply._talkaction(ctx.mover, ctx.sc, supply.talk_option(MENU_WORKSHOP))
        ctx.note("　選「生產寵物房」")
        end = time.monotonic() + WARP_WAIT
        while time.monotonic() < end:
            _nap(ctx, 0.3)
            p1 = _pos(ctx.sc)
            if p0 and p1 and math.hypot(p1[0] - p0[0], p1[1] - p0[1]) > WARP_JUMP:
                _nap(ctx, 1.0)
                if in_workshop(ctx.sc) is not True:
                    _learn_marks(ctx, before_models)
                ctx.note(f"　到生產寵物房了 {p1}")
                return True, "到生產寵物房了"
        ctx.note("　選了但沒瞬移 → 重來")
        _close_talk(ctx)
    return False, f"踩傳送點 {STEP_TRIES} 次都沒進到生產寵物房"


def _enter(ctx: Ctx, sid: int, own: house.Own) -> tuple[bool, str]:
    """已在房子那張圖 → 走到房子旁 → 進門。"""
    lodge = house.find(ctx.sc, ctx.owner)
    grid = ctx.maps.get(ctx.sc)
    if grid is None:
        return False, "讀不到地形圖"
    cx, cy = (int(lodge.x), int(lodge.y)) if lodge else (own.x, own.y)
    spot = grid.nearest_open(cx, cy, HOUSE_GOAL_RELAX)
    if spot is None:
        return False, f"房子({cx},{cy}) 附近沒有站得住的格子"
    me = _pos(ctx.sc)
    if lodge is None or me is None or math.hypot(me[0] - lodge.x,
                                                 me[1] - lodge.y) > HOUSE_NEAR:
        ctx.note(f"走到房子旁 ({spot[0]},{spot[1]})")
        ok, why = walk(ctx, spot[0] + 0.5, spot[1] + 0.5, arrive=2.0)
        if not ok:
            return False, f"走不到房子旁：{why}"
    for n in range(1, STEP_TRIES + 1):
        lodge = house.find(ctx.sc, ctx.owner)
        if lodge is None:
            return False, f"房子旁看不到{ctx.owner}的小屋（被收了？換地方了？）"
        if lodge.locked:
            return False, "自己的小屋設了密碼 —— 工具箱不送密碼"
        ok, why = house.enter(ctx.mover, ctx.sc, lodge)
        ctx.note(f"進門第 {n} 次：{why}")
        end = time.monotonic() + ENTER_WAIT
        while time.monotonic() < end:
            _nap(ctx, 0.4)
            if house.inside(scene.current_id(ctx.sc)):
                _nap(ctx, 1.5)
                return True, "進屋了"
    return False, f"進門 {STEP_TRIES} 次都沒進去"


def go_workshop(ctx: Ctx) -> tuple[str, str]:
    """走到自己小屋的生產寵物房。回 (結果, 說明)：

        "ok"     到了
        "nohouse" 沒辦法有房子（擺不上去／沒裝備房屋）→ 呼叫端取消房屋製作、改原本方式
        "fail"   這一次沒成功（暫時性的）→ 呼叫端重試，連續幾次才停
    """
    try:
        ctx.note("=== 去生產寵物房")
        if in_workshop(ctx.sc):
            ctx.note("已經在生產寵物房")
            return "ok", "已經在生產寵物房"
        if house.inside(scene.current_id(ctx.sc)):
            ok, why = _to_workshop(ctx)
            return ("ok" if ok else "fail"), why

        shown = house.shown(ctx.sc)
        for _ in range(10):
            if shown is not None:
                break
            _nap(ctx, 0.5)
            shown = house.shown(ctx.sc)
        if shown is None:
            return "fail", "讀不到房子展示狀態"
        own = house.own(ctx.sc)
        ctx.note(f"房子展示中={shown}　記憶體位置={own}　清潔指數={house.cleanliness(ctx.sc)}")
        if shown and own is None:
            own = _last_where(ctx.owner)
            if own is not None:
                ctx.note(f"記憶體還沒有房子位置（重登後沒看過）→ 用上次看到的 {own}")
        if shown and own is None:
            ctx.note("房子展示中但不知道擺在哪 → 收回來重擺")
            ok, why = house.close(ctx.mover, ctx.sc)
            ctx.note(f"　收回：{why}")
            end = time.monotonic() + 5.0
            while time.monotonic() < end and house.shown(ctx.sc):
                _nap(ctx, 0.3)
            shown = house.shown(ctx.sc)
            if shown:
                return "fail", "房子收不回來"
        if not shown:
            ok, why = place(ctx)
            ctx.note(("✅ " if ok else "⛔ ") + why)
            if not ok:
                return "nohouse", why
            own = house.own(ctx.sc)
            if own is None:
                p = _pos(ctx.sc)
                sid = scene.current_id(ctx.sc)
                own = house.Own(scene.scene_name(sid), int(p[0]), int(p[1])) if p else None
            if own is None:
                return "fail", "房子擺了但讀不到位置"
        else:
            _remember_where(ctx.owner, own)

        sid = house.scene_of(own.map_name)
        if sid is None:
            return "fail", f"場景表查不到「{own.map_name}」"
        ok, why = _goto_map(ctx, sid, own.x, own.y, own.map_name)
        if not ok:
            return "fail", why
        ok, why = _enter(ctx, sid, own)
        if not ok:
            if "看不到" in why:
                _forget_where(ctx.owner)       # 記的位置不對了，下次重讀／重擺
            return "fail", why
        ok, why = _to_workshop(ctx)
        return ("ok" if ok else "fail"), why
    except Cancelled:
        return "fail", "已取消"


# ---------------------------------------------------------------------------
# 屋內保險箱
# ---------------------------------------------------------------------------
def _bank_type(sc):
    try:
        g = lua.globals_of(sc, (supply.BANK_WND, "CUR_BANK_TYPE"))
    except Exception:                                      # noqa: BLE001
        return None, None
    if not g:
        return None, None
    return bool(g.get(supply.BANK_WND)), g.get("CUR_BANK_TYPE")


def _chest_candidates(sc) -> list:
    me = _pos(sc)
    props = scenery.nearby(sc)
    if me is None or props is None:
        return []
    trig = {t.oid for t in (house.things(sc) or []) if t.trigger}
    cands = [p for p in props if p.oid not in trig and not mapobj.hidden(p.model)]
    want = config.get(CFG_CHEST, None)
    # 排序只是省時間（每一個都會用對話訊息 CHEST_MSG 驗身分）：學過的外觀 → 名字（資源包）
    # 有「寶箱」的 → 近的。點錯的只會跳別的對話或沒反應，關掉換下一個。
    cands.sort(key=lambda p: (0 if want is not None and p.model == want else
                              1 if "寶箱" in (mapobj.name_of(p.model) or "") else 2,
                              p.dist(me)))
    return cands


def open_chest(ctx: Ctx, guild: bool) -> tuple[bool, str]:
    """在製作間打開保險箱的個人／社團倉庫。成功回 True（倉庫視窗開著、種類對）。"""
    want_type = BANK_TYPE_GUILD if guild else BANK_TYPE_SELF
    bad: set[int] = set()
    quiet: dict[int, int] = {}          # 點了完全沒反應的次數（沒反應≠不是保險箱，多給一次）
    for _ in range(CHEST_TRIES):
        cands = [p for p in _chest_candidates(ctx.sc) if p.model not in bad]
        if not cands:
            return False, "找不到保險箱"
        p = cands[0]
        name = mapobj.name_of(p.model) or str(p.model)
        ok, why = walk(ctx, p.x, p.y, arrive=REACH, secs=40.0)
        if not ok:
            ctx.note(f"走不到 {name}：{why}")
            bad.add(p.model)
            continue
        _settle(ctx)
        pg0 = _page(ctx.sc)
        ok, why = produce.click_bench(ctx.mover, ctx.sc, p)
        me = _pos(ctx.sc)
        ctx.note(f"點 {name}（{p.x:.1f},{p.y:.1f}，離我 "
                 f"{p.dist(me) if me else -1:.1f} 格）：{why}")
        pg = _wait_page(ctx, pg0, PAGE_WAIT)
        msg = _msg_of(pg)
        if msg is None:
            quiet[p.model] = quiet.get(p.model, 0) + 1
            ctx.note(f"　沒有反應（第 {quiet[p.model]} 次）")
            if quiet[p.model] >= 2:
                bad.add(p.model)
            continue
        if msg != CHEST_MSG:
            ctx.note(f"　不是保險箱（對話訊息 {msg}）")
            _close_talk(ctx)
            bad.add(p.model)
            _nap(ctx, 0.5)
            continue
        if config.get(CFG_CHEST, None) != p.model:
            config.set(CFG_CHEST, p.model)
            config.save()
        supply._talkaction(ctx.mover, ctx.sc, supply.talk_option(CHEST_OPEN))
        pg2 = _wait_page(ctx, pg, PAGE_WAIT)
        if _msg_of(pg2) != CHEST_PICK_MSG:
            _close_talk(ctx)
            return False, f"選「開啟保險箱」後不是「要開啟哪個倉庫？」（訊息 {_msg_of(pg2)}）"
        supply._talkaction(ctx.mover, ctx.sc, supply.talk_option(2 if guild else 1))
        end = time.monotonic() + BANK_WAIT
        while time.monotonic() < end:
            _nap(ctx, 0.3)
            opened, typ = _bank_type(ctx.sc)
            if opened and typ is not None:
                if int(typ) == want_type:
                    ctx.note(f"　{'社團' if guild else '個人'}倉庫開了")
                    return True, "倉庫開了"
                supply._bank_close(ctx.mover, ctx.sc)
                return False, f"開到的倉庫種類不對（CUR_BANK_TYPE={typ}）→ 關掉不存"
        supply._bank_close(ctx.mover, ctx.sc)
        return False, "選了倉庫但視窗沒開"
    return False, f"點了 {CHEST_TRIES} 個候選都不是保險箱"


def deposit(ctx: Ctx, guild: bool, ids: set[int]) -> tuple[bool, str]:
    """把背包裡 ids 那幾種（能存的）存進屋內保險箱的個人／社團倉庫。"""
    from app.game import bank, guildbank
    pend_fn = guildbank.pending if guild else bank.pending
    where = "社團倉庫" if guild else "個人倉庫"
    try:
        ctx.note(f"=== 存{where}（屋內保險箱）")
        pend = pend_fn(ctx.sc, ids)
        if pend is None:
            return False, "讀不到背包"
        if not pend:
            ctx.note("沒有要存的東西")
            return True, f"沒有要存{where}的東西"
        ok, why = open_chest(ctx, guild)
        if not ok:
            ctx.note("⛔ " + why)
            return False, why
        want_type = BANK_TYPE_GUILD if guild else BANK_TYPE_SELF
        deposited, streak, refused = 0, 0, set()
        full = False
        for _ in range(supply.MAX_DEPOSIT):
            pend = pend_fn(ctx.sc, ids)
            if pend is None:
                break
            pend = [it for it in pend if it.serial not in refused]
            if not pend:
                break
            it = pend[0]
            opened, typ = _bank_type(ctx.sc)
            if not opened or typ is None or int(typ) != want_type:
                ctx.note(f"倉庫視窗不見了／種類變了（{opened},{typ}）→ 停")
                break
            ok, msg = supply.deposit_slot(ctx.mover, ctx.sc, it.slot)
            if not ok:
                ctx.note(f"存款送不出去：{msg}")
                break
            left = False
            for _ in range(supply.DEPOSIT_POLL):
                _nap(ctx, supply.DEPOSIT_WAIT)
                if supply._item_gone(ctx.sc, it.serial):
                    left = True
                    break
            if left:
                deposited += 1
                streak = 0
                ctx.note(f"　存入 {it.name}×{it.count}")
            else:
                refused.add(it.serial)
                streak += 1
                ctx.note(f"　{it.name} 沒存進去")
                if streak >= 2:
                    full = True
                    break
        supply._bank_close(ctx.mover, ctx.sc)
        tail = "（倉庫滿了／拒收）" if full else ""
        ctx.note(f"存了 {deposited} 件到{where}{tail}")
        return True, f"存了 {deposited} 件到{where}{tail}"
    except Cancelled:
        try:
            supply._bank_close(ctx.mover, ctx.sc)
        except Exception:                                  # noqa: BLE001
            pass
        return False, "已取消"
