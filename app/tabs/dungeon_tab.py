"""自動刷副本：照腳本一步一步跑，路上有怪先清光。

    勾「自動刷副本」→ 選一份腳本 → 立刻開始跑。

## 規格（使用者 2026-09-01 定案）

* 技能怎麼放**照自動掛機那樣**：勾 F 鍵、直讀快捷欄、照 F1→F12 輪流施放
  —— 所以這裡直接重用掛機的 `KeyWorker`／`TargetWorker`／`ScanWorker`，
  不另外寫一套出手邏輯（寫第二套就會有一套跟不上）。
* **去腳本點位的路上有怪，先殺光再走**。
* **結束＝整份腳本跑完，而且周圍沒有任何怪物**（使用者定的收工條件）。
* 組隊這一版不做。

## 跟掛機的差別（刻意簡化的地方）

掛機那頁還要管補給、巡邏、換頻、只打王、通知、交棒收回…這裡都沒有。
接近目標只有兩種：能交棒（整輪都是快捷鍵招式）就走到 `HANDOFF_RANGE`
讓遊戲自己走過去打；不能交棒就自己走到最遠那一招射程的九成，
剩下的射程判斷交給 `KeyWorker`（它每一招各自比自己的射程）。

## 三種一定要大聲停下來的情況（CLAUDE.md：只准大聲停用或安全退化）

1. **腳本跟眼前這張圖對不上**（場景編號或地圖指紋）→ 取消勾選並說原因。
   官方改過地圖還照舊腳本盲走，就是「安靜地做錯事」。
2. **對話那一步找不到對應外觀的物件** → 停下來。⛔ 不可以「就近點一個」——
   點錯東西比不點危險。
3. **讀不到狀態物件／玩家物件** → 這一拍什麼都不做（不寫記憶體、不出手）。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidgetAction,
)

from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import (dungeon, entity, locate, move, navigate, produce, scene,
                      scenery, sell, supply, terrain)
from app.tabs.base_tab import BaseTab
from app.tabs.farm_tab import (DEFAULT_KEY, HANDOFF_RANGE, KeyWorker,
                               MODE_PACKET, ScanWorker, SKILL_KEYS,
                               TargetWorker)

TICK_MS = 100
# 掃描節奏：交戰中要快（換目標／死亡），純趕路可以慢一點。
SCAN_FAST, SCAN_SLOW = 0.25, 0.8
# 走到腳本點位算「到了」的容忍半徑（格）。
ARRIVE = 1.8
# 對話那一步要先靠多近才點（格）。⚠ 太遠點下去人還沒到、對話就開了。
TALK_NEAR = 3.0
# 腳本裡的座標跟現場物件對得起來的最大誤差（格）。
PROP_TOL = 3.0
# 送完一個對話選項之後等多久再送下一個。
MENU_GAP = 0.8
# 這麼久還沒走到就當這一步卡住（大聲停下來，不要無聲無息耗著）。
STEP_TIMEOUT = 90.0
# 收工前要「連續這麼久都掃不到怪」才算真的沒怪了。
# ⚠ 實體是跟著玩家串流進來的，一拍掃不到不代表沒有。
CLEAR_SETTLE = 3.0
# 打不到（走不過去）這麼久就放棄這一隻，換下一隻。
GIVE_UP = 15.0


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class DungeonTab(BaseTab):
    TAB_TITLE = "自動刷副本"
    ORDER = 7                        # 排在副本腳本製作（6）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._hwnds: dict[int, int] = {}
        self._mover = None
        self._pid = None
        self._sc = None
        self._script = None
        self._keys = None            # KeyWorker
        self._atk = None             # TargetWorker
        self._scan = ScanWorker()
        self._scan.done.connect(self._on_scan)
        self._scan.start()
        self._nav = navigate.Navigator()
        self._maps = terrain.Cache()
        self._reset_run()

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(lambda: self._stop("換了分身"))
        bar.addWidget(self.who)
        b = QPushButton("重新整理")
        b.setToolTip("重新列出目前開著的遊戲分身。")
        b.clicked.connect(lambda: self.reload_instances(force_names=True))
        bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        sbar = QHBoxLayout()
        sbar.addWidget(QLabel("腳本"))
        self.files = QComboBox()
        self.files.setFixedWidth(240)
        self.files.setToolTip("在「副本腳本製作」那一頁做出來的腳本。")
        self.files.currentIndexChanged.connect(lambda: self._stop("換了腳本"))
        sbar.addWidget(self.files)
        b = QPushButton("重新整理")
        b.setToolTip("重新讀取腳本資料夾。")
        b.clicked.connect(self._reload_files)
        sbar.addWidget(b)
        sbar.addStretch(1)
        root.addLayout(sbar)

        g = QGroupBox("攻擊（跟自動掛機同一套）")
        a = QHBoxLayout(g)
        a.addWidget(QLabel("技能鍵"))
        # 跟掛機同樣的做法：勾幾個 F 鍵，照 F1→F12 輪流放；鍵上放什麼直讀
        # 快捷欄（空格／物品格自動略過）。⚠ 勾選框要包在 QWidgetAction 裡，
        # 不然點一下選單就關了，沒辦法一次勾多個。
        self.key_btn = QToolButton()
        self.key_btn.setPopupMode(QToolButton.InstantPopup)
        self.key_btn.setToolTip(
            "勾要輪流放的技能鍵（可多選），照 F1→F12 順序施放。\n"
            "放什麼直接讀遊戲快捷欄：空格、物品自動略過。")
        km = QMenu(self.key_btn)
        self._key_cbs: list[tuple[QCheckBox, int]] = []
        for label, vk in SKILL_KEYS:
            cb = QCheckBox(label)
            cb.setChecked(vk == DEFAULT_KEY)
            cb.setStyleSheet("padding: 4px 10px;")
            act = QWidgetAction(km)
            act.setDefaultWidget(cb)
            km.addAction(act)
            self._key_cbs.append((cb, vk))
        for cb, _vk in self._key_cbs:
            cb.toggled.connect(self._keys_changed)
        self.key_btn.setMenu(km)
        a.addWidget(self.key_btn)
        a.addStretch(1)
        root.addWidget(g)

        rbar = QHBoxLayout()
        self.run_cb = QCheckBox("自動刷副本")
        self.run_cb.setToolTip(
            "勾起來就照腳本開始跑：路上有怪先清光，再去下一個點位。\n"
            "整份腳本跑完而且周圍沒有怪 = 這一趟結束。")
        self.run_cb.toggled.connect(self._on_run_toggled)
        rbar.addWidget(self.run_cb)
        rbar.addStretch(1)
        self.prog = QLabel("－")
        rbar.addWidget(self.prog)
        root.addLayout(rbar)

        self.steps = QListWidget()
        self.steps.setSelectionMode(QListWidget.NoSelection)
        root.addWidget(self.steps, 1)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)
        self._reload_files()

    # ------------------------------------------------------------------
    # 分身與腳本
    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()
        self._reload_files()

    def reload_instances(self, force_names: bool = False) -> None:
        self._stop(quiet=True)
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._hwnds.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                            # noqa: BLE001
                continue
            try:
                locate.warm(sc)
            except Exception:                            # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            self._scanners[w.pid] = sc
            self._hwnds[w.pid] = w.hwnd
            self.who.addItem(
                f"{preload.name_of(w.pid, sc, acc, force=force_names)}"
                f"（{acc}）", w.pid)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")

    def _reload_files(self) -> None:
        keep = self.files.currentText()
        self.files.blockSignals(True)
        self.files.clear()
        for p in dungeon.list_scripts():
            self.files.addItem(p.stem, str(p))
        i = self.files.findText(keep)
        if i >= 0:
            self.files.setCurrentIndex(i)
        self.files.blockSignals(False)
        if not self.files.count():
            self.status.setText(
                "腳本資料夾是空的 —— 先去「副本腳本製作」做一份")

    def _keys_changed(self) -> None:
        if self._keys is not None:
            self._keys.vks = self._picked_keys()

    def _picked_keys(self) -> list[int]:
        vks = [vk for cb, vk in self._key_cbs if cb.isChecked()]
        return vks or [DEFAULT_KEY]

    # ------------------------------------------------------------------
    # 開始／停止
    # ------------------------------------------------------------------
    def _reset_run(self) -> None:
        self._i = 0                  # 目前跑到第幾步
        self._step_t = 0.0           # 這一步跑多久了
        self._menu_i = 0             # 對話選項送到第幾個
        self._menu_t = 0.0
        self._clicked = False        # 這一步的物件點過了嗎
        self._wait_left = 0.0
        self._last = None            # 最近一次掃描結果
        self._scan_t = 0.0
        self._cur = None             # 正在打的怪
        self._cur_t = 0.0
        self._state = None
        self._player = None
        self._empty_since = 0.0      # 連續多久掃不到怪
        self._done = False

    def _on_run_toggled(self, on: bool) -> None:
        if not on:
            self._stop("已停止")
            return
        pid = self.who.currentData()
        path = self.files.currentData()
        if pid is None:
            self._stop("先選一台分身")
            return
        if not path:
            self._stop("先選一份腳本")
            return
        from pathlib import Path
        script, why = dungeon.load(Path(path))
        if script is None:
            self._stop(f"⚠ 腳本讀不進來：{why}")
            return
        sc = self._scanners.get(int(pid))
        if sc is None:
            self._stop("這台分身不見了")
            return
        # ★ 開跑前比對地圖：對不上就大聲停用，不拿舊腳本盲走。
        grid = self._maps.get(sc)
        ok, why = dungeon.check_map(script, grid, scene.current_id(sc),
                                    scene.map_key)
        if not ok:
            self._stop(f"⛔ {why}")
            return
        try:
            self._mover = move.acquire(int(pid), injector.process_path(int(pid)),
                                       self)
        except Exception as exc:                         # noqa: BLE001
            self._stop(f"⚠ 無法安裝跳板：{exc}")
            return
        self._pid, self._sc, self._script = int(pid), sc, script
        self._reset_run()
        self._refresh_steps()
        self._atk = TargetWorker(sc)
        self._atk.died.connect(self._on_died)
        self._atk.packets = True
        self._atk.start()
        self._keys = KeyWorker(self._hwnds.get(int(pid), 0), sc)
        self._keys.mode = MODE_PACKET
        self._keys.packets = True
        self._keys.mover = self._mover
        self._keys.vks = self._picked_keys()
        self._keys.start()
        self.status.setText(f"開始跑「{script.name}」共 {len(script.steps)} 步")

    def _stop(self, why: str = "", quiet: bool = False) -> None:
        if self._keys is not None:
            self._keys.set_on(False)
            self._keys.eid = None
            self._keys.stop()
            self._keys.wait(500)
            self._keys = None
        if self._atk is not None:
            self._atk.stop()
            self._atk.wait(500)
            self._atk = None
        if self._mover is not None and self._pid is not None:
            # ★ release() 不是 stop()：跳板是同一個 PID 共用的。
            try:
                move.release(self._pid, self)
            except Exception:                            # noqa: BLE001
                pass
        self._mover = None
        self._nav.reset()
        if self.run_cb.isChecked():
            self.run_cb.blockSignals(True)
            self.run_cb.setChecked(False)
            self.run_cb.blockSignals(False)
        if why and not quiet:
            self.status.setText(why)

    # ------------------------------------------------------------------
    # 掃描
    # ------------------------------------------------------------------
    def _on_scan(self, s) -> None:
        if self._sc is None or s.pid != self._pid:
            return
        self._last = s
        self._state, self._player = s.state, s.player

    def _my_pos(self):
        if not self._player:
            return None
        return entity.read_pos(self._sc, self._player + 8)

    def _live_monsters(self) -> list:
        """掃描結果裡還活著的怪。⚠ 屍體會在清單裡賴很久，一定要濾掉。"""
        if self._last is None:
            return []
        return [m for m in self._last.mons if not m.dead]

    # ------------------------------------------------------------------
    # 主迴圈
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if not self.run_cb.isChecked() or self._sc is None:
            return
        dt = TICK_MS / 1000.0
        sc = self._sc

        # 掃描節奏：有怪要快、趕路可以慢
        self._scan_t -= dt
        if self._scan_t <= 0:
            self._scan_t = SCAN_FAST if self._cur else SCAN_SLOW
            self._scan.request(self._pid, sc)

        if self._state is None or self._player is None:
            # ⚠ 讀不到本體就什麼都不做：不寫記憶體、不出手。
            self._say("等掃描定位（狀態／玩家物件）…")
            return
        me = self._my_pos()
        if me is None:
            self._say("讀不到自己的位置，這一拍不動")
            return

        # ① 先處理怪 —— 使用者定的規矩：路上有怪先殺光再去點位
        if self._fight(me, dt):
            return

        # ② 沒怪了 → 跑腳本
        if self._i >= len(self._script.steps):
            self._finish(dt)
            return
        self._run_step(me, dt)

    # -- 打怪 ---------------------------------------------------------
    def _fight(self, me, dt: float) -> bool:
        """有怪就打。回 True＝這一拍在打怪，腳本先不動。"""
        alive = self._live_monsters()
        if self._cur is not None:
            still = next((m for m in alive if m.eid == self._cur.eid), None)
            if still is None:
                self._drop_target()
            else:
                self._cur = still
        if self._cur is None:
            if not alive:
                self._keys.set_on(False)
                self._keys.eid = None
                return False
            # 最近的一隻。⚠ 走不走得到交給 Navigator 判斷，這裡只挑近的。
            self._cur = min(alive, key=lambda m: _d((m.x, m.y), me))
            self._cur_t = 0.0
            self._atk.attack(self._state, self._cur)
            self._keys.eid = self._cur.eid
            self._empty_since = 0.0

        mp = entity.read_pos(self._sc, self._cur.addr)
        if mp is None:
            self._drop_target()
            return True
        d = _d(mp, me)
        self._keys.pos = (round(mp[0]), round(mp[1]))
        self._keys.player = self._player + 8
        # 能交棒（整輪都是快捷鍵招式）就走到 12 格讓遊戲自己走過去打；
        # 不能交棒就自己走近一點，各招的射程由 KeyWorker 自己比。
        handoff = self._keys.handoff
        self._keys.reach = HANDOFF_RANGE if handoff else 0.0
        self._keys.client_walk = handoff
        # 兩條執行緒對「現在是不是用封包打」要有共識（照抄掛機那邊的算法）：
        #   封包攻擊 → 寫目標那條**不寫血量**，讀到 0 才是真的死亡訊號。
        self._atk.packets = bool(
            self._keys.mode == MODE_PACKET and self._keys.packets
            and self._keys.skill and self._keys.mover is not None)
        # 「選定」封包送出去之後，才開始算「多久沒看到血量 = 屍體」。
        self._atk.engaged = self._keys.selected
        # ⚠ 不能交棒時要走到**最短射程**的九成 —— 取最短的，這一輪每一招才
        #   都打得到；寫死 2.5 格的話遠程角色會白走十幾格貼到怪臉上。
        mr = self._keys.min_range
        want = HANDOFF_RANGE if handoff else max(1.5, (mr or 3) * 0.9)
        if d > want:
            note = self._nav.step(self._sc, self._mover, self._player,
                                  mp[0], mp[1])
            self._keys.set_on(False)
            if self._nav.stuck and self._nav.stuck_reason == "grid":
                # 地形圖說到不了 → 換一隻，別耗著。
                self._drop_target()
                return True
        else:
            self._nav.reset()
            self._keys.set_on(True)
            note = "出手中"
        self._cur_t += dt
        if self._cur_t > GIVE_UP:
            self._drop_target()
            self._say(f"打不到「{self._cur.name if self._cur else '?'}」"
                      f"超過 {GIVE_UP:.0f} 秒 → 換下一隻")
            return True
        self._say(f"打怪：{self._cur.name}　{d:.1f} 格　{note}")
        return True

    def _drop_target(self) -> None:
        self._cur = None
        self._cur_t = 0.0
        if self._keys is not None:
            self._keys.set_on(False)
            self._keys.eid = None
        if self._atk is not None:
            self._atk.hold_off()
        self._nav.reset()
        # 死了／消失了 → 立刻重掃，別等下一個慢拍。
        self._scan_t = 0.0

    def _on_died(self, eid, _confirmed) -> None:
        if self._cur is not None and self._cur.eid == eid:
            self._drop_target()

    # -- 跑腳本 -------------------------------------------------------
    def _run_step(self, me, dt: float) -> None:
        step = self._script.steps[self._i]
        kind = step.get("do")
        self._step_t += dt
        if self._step_t > STEP_TIMEOUT:
            self._stop(f"⛔ 第 {self._i + 1} 步「{dungeon.describe(step)}」"
                       f"卡了 {STEP_TIMEOUT:.0f} 秒還沒完成 —— 停下來")
            return

        if kind == dungeon.CLEAR:
            # 怪已經在 _fight 清掉了，走到這裡就代表沒怪。
            # ⚠ 但要沉澱一下：實體是跟著玩家串流進來的，一拍掃不到不算數。
            self._empty_since += dt
            self._say(f"清怪確認中…{self._empty_since:.1f}/{CLEAR_SETTLE:.0f} 秒")
            if self._empty_since >= CLEAR_SETTLE:
                self._next()
            return

        if kind == dungeon.WAIT:
            if self._wait_left <= 0:
                self._wait_left = float(step.get("secs", 1))
            self._wait_left -= dt
            self._say(f"等待…剩 {max(self._wait_left, 0):.1f} 秒")
            if self._wait_left <= 0:
                self._next()
            return

        if kind == dungeon.WALK:
            gx, gy = step["to"]
            if _d((gx, gy), me) <= ARRIVE:
                self._nav.reset()
                self._next()
                return
            note = self._nav.step(self._sc, self._mover, self._player, gx, gy)
            if self._nav.stuck and self._nav.stuck_reason == "grid":
                self._stop(f"⛔ 第 {self._i + 1} 步：地形圖說走不到 "
                           f"({gx}, {gy}) —— 腳本的點位是不是在別的房間？")
                return
            self._say(f"第 {self._i + 1} 步　走到 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}")
            return

        if kind == dungeon.INTERACT:
            self._do_interact(step, me, dt)
            return

        self._stop(f"⛔ 第 {self._i + 1} 步是不認得的動作「{kind}」")

    def _do_interact(self, step: dict, me, dt: float) -> None:
        ax, ay = step["at"]
        want_model = step.get("model")
        # ⚠ 先走到旁邊再點：太遠就發互動包＝人還沒到、對話先開，選項送出去
        #   會落空（買東西那條路踩過同一個坑）。
        if _d((ax, ay), me) > TALK_NEAR:
            note = self._nav.step(self._sc, self._mover, self._player, ax, ay)
            if self._nav.stuck and self._nav.stuck_reason == "grid":
                self._stop(f"⛔ 第 {self._i + 1} 步：走不到對話點 ({ax}, {ay})")
                return
            self._say(f"第 {self._i + 1} 步　走去對話點 ({ax}, {ay})"
                      f"　剩 {_d((ax, ay), me):.1f} 格　{note}")
            return
        self._nav.reset()

        if not self._clicked:
            props = scenery.nearby(self._sc, (ax, ay), PROP_TOL)
            if props is None:
                # ⚠ 讀不到 ≠ 沒有。等下一拍再試，不要當成「這裡沒東西」。
                self._say("物件清單讀不到，重試中…")
                return
            hit = [p for p in props
                   if want_model is None or p.model == want_model]
            if not hit:
                # ⛔ 絕不「就近點一個」—— 點錯東西比不點危險。
                self._stop(
                    f"⛔ 第 {self._i + 1} 步：({ax}, {ay}) 附近 {PROP_TOL:.0f} "
                    f"格內找不到外觀 {want_model} 的物件"
                    f"（找到 {len(props)} 個別的）—— 停下來")
                return
            ok, msg = produce.click(self._mover, self._sc, hit[0])
            if not ok:
                self._say(f"點不下去（{msg}），重試中…")
                return
            self._clicked = True
            self._menu_i = 0
            # ★ 間隔照這一步自己存的（腳本製作那頁可以調）——太快送選項，
            #   伺服器那邊對話還沒準備好就會被拒絕（使用者 2026-09-02）。
            self._menu_t = float(step.get("gap") or MENU_GAP)
            self._say(f"第 {self._i + 1} 步　已點外觀 {hit[0].model}")
            return

        menu = step.get("menu") or []
        if self._menu_i < len(menu):
            self._menu_t -= dt
            if self._menu_t > 0:
                return
            n = menu[self._menu_i]
            if not sell.talk(self._mover, supply.talk_option(n)):
                self._say(f"第 {n} 項送不出去（指令槽忙碌），重試中…")
                return
            self._menu_i += 1
            self._menu_t = float(step.get("gap") or MENU_GAP)
            self._say(f"第 {self._i + 1} 步　已送第 {n} 項"
                      f"（{self._menu_i}/{len(menu)}）")
            return
        # 選項送完 → 送離開互動（不送的話伺服器會覺得我們還在講話）
        supply.leave_npc(self._mover)
        self._next()

    def _next(self) -> None:
        self._i += 1
        self._step_t = 0.0
        self._menu_i = 0
        self._clicked = False
        self._wait_left = 0.0
        self._empty_since = 0.0
        self._nav.reset()
        self._refresh_steps()

    def _finish(self, dt: float) -> None:
        """腳本跑完了 —— 還要**周圍沒有任何怪物**才算這一趟結束。"""
        if self._live_monsters():
            self._empty_since = 0.0
            return
        self._empty_since += dt
        self._say(f"腳本跑完，確認周圍沒怪…"
                  f"{self._empty_since:.1f}/{CLEAR_SETTLE:.0f} 秒")
        if self._empty_since >= CLEAR_SETTLE:
            self._done = True
            self._stop("✔ 這一趟結束：腳本跑完，周圍也沒有怪了")

    # ------------------------------------------------------------------
    def _refresh_steps(self) -> None:
        self.steps.clear()
        if self._script is None:
            return
        for i, s in enumerate(self._script.steps):
            mark = "▶" if i == self._i else ("✔" if i < self._i else "　")
            self.steps.addItem(f"{mark} {i + 1:>2}. {dungeon.describe(s)}")
        self.prog.setText(f"{min(self._i, len(self._script.steps))}"
                          f" / {len(self._script.steps)} 步")

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # ------------------------------------------------------------------
    def closeEvent(self, ev) -> None:                    # noqa: N802
        self._stop(quiet=True)
        self._scan.stop()
        self._scan.wait(800)
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        super().closeEvent(ev)
