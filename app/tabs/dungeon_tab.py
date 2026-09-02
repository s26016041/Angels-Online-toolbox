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

## ⚠⚠ 副本＝好幾塊互不相通的地方拼起來，靠傳點連（使用者 2026-09-02 提醒）

> 「副本很容易掃到另一個地區的怪物無法到達這點要注意」
> 「副本是多個地圖組合再一起靠傳點連接這點要注意」

實測吞噬之間 1 一張圖切出 **7 塊互不相通的區域**（最厚的牆超過 5 格）。
怪物是**跟著玩家串流進來的**，隔壁區的怪照樣會出現在掃描結果裡 —— 沒有處理
的話會變成：挑最近的一隻 → 尋路說到不了 → 換一隻 → 又挑到同一隻 →
**永遠在挑目標，腳本一步都不會前進**（就是使用者說的「跟自動戰鬥互卡」）。

所以出手之前先問地形圖：**只打「站在我這一區」的怪**（`Grid.reachable()`
泛洪一次 6ms）。打不到的怪不算數 —— 不然「都沒怪才算到點位」這條規矩
會被隔壁區的怪永遠卡住。

⚠⚠ **不能用距離判**（使用者 2026-09-02 特別提醒）：

> 「怪物可能跟我只有隔一牆但距離只有 3~2 格，這種的要注意排除」

判的是「它站的那一格在不在我這一區」。而且怪站在**可走格**上卻不在我這一區
時就直接排除，⛔ 不可以再看它旁邊那一圈 —— 隔一格薄牆的怪，鄰居格很容易
剛好落在我這一區，那樣就會把隔牆的怪誤判成打得到（見 `_can_reach`）。
⚠ 讀不到地形圖時**不過濾**（安全退化：寧可多打，不要因為讀不到就不出手）。

⛔ **不記黑名單**（使用者同日明令）：

> 「不要加黑名單，一直問能不能走到他那邊就好，
>   都問一輪沒有怪物能走到就算殺光」

門會解開，這一秒走不到的怪下一秒可能就打得到。所以每一拍重新問一輪，
打不到就換一隻（只有還有別隻時才跳過剛放棄的那隻），並立刻重讀地形。
保險是看門狗：一直在打卻 `NO_PROGRESS` 秒都沒殺掉半隻 → 大聲停下。

## 到點位跟自動戰鬥的先後（使用者 2026-09-02 定案）

> 「到點位不要跟自動戰鬥互卡，在殺怪物就不要跑點位，都沒怪到點位才算到」
> 「跑路徑的時候要把周圍怪物殺光（不包含無法到達的），要周圍完全沒有
>   怪物才可以繼續跑」

一拍只做一件事：**走得到的怪還有一隻就先打**（腳本完全不動），
問了一輪一隻都走不到才跑腳本。所以「到點位」這件事天生就發生在
「周圍走得到的怪都清光」的時候。

⚠ 也因為這樣，**腳本不需要「清光周圍的怪」這種步驟**（製作頁的按鈕已經
  拿掉，使用者 2026-09-02 指出它是多餘的）。舊腳本裡的 `clear` 照樣跑，
  行為只是「原地確認 CLEAR_SETTLE 秒都沒怪」。

## 三種一定要大聲停下來的情況（CLAUDE.md：只准大聲停用或安全退化）

1. **腳本跟眼前這張圖對不上**（場景編號或地圖指紋）→ 取消勾選並說原因。
   官方改過地圖還照舊腳本盲走，就是「安靜地做錯事」。
2. **對話那一步找不到對應外觀的物件** → 停下來。⛔ 不可以「就近點一個」——
   點錯東西比不點危險。
3. **讀不到狀態物件／玩家物件** → 這一拍什麼都不做（不寫記憶體、不出手）。
"""
from __future__ import annotations

import math
import time

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
from app.core.notifier import Notifier
from app.game import (dungeon, entity, itemname, jumpmap, locate, move,
                      navigate, produce, quickbar, scene, scenery, sell,
                      skills, supply, talkwnd, terrain)
from app.tabs.base_tab import BaseTab
from app.tabs.farm_tab import (DEFAULT_KEY, FULL_HUNT_GAP, HANDOFF_RANGE,
                               KeyWorker, MODE_PACKET, ScanWorker, SKILL_KEYS,
                               TargetWorker)

TICK_MS = 100
# 掃描節奏：交戰中要快（換目標／死亡），純趕路可以慢一點。
# ⚠ 趕路那個本來是 0.8 秒 —— 太慢：怪冒出來要一秒後才看得到，看起來就像
#   「走過去才打怪」（使用者 2026-09-02）。收到 0.35 秒。
SCAN_FAST, SCAN_SLOW = 0.25, 0.35
# 走到腳本點位算「到了」的容忍半徑（格）。
ARRIVE = 1.8
# ⚠⚠ `navigate.ARRIVE` 是 3.0 —— **尋路器在 3 格以內就什麼都不做**。
#   我們的門檻比它嚴（點位 1.8、傳點 2.5），中間那一段沒人負責，就會變成
#   「說還有 2.2 格卻不動」（使用者 2026-09-02 實遇，手動走一步才過）。
#   那一段改成直接送走路（`walk_exact`，不尋路）——就是 move.walk_near 檔頭
#   寫的「站在 2.2 格卡 8.2 秒」同一個老坑。
NAV_DEAD = navigate.ARRIVE
# 對話那一步要先靠多近才點（格）。
# ⚠⚠ 2026-09-02 使用者實遇：本來是 3.0，剛好等於 `navigate.ARRIVE` ——
#   結果站在**上一個點位**（離對話點 2.4 格）就開始點，人根本沒走過去，
#   然後「在上個點位一直打對話」。⛔ 太遠發互動包＝人沒到、對話開不起來
#   （補給點 NPC 那邊踩過同一個坑，見 supply 的「先走到位才發互動包」）。
#   → 收緊到 1.8，最後那一段用 `walk_near` 自己走（尋路器 3 格內不動）。
TALK_NEAR = 1.8
# 走到對話點時要留幾格（物件常常站在不可走的格上，不能走到它身上）。
TALK_KEEP = 1.2
# 腳本裡的座標跟現場物件對得起來的最大誤差（格）。
PROP_TOL = 3.0
# 送完一個對話動作之後等多久再看下一頁。
MENU_GAP = 0.8
# 按了確定之後連續這麼多輪都沒換頁 ＝ 這段對話走完了（那些全域關掉還會
# 留著，只能靠「不再變化」判結束，見 talkwnd.page 的說明）。
TALK_SETTLE = 2
# ★★ 點下去之後這麼久都沒有任何對話反應 → **再點一次**（使用者 2026-09-02
#   回報「最後一個石頭雕像點不到」）。點一次就不管是不對的：遊戲是「自己走
#   過去才開對話」，路上被怪打斷、被人擋住、剛好在走都會讓那一下落空 ——
#   跟補給點 NPC 那套「沒開就再點」同一個道理（見 supply 的 DIALOG_* 說明）。
#   ⚠ 上限交給 STEP_TIMEOUT（90 秒）大聲停，不在這裡另外設。
CLICK_RETRY = 6.0
# 送了選項之後最多等這麼久還沒有下一頁 → 大聲停下。
# ⛔ 這一段**不可以**用 TALK_SETTLE 那種「沒變就當結束」——伺服器回話本來
#   就要時間，那樣會誤判成「對話結束但腳本還有選項沒送到」（使用者實遇）。
TALK_WAIT = 10.0
# 這麼久還沒走到就當這一步卡住（大聲停下來，不要無聲無息耗著）。
STEP_TIMEOUT = 90.0
# 收工前要「連續這麼久都掃不到怪」才算真的沒怪了。
# ⚠ 實體是跟著玩家串流進來的，一拍掃不到不代表沒有。
CLEAR_SETTLE = 3.0
# 打不到（走不過去）這麼久就放棄這一隻，換下一隻。
GIVE_UP = 15.0
# ★★ 順移判定（傳點那一步的完成訊號，使用者 2026-09-02 定案）：
#   「人被傳走不會換地圖，有順移就算吧，有時候傳點之間也很短」
#   —— 所以不能用「離傳點多遠」判，要用**一拍之間跳了多少**：
#   跑步一拍（0.1 秒）最多動 0.6 格，跳這麼多格只可能是被搬過去的。
JUMP_TILES = 3.0
# 兩次取樣隔太久就分不出是走的還是傳的（畫面卡一下就會誤判）→ 這一拍不判。
JUMP_MAX_GAP = 0.35
# 傳到的位置跟腳本記的出口差這麼多格就當「傳到別的地方」，大聲停下。
LAND_TOL = 8.0
# ★★ 地形圖多久重讀一次（秒）。使用者 2026-09-02：
#   「地圖之間有可能會用牆壁隔開，解謎之後會打開又會變成聯通，
#     所以記憶體地圖要即時刷新」
#   —— 機關開門會**就地改掉可走格**，而 terrain.Cache 的身分只看
#   (地圖物件, 寬高, 場景編號, 列指標)，門開了那四樣全都沒變 → 不重讀就
#   一直拿舊的牆算路。一次重讀 7~8ms，兩秒一次完全不痛。
GRID_REFRESH = 2.0
# ⛔ **沒有黑名單**（使用者 2026-09-02 明令）：
#   「不要加黑名單，一直問能不能走到他那邊就好，
#     都問一輪沒有怪物能走到就算殺光」
#   —— 因為門會解開，這一秒走不到的怪下一秒可能就打得到了。
#   打不到就換一隻（有別隻的話），並**立刻重問一次地形**，不記時間。
# 一直在打卻完全沒有進展這麼久 → 大聲停下（不無聲無息耗一整晚）。
NO_PROGRESS = 180.0
# ★★ 尋路說「沒有路」之後還要再等多久才放棄（秒）。⛔ 不可以馬上停：
#   副本的門是**解謎才開**的（使用者 2026-09-02），這一秒沒有路不代表等一下
#   沒有 —— 2026-09-02 實跑就是這樣停在第 4 步（按完火炬、門還沒開）。
#   等的期間每一拍都重讀地形，門一開就走。
UNREACH_GRACE = 25.0
# 重要提示在狀態列上要停留幾秒（不然同一拍的走路訊息會馬上蓋掉）。
NOTICE_SECS = 6.0
# 算出來的「我這一區」小於這麼多格就當錨點抓錯了（碎片區）→ 這一輪不篩選。
# ⚠ 跟 dungeon.rooms() 的 min_cells 同一個道理：實測這張圖有 9/4/2 格的
#   零星角落，錨在那上面會把整張圖的怪都判成走不到。
MIN_REGION = 20
# ★★ 傳點站上去卻沒被搬走時的補救（使用者 2026-09-02 定案）：
#   「偵測走過去然後要有突然位移或換圖，如果失敗就在傳送點每 5 秒送一次，
#     如果 3 分鐘都這樣就結束跳通知警告使用者」
# ★ 人在別的圖（天使學園之類）→ 先用**天使趴趴GO**飛到入口那張圖
#   （使用者 2026-09-02 問：「我在天使學園開自動刷副本會用趴趴GO飛過去嗎」）。
#   實測表裡 `地底廣場(LV70~80)副本進入點` 就是一個合法目的地。
#   ⚠ 送出到人真的過去約 1 秒；這麼久還沒到就再送一次（跟撞入口一樣無限重試）。
FLY_RESEND = 8.0
PORTAL_NEAR = 2.5          # 站到這麼近就算「已經在傳點上」，開始補送
PORTAL_POKE = 5.0          # 每幾秒對傳點物件送一次互動
PORTAL_TIMEOUT = 180.0     # 這麼久還沒被搬走 → 結束並通知
#   ⚠⚠ 這個上限**只管副本裡面的傳點步驟**。「進副本的入口」是
#   **無限重試、不通知**（使用者 2026-09-02 明令）—— 進不去是暫時性失敗
#   （副本冷卻、門口有人擋），重試一定會過，見 `_go_entrance`。


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
        self._notifier = None        # 跳通知用（第一次要通知時才建）
        self._qb_sc = None           # 技能鍵標名字用的 Reader（跟著分身換）
        self._qb_ui = None
        self._scan = ScanWorker()
        self._scan.done.connect(self._on_scan)
        self._scan.start()
        # ⚠ 尋路跟我們自己**共用同一份**地形快取：兩份的話門開了只有一邊
        #   會跟上，另一邊拿舊的牆算路（使用者 2026-09-02 提醒門會開關）。
        self._maps = terrain.Cache()
        self._nav = navigate.Navigator(self._maps)
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
        self._key_cbs: list[tuple[QCheckBox, int, str]] = []
        for label, vk in SKILL_KEYS:
            cb = QCheckBox(label)
            cb.setChecked(vk == DEFAULT_KEY)
            cb.setStyleSheet("padding: 4px 10px;")
            act = QWidgetAction(km)
            act.setDefaultWidget(cb)
            km.addAction(act)
            self._key_cbs.append((cb, vk, label))
        for cb, _vk, _lab in self._key_cbs:
            cb.toggled.connect(self._keys_changed)
        # ★ 點開選單的當下把每個鍵標上「現在放什麼」：F1（電擊術Ⅳ）
        #   —— 跟自動掛機那頁同一套（使用者 2026-09-02：「怎麼沒寫技能名稱，
        #   不能只有 F1 F2 這樣」）。純讀快捷欄，開著跑也沒差。
        km.aboutToShow.connect(self._label_keys)
        self.key_btn.setMenu(km)
        self._sync_key_btn()
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
        self._sync_key_btn()
        if self._keys is not None:
            self._keys.vks = self._picked_keys()

    def _picked_keys(self) -> list[int]:
        vks = [vk for cb, vk, _lab in self._key_cbs if cb.isChecked()]
        return vks or [DEFAULT_KEY]

    def _sync_key_btn(self) -> None:
        """按鈕字樣＝勾了哪些鍵；勾太多就縮寫，別把整條列撐爆。"""
        labels = [lab for cb, _vk, lab in self._key_cbs if cb.isChecked()]
        if not labels:
            self.key_btn.setText("選技能鍵")
        elif len(labels) <= 4:
            self.key_btn.setText("、".join(labels))
        else:
            self.key_btn.setText(f"{labels[0]} 等 {len(labels)} 鍵")

    def _label_keys(self) -> None:
        """把選單上的 F1~F12 標成「F1（電擊術Ⅳ）」—— 跟掛機頁同一套。

        技能格 → F1（電擊術Ⅳ）、物品格 → F5（物品：高效紅藥水）、
        空格 → F7（空）。讀不到快捷欄（還沒進遊戲／改版位移）就維持素的
        F1~F12，**不亂標**。純讀取。

        ⚠ 只改字（setText），不重建清單 —— 重建會讓順序跳動（[[qt-ui-pitfalls]]）。
        ⚠ 這一頁的分身是可以換的，所以 Reader 每次照「現在選的那台」開，
          不像掛機頁綁死一顆（換了分身還用舊的＝標到別隻角色的技能）。
        """
        sc = self._scanners.get(self.who.currentData())
        cells = None
        if sc is not None:
            if getattr(self, "_qb_sc", None) is not sc:
                self._qb_sc, self._qb_ui = sc, quickbar.Reader(sc)
            try:
                cells = quickbar.read_page(sc, self._qb_ui.page())
            except Exception:                            # noqa: BLE001
                cells = None
        for i, (cb, _vk, label) in enumerate(self._key_cbs):
            text = label
            if cells is not None and i < len(cells):
                c = cells[i]
                if c is None:
                    text = f"{label}（空）"
                elif c.is_skill:
                    nm = skills.name_of(c.value) or f"技能{c.value}"
                    # 位移／補血／buff 放進攻擊循環只會幫倒忙，標出來
                    # （循環本來就會跳過它們）
                    if not skills.is_attack(c.value):
                        nm += "・非攻擊"
                    text = f"{label}（{nm}）"
                elif c.is_item:
                    nm = itemname.of(c.value)
                    text = f"{label}（物品：{nm}）" if nm else f"{label}（物品）"
            cb.setText(text)

    # ------------------------------------------------------------------
    # 開始／停止
    # ------------------------------------------------------------------
    def _reset_run(self) -> None:
        self._i = 0                  # 目前跑到第幾步
        self._step_t = 0.0           # 這一步跑多久了
        self._menu_i = 0             # 對話選項送到第幾個
        self._menu_t = 0.0
        self._full_req_t = 0.0       # 補救全掃的節流
        self._talk_sig = None        # 上一輪看到的對話簽章（換頁偵測）
        self._talk_same = 0          # 簽章連續幾輪沒變
        self._talk_did = ""          # 這一頁做過什麼（"opt"／"close"）
        self._talk_base = None       # 點下去那一刻的簽章（判「有沒有點到」）
        self._click_t = 0.0          # 點了多久還沒反應
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
        self._map_key = None         # 現在**應該**在哪一張圖（走過傳點可能換）
        self._pos_prev = None        # 上一拍的位置（順移偵測用）
        self._pos_t = 0.0
        self._jumped = False         # 這一拍有沒有順移
        self._grid_t = 0.0           # 還有多久重讀地形圖
        self._unreach_t = 0.0        # 「沒有路」連續多久了（等門開）
        self._blocked_last = False   # 上一拍是不是卡在「沒有路」
        self._notice = ""            # 要停留幾秒的提示
        self._poke_t = 0.0           # 還有多久對傳點補送一次互動
        # "fly"＝趴趴GO去入口那張圖、"enter"＝撞入口、"run"＝跑腳本
        self._phase = "run"
        self._enter_t = 0.0          # 撞入口撞多久了
        self._fly = None             # 要飛去哪個傳送點（jumpmap.Entry）
        self._fly_t = 0.0            # 還有多久重送一次趴趴GO
        self._fly_total = 0.0        # 飛了多久了（只拿來顯示）
        self._notice_t = 0.0
        self._reach = None           # 「我這一區」走得到的格子（None＝沒有圖）
        self._reach_n = 0            # 上次那一區有幾格（拿來看門開了沒）
        self._grid = None            # 上次讀到的地形圖（判斷怪站的是不是可走格）
        self._last_gave_up = None    # 剛放棄的那一隻（有別隻時先挑別隻）
        self._nokill_t = 0.0         # 一直在打卻沒殺掉半隻多久了

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
        # ★★ 現在人在哪？（使用者 2026-09-02：「自動刷副本那邊就會看，
        #   如果在副本裡面會直接執行 json 開始跑；如果不在就會去撞副本傳點」）
        here = scene.map_key(scene.current_id(sc))
        ent = script.entrance or {}
        fly = None
        if here is not None and script.scene is not None and here != script.scene:
            if not ent:
                self._stop(f"⛔ 你不在「{scene.scene_name(script.scene)}」，"
                           f"這份腳本也沒記入口傳送點 —— 先進副本再開，"
                           f"或在製作頁按「這是進副本的入口」記一次。")
                return
            if here != ent.get("scene"):
                # ★ 在別的圖（天使學園之類）→ 先用天使趴趴GO飛到入口那張圖。
                ex, ey = (ent.get("to") or [None, None])[:2]
                fly = jumpmap.nearest(ent["scene"], ex, ey)
                if fly is None:
                    self._stop(
                        f"⛔ 你在「{scene.scene_name(here)}」（{here}），"
                        f"而趴趴GO沒有到「{scene.scene_name(ent.get('scene'))}」"
                        f"的傳送點 —— 自己走過去再開。")
                    return
                phase = "fly"
            else:
                phase = "enter"            # 在入口那張圖 → 先去撞傳點
        else:
            # ★ 已經在副本裡 → 照舊比對地圖指紋，對不上就大聲停用。
            grid = self._maps.get(sc)
            ok, why = dungeon.check_map(script, grid, scene.current_id(sc),
                                        scene.map_key)
            if not ok:
                self._stop(f"⛔ {why}")
                return
            phase = "run"
        try:
            self._mover = move.acquire(int(pid), injector.process_path(int(pid)),
                                       self)
        except Exception as exc:                         # noqa: BLE001
            self._stop(f"⚠ 無法安裝跳板：{exc}")
            return
        self._pid, self._sc, self._script = int(pid), sc, script
        self._reset_run()
        self._phase = phase
        self._fly = fly
        self._map_key = here
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
        if phase == "fly":
            self.status.setText(
                f"「{script.name}」：先用趴趴GO飛去「{fly.name}」，"
                f"再去撞入口 —— {dungeon.describe_entrance(ent)}")
        elif phase == "enter":
            self.status.setText(
                f"「{script.name}」：先去撞入口傳送點 —— "
                f"{dungeon.describe_entrance(ent)}")
        else:
            self.status.setText(
                f"開始跑「{script.name}」共 {len(script.steps)} 步")

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
        if self._mover is not None:
            # ★ 停機也要把對話框收掉，不然人會帶著框走來走去。
            try:
                talkwnd.close_window(self._mover, self._sc)
                supply.leave_npc(self._mover)
            except Exception:                            # noqa: BLE001
                pass
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
        # ⚠⚠ `s.player` 是 `entity.snapshot()` 掃 VT_PLAYER 找到的位址，
        #   **已經是實體本體**（＝`pathfinder_this() + 8`）—— 這裡再 +8 讀到的
        #   是後面 8 bytes，實測回 (0.0, 0.0)：順移偵測、走到沒、傳點落點驗證
        #   全部拿 (0,0) 在算。2026-09-02 修。
        #   （`bag.player_entity()` 回的才是 pf 那一種，那邊要 +8，見
        #     [[entity-coordinates]]「差 8 bytes」那條。）
        return entity.read_pos(self._sc, self._player)

    def _live_monsters(self) -> list:
        """掃描結果裡還活著的怪。⚠ 屍體會在清單裡賴很久，一定要濾掉。"""
        if self._last is None:
            return []
        return [m for m in self._last.mons if not m.dead]

    # -- 這一區走得到哪裡 ---------------------------------------------
    def _refresh_grid(self, me, dt: float) -> None:
        """定期重讀地形圖，並算出「我這一區」走得到哪些格子。

        ★★ 為什麼要定期重讀（使用者 2026-09-02）：機關解開會**把牆變成路**，
          但 `terrain.Cache` 的身分是 (地圖物件, 寬高, 場景編號, 列指標)，
          門開了那四樣一個都沒變 —— 不主動丟快取就會一直拿舊的牆算路，
          「解完謎卻說走不到」。所以每 GRID_REFRESH 秒 `drop()` 一次。
        ⚠ 讀不到圖就把 `_reach` 設成 None ＝**不過濾**（安全退化）：
          寧可多打幾隻打不到的，也不要因為讀不到圖就完全不出手。
        """
        self._grid_t -= dt
        stale = self._grid_t <= 0
        if stale:
            self._grid_t = GRID_REFRESH
            self._maps.drop()
        cell = (int(me[0]), int(me[1]))
        # 沒過期、而且我還站在上次算的那一區裡 → 直接沿用
        if not stale and self._reach is not None and cell in self._reach:
            return
        grid = self._maps.get(self._sc)
        if grid is None:
            self._reach, self._reach_n = None, 0
            return
        # ⚠⚠ 錨點要挑**最大的那一區**，不是「第一個問得到的」（2026-09-02
        #   修「不打怪」的第三個病灶）：角色常常站在地形圖標成不可走的格上
        #   （貼牆、石頭邊、剛落地），這時要問旁邊那圈；照順序取第一個問得到
        #   的，很可能拿到一個幾格大的**碎片區**（實測這張圖有 9/4/2 格的
        #   零星角落）—— 一旦 `_reach` 變成碎片，**每一隻怪都會被判成走不到**，
        #   症狀就是「完全不理怪物，直接走點位」。
        got = grid.reachable(*cell)
        if got is None:
            best = None
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                cand = grid.reachable(cell[0] + dx, cell[1] + dy)
                if cand and (best is None or len(cand) > len(best)):
                    best = cand
            got = best
        self._grid = grid
        if not got:
            self._reach, self._reach_n = None, 0
            return
        if len(got) < MIN_REGION:
            # 算出來只有幾格 ＝ 錨點多半錯了（碎片區）。⚠ 這種時候**不過濾**，
            #   不要拿一個顯然不對的可達區把全部的怪都判掉（安全退化）。
            self._notify(f"⚠ 算出來的可走區只有 {len(got)} 格，看起來不對 "
                         f"→ 這一輪不篩選怪物")
            self._reach, self._reach_n = None, 0
            return
        # 這一區的格數變了 ＝ 門開了／關了，講出來（使用者要看得到）
        if self._reach is not None and len(got) != self._reach_n:
            self._notify(f"地形變了：這一區從 {self._reach_n} 格變成 "
                         f"{len(got)} 格（機關開門／關門？）")
        self._reach, self._reach_n = got, len(got)

    def _can_reach(self, pos) -> bool:
        """那個位置**現在**走得到嗎（每次都重新問，⛔ 不記黑名單）。

        ⚠⚠ 使用者 2026-09-02 特別提醒的坑：
        > 「怪物可能跟我只有隔一牆但距離只有 3~2 格，這種的要注意排除」

        所以判定是「它站的那一格在不在**我這一區**」，**不是**看距離：
        · 怪站在可走格上但不在我的可達集合裡 → **隔壁區，直接排除**
          （⛔ 這裡不可以再看它旁邊那一圈 —— 隔一格薄牆的怪，鄰居格很可能
            剛好落在我這一區，那樣就會把「隔牆的怪」誤判成打得到）。
        · 怪站在**不可走格**上（貼牆、卡在門框、飛行怪）→ 它本來就不會在
          任何一區裡，這時才看旁邊一圈有沒有我走得到的格子。
        ⚠ 沒有可達集合（讀不到地形圖）一律回 True＝不過濾（安全退化）。
        """
        if self._reach is None:
            return True
        x, y = int(pos[0]), int(pos[1])
        if (x, y) in self._reach:
            return True
        if self._grid is not None and self._grid.walkable(x, y):
            return False              # 站在可走格卻不在我這一區 ＝ 隔壁區
        return any((x + dx, y + dy) in self._reach
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    def _targets(self) -> list:
        """**現在**走得到的活怪。每一拍重新問一輪，⛔ 不記黑名單。

        使用者 2026-09-02 定案：
        > 「不要加黑名單，一直問能不能走到他那邊就好，
        >   都問一輪沒有怪物能走到就算殺光」

        —— 門會解開，這一秒走不到的怪下一秒可能就打得到了；記黑名單會讓
        它在名單過期前一直被忽略。
        """
        return [m for m in self._live_monsters()
                if self._can_reach((m.x, m.y))]

    def _give_up(self, why: str) -> None:
        """這一隻先放著，換一隻打。⛔ 不記黑名單，只是**立刻重問一次地形**。"""
        if self._cur is not None:
            self._last_gave_up = self._cur.eid
            self._say(f"{why} → 先換一隻（「{self._cur.name}」等一下再問）")
        self._grid_t = 0.0            # 下一拍就重讀地形＋重算可達區
        self._drop_target()

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

        # ⓪ 人被搬走了嗎？順移要**每一拍**都採樣，不然錯過那一下就看不到了。
        self._jumped = self._check_jump(me)
        # 換圖了嗎？—— 座標是**跟著地圖**的，圖一換舊座標全部沒有意義。
        #   只有 `portal` 那一步准許換圖（有些傳點確實會換圖）；其他時候換圖
        #   ＝被傳走／死亡回城，繼續跑就是拿別張圖的座標亂走。
        if self._check_map_change():
            return

        # ⓪-2 地形圖定期重讀（機關會開門）＋算出「我這一區」走得到哪裡
        self._refresh_grid(me, dt)

        # ① 先處理怪 —— 使用者定的規矩：路上有怪先殺光再去點位
        #    ⚠ 這裡回 True 就整拍不跑腳本 ＝「在殺怪就不跑點位」；
        #      回 False 代表**打得到的怪一隻都不剩**，才輪到腳本。
        #    ⛔ **只在副本裡面打**（使用者 2026-09-02：「進入副本前不要
        #      自動打怪」）——去副本路上遇到什麼都不理，直接趕路。
        if self._phase == "run" and self._fight(me, dt):
            return
        if self._phase != "run" and self._keys is not None:
            self._keys.set_on(False)      # 趕路途中確保不會出手
            self._keys.eid = None

        # ② 沒怪了 → 還在別張圖就先飛過去，在入口那張圖就去撞入口
        if self._phase == "fly":
            self._go_fly(dt)
            return
        if self._phase == "enter":
            self._go_entrance(me, dt)
            return
        if self._i >= len(self._script.steps):
            self._finish(dt)
            return
        self._run_step(me, dt)

    # -- 進副本 -------------------------------------------------------
    def _go_fly(self, dt: float) -> None:
        """人在別張圖：用天使趴趴GO飛到入口那張圖。

        使用者 2026-09-02 問：「我在天使學園開自動刷副本，會用天使趴趴GO
        飛過去最近地方嗎」—— 會（本來不會，這一支就是為此加的）。
        ⚠ 挑的是**離腳本記的入口最近**的那個傳送點（`jumpmap.nearest`），
          不是隨便第一個 —— 一張圖常有「入口」與「副本進入點」兩個落點。
        ⚠ 跟撞入口同一條規矩：**無限重試、不通知**（送不出去多半是暫時性的，
          剛戰鬥完／指令槽忙）。到了沒有是靠換圖偵測認的。
        """
        self._fly_total += dt
        self._fly_t -= dt
        if self._fly is None:                 # 理論上不會（開跑時就挑好了）
            self._stop("⛔ 沒有可以飛的傳送點")
            return
        if self._fly_t > 0:
            self._say(f"趴趴GO去「{self._fly.name}」…等落地"
                      f"（已試 {self._fly_total / 60.0:.1f} 分鐘）")
            return
        self._fly_t = FLY_RESEND
        ok, why = jumpmap.teleport(self._mover, self._sc, self._fly.jump_id)
        self._say(f"趴趴GO去「{self._fly.name}」（{why}）"
                  f"　已試 {self._fly_total / 60.0:.1f} 分鐘")

    def _go_entrance(self, me, dt: float) -> None:
        """還在外面：走去入口傳送點，撞進去。

        使用者 2026-09-02 定案：
        > 「如果在副本裡面會直接執行 json 開始跑；如果不在就會去撞副本傳點，
        >   如果撞了沒效就會每 5 秒送一次傳送直到成功，然後執行 json」
        > 「無限嘗試不需要通知」（同日補充）

        ⚠ 進去了是靠 `_check_map_change()` 認的（場景會變）——這裡只負責
          「走過去 ＋ 撞不進去就補送」。
        """
        ent = self._script.entrance or {}
        gx, gy = ent.get("to", [0, 0])
        self._enter_t += dt
        # ⛔⛔ **這一階段不設上限、也不通知**（使用者 2026-09-02 明令：
        #   「無限嘗試不需要通知」）。進不去副本是**暫時性失敗**——副本有
        #   冷卻、有人在門口擋、剛好被打斷都會這樣，重試一定會過；
        #   出口是使用者自己把勾選拿掉（[[transient-failure-auto-retry]]）。
        #   ⚠ 只有進到副本**之後**的傳點才有 3 分鐘上限＋通知（那是走不通
        #   的訊號，不是等一下就好）。
        mins = self._enter_t / 60.0
        tail = f"（已試 {mins:.1f} 分鐘）"
        if _d((gx, gy), me) > PORTAL_NEAR:
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._close_in(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    # ⛔ 這裡不用 `_blocked()`：那支等過寬限就會停，跟「無限
                    #   嘗試」衝突。改成重讀地形繼續試（人牆散開就走得到）。
                    self._grid_t = 0.0
                    self._say(f"去入口 ({gx}, {gy})：現在算不出路，重讀地形再試"
                              f"{tail}")
                    return
            self._say(f"去入口傳送點 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}{tail}")
            return
        # 已經站在入口上還沒進去 → 每 PORTAL_POKE 秒補送一次互動
        self._nav.reset()
        self._poke_t -= dt
        want = ent.get("model")
        if want is None or self._poke_t > 0:
            self._say(f"站在入口上等被傳進去…{tail}")
            return
        self._poke_t = PORTAL_POKE
        props = scenery.nearby(self._sc, (gx, gy), PROP_TOL)
        if props is None:
            self._say(f"物件清單讀不到，等下一次補送…{tail}")
            return
        hit = [p for p in props if p.model == want]
        if not hit:
            # ⛔ 找不到就等，**絕不就近點一個**。
            self._say(f"入口附近找不到外觀 {want} 的物件，繼續等{tail}")
            return
        ok, msg = produce.click(self._mover, self._sc, hit[0])
        self._say(f"撞入口傳送點（{'送出' if ok else msg}）…{tail}")

    def _check_jump(self, me) -> bool:
        """這一拍人有沒有被「搬」過去（順移）。

        ★ 傳點的完成訊號就是它（使用者 2026-09-02：「人被傳走不會換地圖，
          有順移就算吧，有時候傳點之間也很短」）—— 用距離門檻會漏掉短傳點，
          用速度就分得出來：跑步一拍最多 0.6 格，順移一拍好幾十格。
        ⚠ 兩次取樣隔太久（畫面卡住、剛開始跑）一律**不判**，只重設基準 ——
          寧可漏一次（下一拍還會再看），不要誤判成傳送了就跳下一步。
        """
        now = time.monotonic()
        prev, prev_t = self._pos_prev, self._pos_t
        self._pos_prev, self._pos_t = me, now
        if prev is None or now - prev_t > JUMP_MAX_GAP:
            return False
        return _d(prev, me) >= JUMP_TILES

    def _check_map_change(self) -> bool:
        """換圖了就處理掉。回 True＝這一拍不要再往下跑。

        ⚠ `allow_scan=False`：這是 10 Hz 的心跳，全掃備援 0.3 秒會卡住畫面。
          讀不到就當「不知道」跳過 —— 讀不到 ≠ 換圖了。
        """
        here = scene.map_key(scene.current_id(self._sc, allow_scan=False))
        if here is None or self._map_key is None or here == self._map_key:
            return False
        if self._phase == "fly":
            # 飛到了：落在入口那張圖 → 去撞入口；直接落在副本裡 → 開跑。
            ent = self._script.entrance or {}
            self._map_key = here
            self._drop_target()
            self._nav.reset()
            self._grid_t = 0.0
            if here == self._script.scene:
                self._phase = "run"
                self._scan.force_full(self._pid)
                self._notify(f"飛到副本裡了（{scene.scene_name(here)}）→ 跑腳本")
            elif here == ent.get("scene"):
                self._phase = "enter"
                self._enter_t = 0.0
                self._poke_t = 0.0
                self._notify(f"飛到「{scene.scene_name(here)}」→ 去撞入口")
            else:
                self._say(f"飛到了「{scene.scene_name(here)}」，"
                          f"不是要去的那張圖 —— 再飛一次")
                self._fly_t = 0.0            # 立刻重送
            return True
        if self._phase == "enter":
            # ★ 還在外面撞入口：換到腳本那張圖就是進來了。
            if here != self._script.scene:
                self._stop(f"⛔ 從入口進到的是「{scene.scene_name(here)}」"
                           f"（{here}），不是腳本的"
                           f"「{scene.scene_name(self._script.scene)}」—— 停下來")
                return True
            grid = self._maps.get(self._sc)
            ok, why = dungeon.check_map(self._script, grid,
                                        scene.current_id(self._sc),
                                        scene.map_key)
            if not ok:
                self._stop(f"⛔ 進來了，但{why}")
                return True
            self._map_key = here
            self._phase = "run"
            # ⚠⚠ 換圖之後怪是**重新配置**的，掃描的「熱區」還是舊圖那一塊 →
            #   不強制全掃的話最久要等 FULL_EVERY(30 秒) 才看得到怪，
            #   症狀就是「進副本完全不打怪一直往點位走」（使用者 2026-09-02）。
            self._scan.force_full(self._pid)
            self._drop_target()
            self._nav.reset()
            self._grid_t = 0.0            # 換圖了 → 地形與可達區重算
            self._notify(f"進副本了（{scene.scene_name(here)}）→ 開始跑腳本")
            return True
        step = (self._script.steps[self._i]
                if self._i < len(self._script.steps) else {})
        if step.get("do") != dungeon.PORTAL:
            self._stop(
                f"⛔ 地圖變了（{scene.scene_name(self._map_key)} → "
                f"{scene.scene_name(here)}），但第 {self._i + 1} 步不是傳點 "
                f"—— 被傳走還是死亡回城了？停下來，不拿舊座標亂走")
            return True
        want = step.get("scene")
        if want is not None and want != here:
            self._stop(
                f"⛔ 第 {self._i + 1} 步的傳點應該到"
                f"「{scene.scene_name(want)}」（{want}），"
                f"實際到了「{scene.scene_name(here)}」（{here}）—— 停下來")
            return True
        # 到了新的圖：座標系換了，正在走的路線與正在打的怪全部作廢。
        self._map_key = here
        self._scan.force_full(self._pid)      # 新圖的怪是新配置的，熱區要重建
        self._drop_target()
        self._nav.reset()
        self._empty_since = 0.0
        self._say(f"第 {self._i + 1} 步　已經傳到"
                  f"「{scene.scene_name(here)}」")
        self._next()
        return True

    # -- 打怪 ---------------------------------------------------------
    def _fight(self, me, dt: float) -> bool:
        """**打得到的**怪還有就打。回 True＝這一拍在打怪，腳本先不動。

        ⛔ 打不到的（隔壁區、地形圖說沒有路）不算數 —— 算進去的話
          「都沒怪才算到點位」就會被永遠卡住（使用者 2026-09-02）。
        """
        alive = self._targets()
        if self._cur is not None:
            still = next((m for m in alive if m.eid == self._cur.eid), None)
            if still is None:
                self._drop_target()
            else:
                self._cur = still
        if self._cur is None:
            if not alive:
                # ★ 問了一輪，**沒有任何一隻走得到** ＝ 這裡算殺光了
                #   （使用者 2026-09-02 定的收斂條件）。
                # ⚠ 但要**講出來**是「真的沒怪」還是「有怪但都走不到」——
                #   不講的話就會出現使用者說的「不理怪物直接走點位」而
                #   完全查不出原因（CLAUDE.md：不准安靜地做決定）。
                # ★ 一隻都挑不到 → 定期要求全掃當保險（熱區可能整塊漏掉，
                #   跟掛機頁 FULL_HUNT_GAP 同一套）。
                now = time.monotonic()
                if now - self._full_req_t >= FULL_HUNT_GAP:
                    self._full_req_t = now
                    self._scan.force_full(self._pid)
                skipped = len(self._live_monsters())
                if skipped:
                    self._say(f"周圍 {skipped} 隻怪**全部走不到**"
                              f"（隔壁區／沒有路）→ 當作這裡清光了")
                self._keys.set_on(False)
                self._keys.eid = None
                self._nokill_t = 0.0
                self._last_gave_up = None
                return False
            # 剛放棄的那一隻先跳過 —— 但**只有在還有別隻的時候**。
            # ⛔ 不是黑名單：只剩它一隻就照樣再問一次（門可能開了）。
            pool = [m for m in alive if m.eid != self._last_gave_up] or alive
            # 最近的一隻。⚠ 走不走得到已經在 _targets() 問過了。
            self._cur = min(pool, key=lambda m: _d((m.x, m.y), me))
            self._cur_t = 0.0
            self._atk.attack(self._state, self._cur)
            self._keys.eid = self._cur.eid
            self._empty_since = 0.0
            # ★ 打起來了 → 正在數的「休息」作廢，等清乾淨再從頭數
            #   （使用者 2026-09-02：沒有可以打到的怪才算進入休息）。
            self._wait_left = 0.0
        # 一直在打卻半隻都沒殺掉 → 大聲停下，不要無聲無息耗一整晚。
        self._nokill_t += dt
        if self._nokill_t > NO_PROGRESS:
            self._stop(f"⛔ 打了 {NO_PROGRESS:.0f} 秒一隻都沒殺掉 —— 停下來"
                       f"（打不動？被卡住？）")
            return True

        mp = entity.read_pos(self._sc, self._cur.addr)
        if mp is None:
            self._drop_target()
            return True
        d = _d(mp, me)
        self._keys.pos = (round(mp[0]), round(mp[1]))
        # ⚠⚠ 跟 `_my_pos()` 同一個坑（2026-09-02 第二處）：`s.player` 已經是
        #   實體本體，**不可以再 +8**。KeyWorker 拿它讀「我現在離目標多遠」
        #   （`entity.read_pos(self.player)`）—— 讀成 (0,0) 的話每一招都會
        #   判成「超出射程」而完全不出手，症狀就是「完全不打怪物」。
        #   掛機頁是 `self._keys.player = self.player`（farm_tab），照它。
        self._keys.player = self._player
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
                # 地形圖說到不了 → 換一隻，並立刻重問一次地形（門可能開了）。
                self._give_up("地形圖說走不到")
                return True
        else:
            self._nav.reset()
            self._keys.set_on(True)
            note = "出手中"
        self._cur_t += dt
        if self._cur_t > GIVE_UP:
            self._give_up(f"打不到超過 {GIVE_UP:.0f} 秒")
            return True
        left = len(alive)
        self._say(f"打怪：{self._cur.name}　{d:.1f} 格　{note}"
                  f"　（走得到的還有 {left} 隻）")
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
            self._nokill_t = 0.0          # 有進展了 → 看門狗歸零
            self._last_gave_up = None
            self._drop_target()

    # -- 跑腳本 -------------------------------------------------------
    def _run_step(self, me, dt: float) -> None:
        step = self._script.steps[self._i]
        kind = step.get("do")
        # 上一拍沒卡在「沒有路」→ 等門開的計時歸零（門開了就重新算）
        if not self._blocked_last:
            self._unreach_t = 0.0
        self._blocked_last = False
        self._step_t += dt
        # ⚠ 傳點那一步有自己的（比較長的）上限：使用者要求站上去沒反應時
        #   每 5 秒補送一次、撐滿 3 分鐘才放棄，90 秒會提早砍掉。
        cap = PORTAL_TIMEOUT if kind == dungeon.PORTAL else STEP_TIMEOUT
        if self._step_t > cap:
            self._stop(f"⛔ 第 {self._i + 1} 步「{dungeon.describe(step)}」"
                       f"卡了 {cap:.0f} 秒還沒完成 —— 停下來")
            if kind == dungeon.PORTAL:
                self._warn(f"傳點過不去：第 {self._i + 1} 步"
                           f"「{dungeon.describe(step)}」站上去 "
                           f"{PORTAL_TIMEOUT:.0f} 秒都沒有被傳走，這一趟停了。")
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
            # ★ 使用者 2026-09-02：「休息要確認周圍沒有可以打到的怪物
            #   才能進入休息」——「休息」是拿來等機關／等對話準備好的，
            #   一邊被打一邊倒數等於白等（而且倒數完就去點機關，人還在挨打）。
            #   ⚠ 正常情況 `_fight` 已經擋在前面；這裡再明寫一次，
            #   而且中途被打斷就**從頭數**（見 `_fight` 挑到目標時歸零）。
            if self._targets():
                self._wait_left = 0.0
                self._say(f"第 {self._i + 1} 步　先別休息：周圍還有 "
                          f"{len(self._targets())} 隻走得到的怪")
                return
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
                # ★ 使用者 2026-09-02 定的規矩，這裡再明寫一次當保險：
                #   「並且周圍（不含走不到的）沒有怪物才算是有走到點位」。
                #   —— 正常情況 `_fight` 已經擋在前面了，但這一條是規格，
                #   寫在完成判定裡才不會被別的改動不小心繞過去。
                if self._targets():
                    self._say(f"第 {self._i + 1} 步　已經站上點位，"
                              f"但周圍還有 {len(self._targets())} 隻走得到的怪"
                              f" —— 先清光才算到")
                    return
                self._nav.reset()
                self._next()
                return
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._close_in(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到 ({gx}, {gy})", (gx, gy))
                    return
            self._say(f"第 {self._i + 1} 步　走到 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}"
                      f"　{self._mon_note()}")
            return

        if kind == dungeon.PORTAL:
            # ★ 完成條件**不是**「走到那一格」而是「人被搬走了」——踩上傳點
            #   的下一瞬間人就被移走，那一格永遠不會「到達」。
            #   換圖那種由 `_check_map_change` 接手；同一張圖裡的順移看這裡。
            if self._jumped:
                land = step.get("land")
                if land and _d(land, me) > LAND_TOL:
                    self._stop(
                        f"⛔ 第 {self._i + 1} 步：傳點把人送到 "
                        f"({me[0]:.0f}, {me[1]:.0f})，"
                        f"腳本記的出口是 ({land[0]:g}, {land[1]:g}) —— "
                        f"差 {_d(land, me):.0f} 格，停下來")
                    return
                self._drop_target()
                self._scan.force_full(self._pid)   # 順移到新的一區＝新的怪
                self._say(f"第 {self._i + 1} 步　傳點過了，落在 "
                          f"({me[0]:.0f}, {me[1]:.0f})")
                self._next()
                return
            gx, gy = step["to"]
            if _d((gx, gy), me) <= PORTAL_NEAR:
                # ★ 已經站在傳點上卻沒被搬走 → 每 PORTAL_POKE 秒對它送一次
                #   互動（有些傳點要點一下才走）。⛔ 不是每一拍狂送：那是
                #   洪水，伺服器會擋（跟補給點 NPC 同一個道理）。
                self._nav.reset()
                self._poke_portal(step, dt)
                return
            if _d((gx, gy), me) <= NAV_DEAD:
                note = self._close_in(gx, gy)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      gx, gy)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到傳點 ({gx}, {gy})", (gx, gy))
                    return
            self._say(f"第 {self._i + 1} 步　走進傳點 ({gx}, {gy})"
                      f"　剩 {_d((gx, gy), me):.1f} 格　{note}")
            return

        if kind == dungeon.INTERACT:
            self._do_interact(step, me, dt)
            return

        self._stop(f"⛔ 第 {self._i + 1} 步是不認得的動作「{kind}」")

    def _poke_portal(self, step: dict, dt: float) -> None:
        """人已經站在傳點上但沒被搬走 —— 每 PORTAL_POKE 秒補送一次互動。

        使用者 2026-09-02：「如果失敗就在傳送點每 5 秒送一次，如果 3 分鐘
        都這樣就結束跳通知警告使用者」。3 分鐘那道閘在 `_run_step` 開頭
        （`PORTAL_TIMEOUT`），這裡只負責補送。
        ⚠ 沒記外觀編號（舊腳本）就只站著等：⛔ 不可以就近亂點一個東西。
        """
        self._poke_t -= dt
        left = PORTAL_TIMEOUT - self._step_t
        want = step.get("model")
        if want is None:
            self._say(f"第 {self._i + 1} 步　站在傳點上等被傳走…"
                      f"（還有 {max(left, 0):.0f} 秒）")
            return
        if self._poke_t > 0:
            self._say(f"第 {self._i + 1} 步　站在傳點上等被傳走…"
                      f"下次補送 {self._poke_t:.1f} 秒（還有 {max(left, 0):.0f} 秒）")
            return
        self._poke_t = PORTAL_POKE
        ax, ay = step["to"]
        props = scenery.nearby(self._sc, (ax, ay), PROP_TOL)
        if props is None:
            self._say("物件清單讀不到，等下一次補送…")
            return
        hit = [p for p in props if p.model == want]
        if not hit:
            # ⛔ 找不到就等，**絕不就近點一個**（點錯東西比不點危險）。
            self._say(f"第 {self._i + 1} 步　傳點附近找不到外觀 {want} 的物件，"
                      f"繼續等（還有 {max(left, 0):.0f} 秒）")
            return
        ok, msg = produce.click(self._mover, self._sc, hit[0])
        self._say(f"第 {self._i + 1} 步　站在傳點上，補送互動"
                  f"（{'送出' if ok else msg}）…還有 {max(left, 0):.0f} 秒")

    def _warn(self, msg: str) -> None:
        """跳通知警告使用者（跟掛機頁同一套 Notifier）。"""
        try:
            if self._notifier is None:
                self._notifier = Notifier(self, title="⚠ 自動刷副本")
            who = self.who.currentText() or "副本"
            note = self._notifier.fire(who, msg)
            self.status.setText(self.status.text() + f"　[{note}]")
        except Exception:                                # noqa: BLE001
            pass                       # 通知送不出去不該再把事情弄糟

    def _do_interact(self, step: dict, me, dt: float) -> None:
        ax, ay = step["at"]
        want_model = step.get("model")
        # ⚠ 先走到旁邊再點：太遠就發互動包＝人還沒到、對話先開，選項送出去
        #   會落空（買東西那條路踩過同一個坑）。
        if _d((ax, ay), me) > TALK_NEAR:
            if _d((ax, ay), me) <= NAV_DEAD:
                note = self._close_in(ax, ay, TALK_KEEP)
            else:
                note = self._nav.step(self._sc, self._mover, self._player,
                                      ax, ay)
                if self._nav.stuck and self._nav.stuck_reason == "grid":
                    self._blocked(dt, f"走不到對話點 ({ax}, {ay})", (ax, ay))
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
            # ★ 基準要在**點下去之前**讀：點完對話可能立刻就開了，
            #   那時再讀就跟第一頁一樣，永遠判不出「有沒有點到」。
            pg0 = talkwnd.page(self._sc)
            ok, msg = produce.click(self._mover, self._sc, hit[0])
            if not ok:
                self._say(f"點不下去（{msg}），重試中…")
                return
            self._clicked = True
            self._menu_i = 0
            # ⚠ 那些全域關著也會留舊值（見 talkwnd）→ 簽章一直沒變＝沒點到。
            self._talk_sig = self._talk_base = (pg0.sig if pg0 else None)
            self._talk_same, self._click_t = 0, 0.0
            self._talk_did = ""
            # ★ 間隔照這一步自己存的（腳本製作那頁可以調）——太快送選項，
            #   伺服器那邊對話還沒準備好就會被拒絕（使用者 2026-09-02）。
            self._menu_t = float(step.get("gap") or MENU_GAP)
            self._say(f"第 {self._i + 1} 步　已點外觀 {hit[0].model}")
            return

        # ★★★ 走對話（使用者 2026-09-02 定案）：
        #   「只要沒選項就幫我對話到結束或出現選項」
        #   → 腳本裡**只記要選第幾項**，沒有選項的那些頁自己按確定過掉。
        #   ⚠ 舊腳本裡記的 0（過場）直接忽略：現在是自動的，再送一次會多按。
        menu = [n for n in (step.get("menu") or []) if n]
        gap = float(step.get("gap") or MENU_GAP)
        self._menu_t -= dt
        if self._menu_t > 0:
            return
        self._menu_t = gap
        pg = talkwnd.page(self._sc)
        # ★ 點下去之後對話完全沒動靜 → 再點一次（不是點一次就不管）。
        if pg is not None and pg.sig == self._talk_base:
            self._click_t += gap
            if self._click_t >= CLICK_RETRY:
                self._click_t = 0.0
                props = scenery.nearby(self._sc, (ax, ay), PROP_TOL) or []
                hit = [p for p in props
                       if want_model is None or p.model == want_model]
                if hit:
                    ok, msg = produce.click(self._mover, self._sc, hit[0])
                    self._say(f"第 {self._i + 1} 步　點了沒反應 → 再點一次"
                              f"（{'送出' if ok else msg}）")
                else:
                    self._say(f"第 {self._i + 1} 步　點了沒反應，"
                              f"而且現在找不到外觀 {want_model} —— 等下一輪")
                return
            self._say(f"第 {self._i + 1} 步　等對話出現…"
                      f"（{self._click_t:.0f}/{CLICK_RETRY:.0f} 秒）")
            return
        if pg is None:
            # 讀不到 Lua 表 → 退化成「照腳本把選項送完就離開」（不自動翻頁）。
            if self._menu_i < len(menu):
                n = menu[self._menu_i]
                if sell.talk(self._mover, supply.talk_option(n)):
                    self._menu_i += 1
                self._say(f"第 {self._i + 1} 步　讀不到對話狀態 → 照腳本送第 {n} 項")
                return
            supply.leave_npc(self._mover)
            self._next()
            return
        # ⚠⚠ 這些全域**關掉對話還會留著**，所以一切以「簽章變了＝換頁了」為準：
        #   · 換頁了 → 上一個動作生效了（送出去的選項才算真的送到）
        #   · 沒換頁 → 還在等伺服器回，**不可以**當成「對話結束」
        #     （使用者 2026-09-02 回報的 bug 就是這樣誤判：明明有選項卻說
        #      「對話結束但腳本還有一個選項沒送到」）
        if pg.sig != self._talk_sig:
            if self._talk_did == "opt":
                self._menu_i += 1           # 有換頁 ＝ 剛剛那一項真的送到了
            self._talk_sig, self._talk_same, self._talk_did = pg.sig, 0, ""
        else:
            self._talk_same += 1
        if not self._talk_did:
            # 這一頁還沒動過 → 決定要做什麼
            if pg.has_options:
                if self._menu_i >= len(menu):
                    # ⛔ 跳出選項但腳本沒說要選哪一項 —— **絕不亂選**。
                    self._stop(f"⛔ 第 {self._i + 1} 步：對話跳出 "
                               f"{len(pg.options)} 個選項，"
                               f"但腳本沒有記要選第幾項 —— 停下來")
                    return
                n = menu[self._menu_i]
                if n not in pg.options:
                    self._stop(f"⛔ 第 {self._i + 1} 步：腳本要選第 {n} 項，"
                               f"但這一頁只有 {list(pg.options)} —— 停下來")
                    return
                if not sell.talk(self._mover, supply.talk_option(n)):
                    self._say(f"第 {n} 項送不出去（指令槽忙碌），重試中…")
                    return
                self._talk_did = "opt"
                self._say(f"第 {self._i + 1} 步　已送第 {n} 項"
                          f"（{self._menu_i + 1}/{len(menu)}）")
                return
            if self._talk_same < TALK_SETTLE:
                # ★ 先確認這一頁是「穩定的沒有選項」才按確定 —— 剛點下去的
                #   那幾拍讀到的可能還是上一次的殘留（那些全域不會被清掉）。
                self._say(f"第 {self._i + 1} 步　等對話出現…")
                return
            ok, why = talkwnd.close_page(self._mover, self._sc)
            self._talk_did = "close"
            self._say(f"第 {self._i + 1} 步　沒有選項的那一頁 → 按確定"
                      f"（{'送出' if ok else why}）")
            return
        # 動過了但畫面沒換頁 —— 分兩種情況，⛔ 不可以都當成「對話結束」
        if self._talk_did == "opt":
            if self._talk_same * gap >= TALK_WAIT:
                self._stop(f"⛔ 第 {self._i + 1} 步：送了第 "
                           f"{menu[self._menu_i]} 項之後 {TALK_WAIT:.0f} 秒"
                           f"都沒有下一頁 —— 停下來")
                return
            self._say(f"第 {self._i + 1} 步　等對話回應…")
            return
        if self._talk_same < TALK_SETTLE:
            self._say(f"第 {self._i + 1} 步　等對話翻頁…")
            return
        # 按了確定又沒有下一頁 ＝ 這段對話走完了
        if self._menu_i < len(menu):
            self._stop(f"⛔ 第 {self._i + 1} 步：對話結束了，"
                       f"但腳本還有 {len(menu) - self._menu_i} 個選項沒送到"
                       f" —— 停下來（NPC 的對話跟腳本記的不一樣？）")
            return
        # ★ 收尾：**先把對話框從畫面上收掉**再送離開互動。
        #   使用者 2026-09-02：「現在都會帶著最後無異議對話離開到處跑，
        #   要把對話框點掉避免 BUG」——`messageclose` 只通知伺服器，
        #   畫面上那個框要叫 Lua 的 DestroyMessageWnd 才會收。
        talkwnd.close_window(self._mover, self._sc)
        supply.leave_npc(self._mover)
        self._next()

    def _next(self) -> None:
        self._i += 1
        self._step_t = 0.0
        self._unreach_t = 0.0
        self._blocked_last = False
        self._menu_i = 0
        self._clicked = False
        self._wait_left = 0.0
        self._empty_since = 0.0
        self._poke_t = 0.0            # 下一步的傳點要馬上補送第一次
        self._nav.reset()
        self._refresh_steps()

    def _finish(self, dt: float) -> None:
        """腳本跑完了 —— 還要**周圍沒有打得到的怪**才算這一趟結束。

        ⚠ 用 `_targets()` 不是 `_live_monsters()`：隔壁區有一隻永遠打不到的
          怪就會讓這一趟永遠收不了工。
        """
        if self._targets():
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
        # ★ 重要提示要**看得到**：同一拍裡 `_refresh_grid` 講的「地形變了」
        #   會被緊接著的走路訊息蓋掉（2026-09-02 實跑時整趟一次都沒顯示到）。
        #   所以 `_notify()` 的內容在 NOTICE_SECS 秒內一直掛在前面。
        if self._notice and time.monotonic() < self._notice_t:
            text = f"{self._notice}　｜　{text}"
        else:
            self._notice = ""
        self.status.setText(text)

    def _notify(self, text: str) -> None:
        """停留幾秒的提示（地形變了、可達區怪怪的…）。"""
        self._notice, self._notice_t = text, time.monotonic() + NOTICE_SECS
        self.status.setText(text)

    def _mon_note(self) -> str:
        """狀態列上的怪物盤點：看得到幾隻、其中幾隻走得到。

        ★ 使用者 2026-09-02 說「走過去才打怪」時，第一件要分清楚的事就是
          「當下到底看不看得到怪」——沒有這一行只能用猜的。
        """
        live = len(self._live_monsters())
        if not live:
            return "（周圍 0 隻怪）"
        return f"（周圍 {live} 隻怪，走得到 {len(self._targets())} 隻）"

    def _close_in(self, gx: float, gy: float, keep: float = 0.0) -> str:
        """最後那 3 格：尋路器已經當「到了」不動作 → 自己直接走過去。

        `keep` > 0 就停在離目標那麼多格（對話點用 —— 機關／NPC 常常站在
        不可走的格上，走到它身上是走不到的）。
        ⚠ 正在走就別再送（重下指令會把上一段打斷）。
        """
        try:
            if entity.is_walking(self._sc, self._player):
                return "走最後一段…"
            if keep > 0:
                self._mover.walk_near(self._sc, self._player, gx, gy, keep)
            else:
                self._mover.walk_exact(self._sc, self._player, gx, gy)
        except Exception:                                # noqa: BLE001
            return "走最後一段（送不出去）"
        return "直接走最後一段（尋路器 3 格內不動作）"

    def _blocked(self, dt: float, what: str, goal=None) -> None:
        """尋路說「現在沒有路」——**不要馬上停**，重讀地形等門開。

        ⛔ 副本的牆是解謎才打開的（使用者 2026-09-02：「地圖之間有可能會用
          牆壁隔開，解謎之後會打開又會變成聯通」）。馬上停＝按完機關的那一拍
          剛好還沒開門就整趟結束（2026-09-02 實跑真的踩到，停在第 4 步）。
        """
        self._unreach_t += dt
        self._blocked_last = True
        self._grid_t = 0.0                    # 下一拍就重讀地形（門可能剛開）
        if self._unreach_t > UNREACH_GRACE:
            self._stop(f"⛔ 第 {self._i + 1} 步：{what} —— 等了 "
                       f"{UNREACH_GRACE:.0f} 秒地形圖還是說沒有路。"
                       f"{self._why_unreachable(goal)}")
            return
        self._say(f"第 {self._i + 1} 步：{what} —— 現在沒有路，"
                  f"重讀地形等門開…{self._unreach_t:.0f}/"
                  f"{UNREACH_GRACE:.0f} 秒")

    def _why_unreachable(self, goal) -> str:
        """停下來時**把證據講出來**：那一格到底是牆，還是在別的區。

        ⚠ 只在要停的那一拍算（多一次泛洪 6ms）—— 使用者看到「走不到」時
          最想知道的就是「是門沒開，還是我腳本點錯地方」。
        """
        if goal is None or self._grid is None:
            return "門沒開？點位在別的區？"
        gx, gy = int(goal[0]), int(goal[1])
        if not self._grid.walkable(gx, gy):
            return (f"（({gx}, {gy}) 那一格在地形圖上**是牆** —— "
                    f"腳本的點位放在不能站的地方？）")
        comp = self._grid.reachable(gx, gy)
        mine = self._reach_n if self._reach is not None else "?"
        return (f"（那一格可以站，但屬於另一區：它那區 "
                f"{len(comp) if comp else '?'} 格、我這區 {mine} 格 —— "
                f"中間的門沒開，或是要先走傳點）")

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        """應用程式關閉前的收尾。

        ⚠⚠ **一定要是 `on_close()` 不是 `closeEvent()`**：分頁是塞在
          QTabWidget 裡的子視窗，Qt 根本不會對它發 close 事件 —— 主視窗
          關閉時走的是 `MainWindow.closeEvent` → 逐個 `tab.on_close()`。
          寫成 closeEvent 等於沒收尾，掃描執行緒還在跑就被解構，
          Qt 丟「QThread: Destroyed while thread '' is still running」，
          嚴重時直接 0xC0000409 當掉（跟自我監察那條同一個坑）。
        """
        self._timer.stop()
        self._stop(quiet=True)
        self._scan.stop()
        self._scan.wait(800)
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
