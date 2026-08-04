"""能量晶化分頁。

先做最小可用的一件事：**選一個分身，按一下「能量晶化」**。
背後就是 `app/game/energy.roll()` —— 呼叫遊戲自己的泛用送包函式
`0x5D3D97(0x38, 1)`（跟選定怪物、切換分流同一個函式，只差種類碼）。

為什麼要選分身
--------------
多開時「按一下」一定要指名對誰按，不然行為不確定。所以有一個下拉選單；
只開一個分身時它會自動選好，等於還是「一個按鈕」。

⚠ 不做自動連按。晶化要花能量、結果隨機，按幾次是使用者的決定 ——
  程式不該替他決定，也不該在他沒看著的時候一直按。

⚠ 需要跳板（`move.Mover`，掛在 PeekMessageA 的 IAT 上）才能請遊戲主執行緒
  替我們呼叫函式。第一次按的時候才安裝，關掉分頁時移除。
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from app.core import charname, injector
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import energy, locate, move
from app.tabs.base_tab import BaseTab


class EnergyTab(BaseTab):
    TAB_TITLE = "能量晶化"
    ORDER = 45

    def build_ui(self) -> None:
        self._movers: dict[int, move.Mover] = {}
        self._scanners: list[MemoryScanner] = []
        self._insts: list[tuple[int, str]] = []      # (pid, 顯示名)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "按一下遊戲裡的「能量晶化」。跟你手動點那顆按鈕送出的是同一個封包"
            "（呼叫遊戲自己的函式，加解密都由客戶端處理）。"))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(260)
        bar.addWidget(self.who)
        refresh = QPushButton("重新整理")
        refresh.setToolTip("重新列出目前開著的遊戲分身。")
        refresh.clicked.connect(self.reload_instances)
        bar.addWidget(refresh)
        bar.addSpacing(16)
        self.roll_btn = QPushButton("能量晶化")
        self.roll_btn.setToolTip(
            "送出一次能量晶化。\n"
            "⚠ 每 1 點能量可進行 1 次，屬性隨機 —— 按幾次由你決定，"
            "程式不會自動連按。")
        self.roll_btn.clicked.connect(self._roll)
        bar.addWidget(self.roll_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        self.status = QLabel("尚未選擇分身")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)
        root.addStretch(1)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._insts:
            self.reload_instances()

    def reload_instances(self) -> None:
        self.who.clear()
        self._insts.clear()
        for sc in self._scanners:
            sc.close()
        self._scanners.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                       # noqa: BLE001
                continue
            # 位址可能因改版位移，接上就用 AOB 校正一次（只會真的做一次）
            try:
                locate.warm(sc)
            except Exception:                       # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            try:
                nm = charname.read_character_name(sc, acc) or acc
            except Exception:                       # noqa: BLE001
                nm = acc
            self._scanners.append(sc)
            self._insts.append((w.pid, f"{nm}（{acc}）"))
            self.who.addItem(f"{nm}（{acc}）", w.pid)
        if not self._insts:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        else:
            self.status.setText(f"找到 {len(self._insts)} 個分身")

    # ------------------------------------------------------------------
    def _mover(self, pid: int) -> move.Mover | None:
        """取得（必要時安裝）那個分身的跳板。裝不起來回 None。"""
        mv = self._movers.get(pid)
        if mv is not None:
            return mv if mv.active else None
        try:
            mv = move.Mover(pid, injector.process_path(pid))
            mv.start()
        except Exception as exc:                    # noqa: BLE001
            self.status.setText(f"⚠ 無法安裝跳板：{exc}")
            self._movers[pid] = move.Mover(pid, "")   # 佔位，別一直重試
            return None
        self._movers[pid] = mv
        return mv

    def _roll(self) -> None:
        pid = self.who.currentData()
        if pid is None:
            self.status.setText("請先選一個分身")
            return
        mv = self._mover(int(pid))
        if mv is None:
            return
        t0 = time.time()
        ok = energy.roll(mv)
        ms = (time.time() - t0) * 1000
        who = self.who.currentText()
        self.status.setText(
            f"{'✔ 已送出' if ok else '⚠ 送不出去（指令槽忙碌，再按一次）'}"
            f"　{who}　能量晶化　{ms:.0f} ms")

    def on_close(self) -> None:
        for mv in self._movers.values():
            try:
                mv.stop()
            except Exception:                       # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners:
            sc.close()
        self._scanners.clear()
