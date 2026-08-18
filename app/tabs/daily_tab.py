"""領取每日分頁。

兩顆按鈕，都是**不必選分身**，按下去對目前開著的每一台各做一輪：

  · 領取在線獎勵     —— 6 格全送（`0x5D3D97(0x48, 獎勵編號)`）
  · 全部換取翔宇聖翼 —— 身上有幾張「勤奮在線獎勵卷」就換幾次
                        （先 `0x5D3D97(0x49, 群組)` 開商店，再送封包 0x148，
                         換完叫遊戲自己的開關函式把商店視窗關掉）

背後是 `app/game/dailygift.py` 與 `app/game/exchange.py`，
兩邊都是呼叫遊戲自己的函式組包、送出，加解密由客戶端處理。

為什麼不用選分身
----------------
使用者指定：「不需要分角色，按下去直接領取全部角色的獎勵」。
領獎與兌換沒有互斥問題（每台各做各的），跟能量晶化那種「按一下要指名對誰」不同。

節奏
----
所有工作排成一串「小步驟」，用一個 QTimer 一拍做一步（**不開執行緒** ——
打包 exe 時背景 QThread 沒無頭防護會原生當機，見 memory 的
packaging-and-release）。找兌換編號那個全記憶體掃描也切成小塊夾在步驟裡跑，
所以整個過程畫面都不會凍住。

暫時性失敗自己扛，不丟回給使用者
--------------------------------
「排不進指令槽」（掛機正在用槽）、「背包沒變多」（伺服器忙、開店包沒被受理）
都是**等一下重來就會好**的事 —— 使用者明確要求：工具自己等、自己重試，
**不設重試上限**（有沒有真的換到，看背包就知道，多試不會多換）；
覺得等太久有「暫停」鈕可以隨時喊停。
同一台卡太久時，它的重試會排到隊伍最後（見 _retry），不擋其他分身。
重送都是安全的：領獎＝連點兩下按鈕（伺服器忽略重複）、開店＝再點一次 NPC、
兌換每次送之前都**重新數券**（次數照最新張數算，前一發真的有送到也不會多換）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import bag, dailygift, exchange, itemname, locate, move
from app.tabs.base_tab import BaseTab

TICK_MS = 200             # 領獎：一拍送一包（6 格 × 5 台 ≈ 6 秒，不急）
OPEN_WAIT_MS = 900        # 開兌換商店 → 等伺服器回清單
SETTLE_MS = 900           # 送出兌換 → 等結果寫進背包
RETRY_MS = 250            # 排不進指令槽 → 等這麼久再試。掛機的攻擊迴圈約 50ms
                          #   一拍、拍與拍之間會放開槽，250ms 幾乎一定輪得到。
# ⚠ 重試**沒有上限**（使用者指定：伺服器忙那類問題就一直試，嫌久按「暫停」）。
#   下面兩個門檻不是放棄門檻，是「讓路」門檻：同一台重試超過這個次數，
#   之後的重試改排到隊伍最後，別讓其他分身全排在它後面乾等（見 _retry）。
SLOT_YIELD = 12           # 指令槽重試 12 次（約 3 秒）還進不去 → 開始讓路。
CHECK_TRIES = 4           # 兌換後背包沒變 → 先多看幾拍（每拍隔 SETTLE_MS）——
                          #   伺服器忙的時候只是回得慢，急著重送等於多繞一整輪。
EX_YIELD = 3              # 「開店→兌換→驗背包」整輪重來 3 輪還不成 → 開始讓路。
                          #   背包沒變最常見的原因是開店那包沒被受理（不開店兌換
                          #   伺服器完全不理，見 _ex_begin），所以重來從開店做起。
READ_TRIES = 8            # ⚠ 讀不到背包（還在讀取畫面／換地圖／剛登入）→ 重試
                          #   這麼多次（約 2 秒）。這裡**有上限**而且跟上面那些
                          #   不同款：讀不到不是「伺服器忙」，是這台現在沒東西可
                          #   讀，一直試只會卡住整輪；試完就**大聲說讀不到**，
                          #   絕不把它講成「你沒有券」（見 _count）。
LOG_COLS = ("時間", "分身", "動作", "結果")
LOG_MAX = 200

# ★ 這兩個道具編號是唯二寫死的遊戲資料，但**不會安靜地換錯東西**：
#   `exchange.find()` 要求「獎賞＝翔宇聖翼」且「材料＝兌換券」兩邊都對得上
#   才算數，改版把編號動了就會找不到 → 我們拒絕送出（見下面 _ex_scan）。
WING_ITEM = 25832         # 翔宇聖翼
TOKEN_ITEM = 5346         # 勤奮在線獎勵卷(1天) —— 在線獎勵發的就是這個

# 找到的兌換項目。★ 資料表是資源包來的，每台分身內容都一樣，所以整個工具箱
# 只掃一台就夠（掃一次 ~0.8 秒，之後都是瞬間）。
_entry: exchange.Entry | None = None


class DailyTab(BaseTab):
    TAB_TITLE = "領取每日"
    ORDER = 46                       # 排在能量晶化（45）與販賣裝備（47）之間

    def build_ui(self) -> None:
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        self._steps: list = []                    # 待做的小步驟
        self._jobs: list[tuple[int, str]] = []    # 這一輪要處理的分身
        self._dead: set[int] = set()              # 這一輪已經放棄的分身
        self._sent: dict[int, int] = {}           # pid -> 這一輪送出幾包
        self._done = 0                            # 完成幾台（收尾訊息用）
        self._find_gen = None                     # 分次掃描的產生器
        self._paused = False
        self._summary = None                      # 全部做完時的收尾訊息

        root = QVBoxLayout(self)
        hint = QLabel(
            "對目前開著的每一台分身各做一輪，不必選分身、不會動到鍵盤滑鼠，"
            "遊戲視窗在背景也照做。送出的跟你自己在遊戲裡點按鈕是同一個封包。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        self.claim_btn = QPushButton("領取在線獎勵")
        # ⚠ tooltip 一律短（2026-08-19 使用者：太長太繁瑣，改簡單明瞭）。
        self.claim_btn.setToolTip(
            "對每一台分身把 6 格在線獎勵全領一次（重複送沒影響）。")
        self.claim_btn.clicked.connect(self._start_claim)
        bar.addWidget(self.claim_btn)

        self.ex_btn = QPushButton("全部換取翔宇聖翼")
        self.ex_btn.setToolTip(
            "把每台身上的「勤奮在線獎勵卷」全部換成翔宇聖翼（券 1 天過期）。\n"
            "過程中跳出的兌換商店視窗會自動關掉。")
        self.ex_btn.clicked.connect(self._start_exchange)
        bar.addWidget(self.ex_btn)

        # 重試不設上限，這顆就是「嫌太久」的出口
        self.pause_btn = QPushButton("暫停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip(
            "先停在原地；按「繼續」從停的地方接著做（重送是安全的）。")
        self.pause_btn.clicked.connect(self._toggle_pause)
        bar.addWidget(self.pause_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        # 每台一列的結果，才看得出誰做了、誰沒做到
        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log.setSelectionMode(QTableWidget.NoSelection)
        self.log.verticalHeader().setVisible(False)
        self.log.setAlternatingRowColors(True)
        hh = self.log.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        root.addWidget(self.log)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

        # ⚠ 單次觸發：每一步自己決定下一步要隔多久（開商店要等伺服器回話，
        #   送領獎只要 0.2 秒）。用固定間隔的話快的要陪慢的一起等。
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # -- 共用：分身清單、跳板、記錄 --------------------------------------
    def _clients(self) -> list[tuple[int, str]]:
        """目前開著的分身 [(pid, 顯示名), …]。"""
        out: list[tuple[int, str]] = []
        seen: set[int] = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            acc = charname.account_from_title(w.title)
            # ⚠ 只用預讀快取拿角色名（不帶 scanner 絕不掃描，見 preload）。
            nm = preload.name_of(w.pid)
            out.append((w.pid, f"{nm}（{acc}）" if nm else acc))
        return out

    def _scanner(self, pid: int) -> MemoryScanner | None:
        sc = self._scanners.get(pid)
        if sc is not None:
            return sc
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)               # 改版位移自動校正（全域只做一次）
        except Exception:                              # noqa: BLE001
            sc.close()   # open() 成功但 warm() 炸掉時要收回 handle，不然每一拍洩一個
            return None
        self._scanners[pid] = sc
        return sc

    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，**不要自己 new 一個 Mover** ——
          同一個遊戲行程只能有一份跳板，自己裝會把掛機分頁那份拆掉
          （見 move.acquire 的說明）。
        ⚠ 失敗不要記進 _movers：記了空殼之後這台就永遠裝不上了。
        """
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception:                              # noqa: BLE001
            self._movers.pop(pid, None)
            return None
        self._movers[pid] = mv
        return mv

    def _log(self, who: str, action: str, result: str) -> None:
        row = (time.strftime("%H:%M:%S"), who, action, result)
        self.log.insertRow(0)
        for c, cell in enumerate(row):
            it = QTableWidgetItem(cell)
            it.setToolTip(cell)   # 欄位塞不下被截掉時，滑鼠移上去看得到全文
            self.log.setItem(0, c, it)
        # 只留最近 LOG_MAX 列，放著跑一整晚也不會吃掉記憶體
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)

    # ------------------------------------------------------------------
    # -- 步驟跑者 --------------------------------------------------------
    def _begin(self, steps: list, summary=None) -> None:
        """開跑一串步驟。每個步驟做完回傳「下一步要隔幾毫秒」。

        summary＝全部做完時的收尾訊息（函式，因為 _done 要到那時才是對的）。
        ⚠ 收尾訊息放 _finish 不排進 steps —— 被「讓路」排到隊尾的重試
        會跑在隊尾那步之後，先排好的「完成」會在還沒完成時就顯示出來。
        """
        self._steps = steps
        self._summary = summary
        self._dead.clear()
        self._sent.clear()
        self._done = 0
        self._paused = False
        self.claim_btn.setEnabled(False)
        self.ex_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暫停")
        self._tick()

    def _tick(self) -> None:
        if self._paused:
            return
        if not self._steps:
            self._finish()
            return
        step = self._steps.pop(0)
        try:
            delay = step()
        except Exception as exc:                       # noqa: BLE001
            # 一步炸掉不要讓整輪卡死在「按鈕永遠是灰的」
            self._log("—", "錯誤", f"⚠ {exc}")
            delay = 0
        self._timer.start(int(delay or 0))

    def _finish(self) -> None:
        self._timer.stop()
        self.claim_btn.setEnabled(True)
        self.ex_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暫停")
        self._paused = False
        self._find_gen = None
        if self._summary is not None:
            self.status.setText(self._summary())
            self._summary = None

    def _toggle_pause(self) -> None:
        """暫停／繼續。

        暫停時主按鈕會亮回來 —— 直接按主按鈕＝放掉剩下的步驟、整輪重新開始
        （_begin 會把整串 steps 換掉），重送是安全的所以不會多領多換。
        """
        if not self._paused:
            self._paused = True
            self._timer.stop()
            self.pause_btn.setText("繼續")
            self.claim_btn.setEnabled(True)
            self.ex_btn.setEnabled(True)
            self.status.setText(
                "⏸ 已暫停 —— 按「繼續」接著做，或再按左邊的按鈕整輪重新開始")
        else:
            self._paused = False
            self.pause_btn.setText("暫停")
            self.claim_btn.setEnabled(False)
            self.ex_btn.setEnabled(False)
            self._tick()

    def _retry(self, fn, tries: int, patience: int) -> None:
        """把重試排回隊伍 —— 重試**不設上限**，只決定排哪裡。

        前 patience 次插在最前面（搶快、同一台的順序不亂）；超過就排到
        隊伍最後 —— 一台卡住不放（遊戲凍結、掉線重連中）的時候，
        別讓其他分身全排在它後面乾等。
        """
        if tries < patience:
            self._steps.insert(0, fn)
        else:
            self._steps.append(fn)

    # ------------------------------------------------------------------
    # -- 領取在線獎勵 ----------------------------------------------------
    def _start_claim(self) -> None:
        clients = self._clients()
        if not clients:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            return
        # ⚠ 先開一個 scanner —— 那會順便跑 locate.warm()（改版位移校正）。
        #   領獎本身不讀記憶體，少了這一步就會拿沒校正過的位址去呼叫遊戲函式。
        # ★ 順便把獎勵格編號**現場讀 OnlineGift 表**（改版增減格數自動跟上）；
        #   讀不到 reward_ids() 自己退回寫死的 REWARD_IDS（跟以前一模一樣）。
        sc0 = self._scanner(clients[0][0])
        ids = dailygift.reward_ids(sc0) if sc0 else dailygift.REWARD_IDS
        n = len(ids)
        steps = [lambda p=clients[0][0]: self._warm(p)]
        for pid, label in clients:
            for k, rid in enumerate(ids):
                steps.append(
                    lambda p=pid, l=label, r=rid, k=k, n=n:
                    self._claim_one(p, l, r, k, n))
        self.status.setText(f"開始領取：{len(clients)} 台 × {n} 格")
        self._begin(steps, summary=lambda m=len(clients): f"領取完成：{m} 台")

    def _claim_one(self, pid: int, label: str, rid: int,
                   k: int, n: int, tries: int = 0) -> int:
        # 跳板裝不上（多半是那台剛關掉）—— 記進 _dead，這台剩下的格子直接跳過，
        # 不然會連跳 6 拍、每拍再失敗一次。
        if pid in self._dead:
            return 0
        mv = self._mover(pid)
        if mv is None:
            self._dead.add(pid)
            self._log(label, "領取在線獎勵", "⚠ 無法安裝跳板（視窗關了？）")
            return 0
        if not dailygift.claim(mv, rid):
            # 排不進指令槽（多半是掛機正在用）→ 自己等、自己重試同一格，
            # 試到送出去為止。重送安全：跟連點兩下同一格一樣，伺服器自己會擋。
            self.status.setText(
                f"領取中… {label}　第 {k + 1}/{n} 格"
                f"（指令槽忙，第 {tries + 1} 次重試…嫌久可按「暫停」）")
            self._retry(lambda: self._claim_one(pid, label, rid, k, n,
                                                tries + 1), tries, SLOT_YIELD)
            return RETRY_MS
        self._sent[pid] = self._sent.get(pid, 0) + 1
        if k == n - 1:                                 # 這台的最後一格
            got = self._sent.get(pid, 0)
            self._done += 1
            self._log(label, "領取在線獎勵", f"已送出 {got}/{n} 格")
        self.status.setText(f"領取中… {label}　第 {k + 1}/{n} 格")
        return TICK_MS

    def _warm(self, pid: int) -> int:
        """開一個 scanner 讓 locate.warm() 跑過（位址校正，全域只做一次）。"""
        self._scanner(pid)
        return 0

    # ------------------------------------------------------------------
    # -- 全部換取翔宇聖翼 ------------------------------------------------
    def _start_exchange(self) -> None:
        clients = self._clients()
        if not clients:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            return
        self._jobs = clients
        steps = [self._ex_scan]                        # 先確認兌換項目
        for pid, label in clients:
            steps.append(lambda p=pid, l=label: self._ex_begin(p, l))
        self.status.setText("確認兌換項目…")
        self._begin(steps, summary=lambda m=len(clients): (
            f"兌換完成：{self._done} 台換到東西"
            + (f"，{m - self._done} 台沒換" if self._done < m else "")))

    def _ex_scan(self) -> int:
        """分次掃描找「券 → 翔宇聖翼」那筆兌換。找到才會排後面的步驟。

        ⚠ 找不到就把整輪取消 —— 群組 580 裡有幾十筆同樣「1 張券」的兌換，
          編號猜錯不會失敗，只會換到別的東西。寧可不送。
        """
        global _entry
        if _entry is not None:
            return 0
        if self._find_gen is None:
            sc = next((s for s in (self._scanner(p) for p, _ in self._jobs)
                       if s is not None), None)
            if sc is None:
                self._steps.clear()
                self.status.setText("⚠ 讀不到遊戲記憶體，沒有送出任何東西")
                return 0
            self._find_gen = exchange.finder(sc, WING_ITEM, TOKEN_ITEM)
        try:
            got = next(self._find_gen)
        except StopIteration:
            self._steps.clear()
            self.status.setText(
                f"⚠ 在遊戲裡找不到「{self._name(TOKEN_ITEM)} → "
                f"{self._name(WING_ITEM)}」這筆兌換 —— 沒有送出任何東西"
                "（改版動過兌換表？）")
            return 0
        if got is None:
            self._steps.insert(0, self._ex_scan)       # 還沒掃完，下一拍繼續
            return 0
        _entry = got
        r_id, r_n = got.reward
        m_id, m_n = got.material
        self.status.setText(
            f"兌換項目：{m_n} 個{self._name(m_id)} → "
            f"{r_n} 個{self._name(r_id)}（編號 {got.id}／商店 {got.group}）")
        return 0

    def _count(self, sc) -> tuple[int, int, int] | None:
        """(券, 翔宇聖翼, 讀到幾件)；**讀不到背包回 None**（≠ 沒有券）。

        ⚠⚠ 這支以前是 `bag.items()` 直接加總 —— 讀不到時它回空清單，於是
          「還在讀取畫面／換地圖／位址定位失敗」全被講成「你沒有獎勵券」。
          使用者 2026-08-08 回報：另一台電腦身上有 6 張券卻說沒有。
          現在改用 `bag.scan()` 看「整段真的讀到了嗎」，讀不到就回 None，
          由呼叫端重試／大聲說讀不到（見 READ_TRIES）。

        ★ 數的是**整個物品容器**（0 ~ 上限），不是只有背包那段 0x14~0xA9：
          兌換封包填的是「兌換編號＋次數」，跟格號完全無關，所以券躺在任務
          道具欄那種非背包格照樣換得掉 —— 反而漏數會變成「明明有券卻說沒有」。
          （容器裡的東西本來就都是自己的；倉庫不在這個容器裡，不會多算。）
        """
        items, ok = bag.scan(sc, 0, bag.MAX_SLOTS)
        if not ok:
            return None
        return (sum(it.count for it in items if it.type_id == TOKEN_ITEM),
                sum(it.count for it in items if it.type_id == WING_ITEM),
                len(items))

    def _unreadable(self, sc, label: str, reads: int, again) -> int:
        """背包讀不到時的共同處理：先重試幾次，試完大聲說讀不到。

        ★ 最後那句話分兩種講，因為要做的事完全不同：
          · **沒進遊戲**（登入畫面／選角／讀取中／斷線重連）—— 這時候背包
            物件根本還不存在，讀不到很正常，不是壞掉。
          · **進遊戲了卻讀不到** —— 這才是真的有問題（位址失效那類），
            要去跑 `tools\\selfcheck.py`。
          實測在遊戲裡連讀 1500 次 0 失敗（2026-08-08，五台各 300 次），
          所以第二種訊息一旦出現，就是真的要查，不是運氣不好。
        """
        if reads < READ_TRIES:
            self.status.setText(
                f"{label}：讀不到背包，等一下再看…"
                f"（{reads + 1}/{READ_TRIES}）")
            self._steps.insert(0, again)
            return RETRY_MS
        if bag.player_entity(sc) is None:
            self._log(label, "換翔宇聖翼",
                      "這台還沒進遊戲（登入畫面／選角／讀取中？）—— 跳過")
        else:
            self._log(label, "換翔宇聖翼",
                      "⚠ 進遊戲了卻讀不到背包 —— 這台沒換，"
                      "請跑 tools\\selfcheck.py 看是不是位址失效")
        return 0

    def _ex_begin(self, pid: int, label: str, cycle: int = 0,
                  tries: int = 0, reads: int = 0) -> int:
        """數券 → 開兌換商店。沒有券就跳過這台（連商店都不開）。

        ⚠⚠ **一定要先開商店**：2026-08-07 使用者實測，不開商店直接送兌換封包
          伺服器完全不理（背包沒變）。曾經做過「先試直送、不行才開商店」的
          自動判斷，既然答案已經確定就拿掉了 —— 白送一包還多等 0.9 秒。

        cycle 是「開店→兌換→驗背包」第幾輪重來（見 _ex_check），
        tries 是這一輪裡開店包排不進指令槽重試了幾次。
        """
        if _entry is None:
            return 0
        sc = self._scanner(pid)
        if sc is None:
            self._log(label, "換翔宇聖翼", "⚠ 讀不到記憶體")
            return 0
        got = self._count(sc)
        if got is None:
            return self._unreadable(
                sc, label, reads,
                lambda: self._ex_begin(pid, label, cycle, tries, reads + 1))
        have, wings, seen = got
        times = _entry.times_for(have)
        if times <= 0:
            # ★ 講「背包 N 件裡沒有」不講「沒有」—— 讀得到才敢下這個結論，
            #   件數也讓使用者一眼看出是不是讀到半份（見 _count）。
            self._log(label, "換翔宇聖翼",
                      f"背包 {seen} 件裡沒有{self._name(TOKEN_ITEM)}")
            return 0
        mv = self._mover(pid)
        if mv is None:
            self._log(label, "換翔宇聖翼", "⚠ 無法安裝跳板（視窗關了？）")
            return 0
        if not exchange.open_shop(mv, _entry.group):
            # 指令槽忙 → 自己等、自己重試，試到送出去為止。
            # 重送＝再點一次 NPC，安全。
            self.status.setText(
                f"{label}：開兌換商店（指令槽忙，第 {tries + 1} 次重試…"
                "嫌久可按「暫停」）")
            self._retry(lambda: self._ex_begin(pid, label, cycle, tries + 1),
                        tries, SLOT_YIELD)
            return RETRY_MS
        self.status.setText(f"{label}：{have} 張券 → 換 {times} 次")
        self._steps.insert(
            0, lambda: self._ex_confirm(pid, label, cycle, have, wings))
        return OPEN_WAIT_MS

    def _ex_confirm(self, pid: int, label: str, cycle: int,
                    have0: int, wings0: int, tries: int = 0,
                    reads: int = 0) -> int:
        """商店開好了才送兌換。

        ⚠ 券在這裡**重新數一次**，不沿用開商店之前那個數字 —— 中間隔了 0.9 秒，
          用最新的才不會送出比手上還多的次數。重試時也一樣重數，所以就算
          前一發其實有送到、券已被扣走，這裡會自動變 0 次，不會多換。
        """
        sc, mv = self._scanners.get(pid), self._movers.get(pid)
        if sc is None or mv is None or _entry is None:
            return 0
        got = self._count(sc)
        if got is None:
            # ⚠ 讀不到就**不准送** —— 次數是照張數算的，猜一個送出去就是
            #   拿垃圾值給伺服器。等一下再看，試完把視窗關掉收工。
            delay = self._unreadable(
                sc, label, reads,
                lambda: self._ex_confirm(pid, label, cycle, have0, wings0,
                                         tries, reads + 1))
            if reads >= READ_TRIES:       # 放棄了 → 順手把商店視窗關掉
                self._steps.insert(0, lambda: self._ex_close(pid, label))
            return delay
        have, wings, _seen = got
        times = _entry.times_for(have)
        if times <= 0:
            # 開店時還有券、現在沒了 —— 幾乎一定是前一發其實成功了
            # （逾時≠沒送出）。交給驗背包那步對帳，翅膀有變多就算成功。
            self._steps.insert(0, lambda: self._ex_check(
                pid, label, have0, wings0, cycle, CHECK_TRIES - 1))
            return 0
        ok, why = exchange.confirm(mv, sc, _entry.id, times)
        if ok:
            self._steps.insert(0, lambda: self._ex_check(
                pid, label, have, wings, cycle))
            return SETTLE_MS
        # 排不進指令槽／正在重連之類的暫時狀況 → 等一下再送，試到送出去為止。
        # 每次重送前都重新數券（上面），前一發真的有送到也不會多換。
        self.status.setText(
            f"{label}：兌換（{why}，第 {tries + 1} 次重試…嫌久可按「暫停」）")
        self._retry(lambda: self._ex_confirm(pid, label, cycle, have0, wings0,
                                             tries + 1), tries, SLOT_YIELD)
        return RETRY_MS

    def _ex_check(self, pid: int, label: str, have: int, wings: int,
                  cycle: int, probes: int = 0, reads: int = 0) -> int:
        """回頭讀背包，報告真的換到幾個 —— 只說「送出了」看不出有沒有成功。

        背包沒變不急著判死刑：
          1. 先多看幾拍（CHECK_TRIES）—— 伺服器忙的時候只是回得慢，
             急著重送等於白繞一整輪。
          2. 還是沒動靜就整輪重來，**沒有輪數上限**（伺服器忙是伺服器的事，
             試到換成功為止，嫌久按「暫停」）—— 從**重新開店**做起，
             因為「背包沒變」最常見的原因就是開店那包沒被伺服器受理。
             重來時 _ex_confirm 會重新數券，所以不會多換。
        """
        sc = self._scanners.get(pid)
        if sc is None:
            self._steps.insert(0, lambda: self._ex_close(pid, label))
            return 0
        counted = self._count(sc)
        if counted is None:
            # 讀不到就不能說「沒換到」（那會害它整輪重來、白換一次）。
            delay = self._unreadable(
                sc, label, reads,
                lambda: self._ex_check(pid, label, have, wings, cycle,
                                       probes, reads + 1))
            if reads >= READ_TRIES:       # 放棄了 → 順手把商店視窗關掉
                self._steps.insert(0, lambda: self._ex_close(pid, label))
            return delay
        now_tok, now_wing, _seen = counted
        used, got = have - now_tok, now_wing - wings
        if got > 0:
            self._done += 1
            self._log(label, "換翔宇聖翼",
                      f"★ 換到 {got} 個{self._name(WING_ITEM)}"
                      f"（用掉 {used} 張券，還剩 {now_tok} 張）")
        elif probes + 1 < CHECK_TRIES:
            self.status.setText(
                f"{label}：等伺服器結算…（{probes + 2}/{CHECK_TRIES}）")
            self._steps.insert(0, lambda: self._ex_check(
                pid, label, have, wings, cycle, probes + 1))
            return SETTLE_MS
        else:
            self.status.setText(
                f"{label}：伺服器沒接受，重新開店再換一次"
                f"（第 {cycle + 2} 輪…嫌久可按「暫停」）")
            self._retry(lambda: self._ex_begin(pid, label, cycle + 1),
                        cycle, EX_YIELD)
            return TICK_MS
        self._steps.insert(0, lambda: self._ex_close(pid, label))
        return 0

    def _ex_close(self, pid: int, label: str) -> int:
        """把跳出來的兌換商店視窗關掉（叫遊戲自己的開關函式）。"""
        sc, mv = self._scanners.get(pid), self._movers.get(pid)
        if sc is None or mv is None:
            return 0
        ok, why = exchange.close_window(mv, sc)
        if not ok and why != "視窗沒開著":
            self._log(label, "關兌換視窗", f"⚠ 關不掉（{why}）—— 請自己關")
        return 0

    @staticmethod
    def _name(type_id: int) -> str:
        return itemname.of(type_id) or f"物品 {type_id}"

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._timer.stop()
        self._steps.clear()
        # ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
        #   掛機分頁可能還在用（見 move.acquire）。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                          # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
