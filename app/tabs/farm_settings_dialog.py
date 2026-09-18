"""「掛機設定」小視窗（掛機頁右上角那顆鈕開的）。

2026-09-06 使用者要求。目前兩項：
  · **負重設定**＝補給時藥水買到負重的幾 %（原本寫死 95%）
  · **補給要去哪座城**（2026-09-18）＝回城改用趴趴GO 之後的目的地，預設棕櫚基地。
    清單只放有藥水商人的 5 座城（見 `farmsettings.SUPPLY_CITIES`）。

值存 config（`farm.fill_pct` / `farm.supply_city`，見 app/game/farmsettings.py），
**全部分身共用** —— 在哪一台改都一樣。

規則：
  · 改一下就存檔（config.set 接 save），不用按確定 —— 跟「存公會倉庫」小視窗同一套。
  · 值夾在 10~100（farmsettings.FILL_MIN/MAX）；讀取端拿到壞值退回預設 95。
  · 以後要加設定就在這個視窗往下加列，別再開第二顆鈕。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QSpinBox, QVBoxLayout)

from app import theme
from app.game import farmsettings, scene
from app.tabs.base_tab import fit_spin


class FarmSettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("掛機設定")
        v = QVBoxLayout(self)

        form = QFormLayout()
        row = QHBoxLayout()
        self.fill_spin = QSpinBox()
        self.fill_spin.setRange(farmsettings.FILL_MIN, farmsettings.FILL_MAX)
        self.fill_spin.setValue(farmsettings.fill_pct())
        self.fill_spin.setToolTip(
            "回城補給時，精靈頁放的藥水買到負重的這個比例。\n"
            "全部分身共用，改一台全部跟著改。")
        fit_spin(self.fill_spin)
        self.fill_spin.valueChanged.connect(self._on_fill_changed)
        row.addWidget(self.fill_spin)
        row.addWidget(QLabel("%"))         # 單位放框外（使用者 2026-09-06：% 不該在輸入框）
        row.addStretch(1)
        form.addRow("補給時藥水買到負重", row)

        # ── 補給要去哪座城（2026-09-18）────────────────────────────
        # ⚠ 存**場景編號**不存標籤（見 farmsettings.CFG_CITY 的說明）。
        self.city_box = QComboBox()
        for sid in farmsettings.SUPPLY_CITIES:
            self.city_box.addItem(scene.scene_name(sid), sid)
        cur = farmsettings.supply_city()
        i = self.city_box.findData(cur)
        self.city_box.setCurrentIndex(i if i >= 0 else 0)
        self.city_box.setToolTip(
            "回城補給要飛去哪一座城（天使趴趴GO，不燒天使之翼）。\n"
            "清單只有這 5 座城有藥水商人，其他城買不到水。\n"
            "⚠ 人已經站在別的補給城裡時就地補給，不會為了這個設定再飛一趟。\n"
            "全部分身共用，改一台全部跟著改。")
        self.city_box.currentIndexChanged.connect(self._on_city_changed)
        form.addRow("補給要去哪座城", self.city_box)
        v.addLayout(form)

        note = QLabel("這裡的設定全部分身共用；改了立刻存檔，不用按確定。")
        note.setStyleSheet(f"color: {theme.TEXT_MUT};")
        v.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_city_changed(self, _index: int) -> None:
        sid = self.city_box.currentData()
        saved = farmsettings.set_supply_city(sid)
        if saved != sid:                   # 夾過的值寫回畫面（清單以外的值）
            i = self.city_box.findData(saved)
            if i >= 0:
                self.city_box.blockSignals(True)
                self.city_box.setCurrentIndex(i)
                self.city_box.blockSignals(False)

    def _on_fill_changed(self, value: int) -> None:
        saved = farmsettings.set_fill_pct(value)
        if saved != value:                 # 夾過的值寫回畫面（理論上 spin 已限範圍）
            self.fill_spin.blockSignals(True)
            self.fill_spin.setValue(saved)
            self.fill_spin.blockSignals(False)
