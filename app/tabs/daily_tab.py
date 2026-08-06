"""領取每日分頁。

兩顆按鈕，都是**不必選分身**，按下去對目前開著的每一台各做一輪：

  · 領取在線獎勵     —— 6 格全送（`0x5D3D97(0x48, 獎勵編號)`）
  · 全部換取翔宇聖翼 —— 身上有幾張「勤奮在線獎勵卷」就換幾次
                        （先 `0x5D3D97(0x49, 群組)` 開商店，再送封包 0x148）

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

        root = QVBoxLayout(self)
        hint = QLabel(
            "對目前開著的每一台分身各做一輪，不必選分身、不會動到鍵盤滑鼠，"
            "遊戲視窗在背景也照做。送出的跟你自己在遊戲裡點按鈕是同一個封包。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        self.claim_btn = QPushButton("領取在線獎勵")
        self.claim_btn.setToolTip(
            "對每一台分身送出 6 格的領取（在線 0~60 分鐘那六格）。\n"
            "還沒到時間或已經領過的格子，判定本來就在伺服器，多送不會有影響。")
        self.claim_btn.clicked.connect(self._start_claim)
        bar.addWidget(self.claim_btn)

        self.ex_btn = QPushButton("全部換取翔宇聖翼")
        self.ex_btn.setToolTip(
            "把每一台身上的「勤奮在線獎勵卷」全部換成翔宇聖翼。\n"
            "券有幾張就換幾次；一張都沒有的分身會直接跳過。\n"
            "⚠ 券是在線獎勵發的，放著會過期（1 天），所以是「全部換掉」。\n"
            "⚠ 過程中遊戲會跳出兌換視窗（跟你自己點開一樣），換完自己關掉即可。")
        self.ex_btn.clicked.connect(self._start_exchange)
        bar.addWidget(self.ex_btn)
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
            self.log.setItem(0, c, QTableWidgetItem(cell))
        # 只留最近 LOG_MAX 列，放著跑一整晚也不會吃掉記憶體
        while self.log.rowCount() > LOG_MAX:
            self.log.removeRow(self.log.rowCount() - 1)

    # ------------------------------------------------------------------
    # -- 步驟跑者 --------------------------------------------------------
    def _begin(self, steps: list) -> None:
        """開跑一串步驟。每個步驟做完回傳「下一步要隔幾毫秒」。"""
        self._steps = steps
        self._dead.clear()
        self._sent.clear()
        self._done = 0
        self.claim_btn.setEnabled(False)
        self.ex_btn.setEnabled(False)
        self._tick()

    def _tick(self) -> None:
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
        self._find_gen = None

    # ------------------------------------------------------------------
    # -- 領取在線獎勵 ----------------------------------------------------
    def _start_claim(self) -> None:
        clients = self._clients()
        if not clients:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            return
        n = len(dailygift.REWARD_IDS)
        # ⚠ 第一步先開一個 scanner —— 那會順便跑 locate.warm()（改版位移校正）。
        #   領獎不讀記憶體，少了這一步就會拿沒校正過的位址去呼叫遊戲函式。
        steps = [lambda p=clients[0][0]: self._warm(p)]
        for pid, label in clients:
            for k, rid in enumerate(dailygift.REWARD_IDS):
                steps.append(
                    lambda p=pid, l=label, r=rid, k=k, n=n:
                    self._claim_one(p, l, r, k, n))
        steps.append(lambda n=len(clients): self._say_done(f"領取完成：{n} 台"))
        self.status.setText(f"開始領取：{len(clients)} 台 × {n} 格")
        self._begin(steps)

    def _claim_one(self, pid: int, label: str, rid: int,
                   k: int, n: int) -> int:
        # 跳板裝不上（多半是那台剛關掉）—— 記進 _dead，這台剩下的格子直接跳過，
        # 不然會連跳 6 拍、每拍再失敗一次。
        if pid in self._dead:
            return 0
        mv = self._mover(pid)
        if mv is None:
            self._dead.add(pid)
            self._log(label, "領取在線獎勵", "⚠ 無法安裝跳板（視窗關了？）")
            return 0
        if dailygift.claim(mv, rid):
            self._sent[pid] = self._sent.get(pid, 0) + 1
        if k == n - 1:                                 # 這台的最後一格
            got = self._sent.get(pid, 0)
            self._done += 1
            self._log(label, "領取在線獎勵",
                      f"已送出 {got}/{n} 格"
                      + ("" if got == n else
                         "（其餘排不進指令槽，再按一次就好）"))
        self.status.setText(f"領取中… {label}　第 {k + 1}/{n} 格")
        return TICK_MS

    def _say_done(self, text: str) -> int:
        self.status.setText(text)
        return 0

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
            steps.append(lambda p=pid, l=label: self._ex_open(p, l))
        steps.append(lambda: self._say_done(
            f"兌換完成：{self._done} 台換到東西"
            + (f"，{len(clients) - self._done} 台沒換"
               if self._done < len(clients) else "")))
        self.status.setText("確認兌換項目…")
        self._begin(steps)

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

    def _ex_open(self, pid: int, label: str) -> int:
        """數券、開兌換商店。沒有券就直接跳過這台。"""
        if _entry is None:
            return 0
        sc = self._scanner(pid)
        if sc is None:
            self._log(label, "換翔宇聖翼", "⚠ 讀不到記憶體")
            return 0
        # 券與翼一次讀完 —— 背包讀一遍只要 1ms，但沒必要讀兩遍
        items = bag.items(sc)
        have = sum(it.count for it in items if it.type_id == TOKEN_ITEM)
        wings = sum(it.count for it in items if it.type_id == WING_ITEM)
        times = _entry.times_for(have)
        if times <= 0:
            self._log(label, "換翔宇聖翼", f"沒有{self._name(TOKEN_ITEM)}")
            return 0
        mv = self._mover(pid)
        if mv is None:
            self._log(label, "換翔宇聖翼", "⚠ 無法安裝跳板（視窗關了？）")
            return 0
        if not exchange.open_shop(mv, _entry.group):
            self._log(label, "換翔宇聖翼", "⚠ 開兌換商店排不進去，再按一次")
            return 0
        self.status.setText(f"{label}：{have} 張券 → 換 {times} 次")
        self._steps.insert(0, lambda: self._ex_confirm(
            pid, label, times, have, wings))
        return OPEN_WAIT_MS

    def _ex_confirm(self, pid: int, label: str, times: int,
                    have: int, wings: int) -> int:
        sc = self._scanners.get(pid)
        mv = self._movers.get(pid)
        if sc is None or mv is None or _entry is None:
            return 0
        ok, why = exchange.confirm(mv, sc, _entry.id, times)
        if not ok:
            self._log(label, "換翔宇聖翼", f"⚠ {why}")
            return 0
        self._steps.insert(0, lambda: self._ex_check(pid, label, have, wings))
        return SETTLE_MS

    def _ex_check(self, pid: int, label: str, have: int, wings: int) -> int:
        """回頭讀背包，報告真的換到幾個 —— 只說「送出了」看不出有沒有成功。"""
        sc = self._scanners.get(pid)
        if sc is None:
            return 0
        items = bag.items(sc)
        now_tok = sum(it.count for it in items if it.type_id == TOKEN_ITEM)
        now_wing = sum(it.count for it in items if it.type_id == WING_ITEM)
        used, got = have - now_tok, now_wing - wings
        if got > 0:
            self._done += 1
            self._log(label, "換翔宇聖翼",
                      f"★ 換到 {got} 個{self._name(WING_ITEM)}"
                      f"（用掉 {used} 張券，還剩 {now_tok} 張）")
        else:
            self._log(label, "換翔宇聖翼",
                      f"⚠ 背包沒變多（券還有 {now_tok} 張）—— 伺服器沒接受")
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
