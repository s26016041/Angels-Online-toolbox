"""自動掛機「通知共用」離線測試 —— offscreen Qt ＋ 假設定檔（不碰遊戲、不碰真 config）。

驗的規格（2026-08-28 使用者：「自動掛機的通知 改成共用 不區分角色」）：
    · 通知那一列（啟用通知／音效 vs Telegram／群組 ID）**所有分身共用一份**
    · 改一台 → 其他分身的頁面**同步跟著變**（不然畫面會說謊：另一頁顯示舊的
      群組 ID，而它送通知時讀的就是自己那一列 → 送到舊的地方去）
    · 從舊版升上來：原本每個角色一份的設定會被**遷移**成共用，不掉設定
    · 不再寫 per-account 的通知鍵
    · 「啟用通知」關掉就真的不送

⚠ 這支自己準備一份假設定：真的 config 是使用者的檔案，測試不可以去改他
  打好的 Telegram 群組 ID。

用法：py tools\\notify_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication          # noqa: E402

from app.tabs import farm_tab                       # noqa: E402

APP = QApplication.instance() or QApplication([])

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeConfig:
    """記憶體版的設定檔（跟 app/config.py 同一套點號路徑規則）。"""

    def __init__(self) -> None:
        self._data: dict = {}
        self.saves = 0

    def get(self, key, default=None):
        node = self._data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key, value) -> None:
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> None:
        self.saves += 1

    def has(self, key) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


CFG = FakeConfig()
farm_tab.config = CFG          # ⚠ 假設定要 patch 進「用到它的模組」


def build_page(account: str, notifier=None):
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        abs(hash(account)) % 60000, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        notifier=notifier, account=account, char_name=account + "角")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.notify = page.notify          # 用真的
    return page


print("① 一開始就是共用鍵，不寫每個角色一份")
a = build_page("甲")
b = build_page("乙")
check("共用鍵建好了",
      CFG.has("farm.notify_on") and CFG.has("farm.notify")
      and CFG.has("farm.tg_id"),
      f"實得 {CFG._data.get('farm', {}).keys()}")
check("沒有 per-account 的通知鍵",
      not CFG.has("farm.甲.notify_on") and not CFG.has("farm.甲.tg_id"))

print("② 改一台 → 另一台的畫面同步跟著變")
a.rb_tg.setChecked(True)                       # 換成 Telegram
a.tg_id.setText("-100123456")
a.tg_id.editingFinished.emit()                 # 使用者離開輸入框
check("乙頁也切到 Telegram", b.rb_tg.isChecked(), "乙頁還停在音效")
check("乙頁的群組 ID 同步", b.tg_id.text() == "-100123456",
      f"實得「{b.tg_id.text()}」")
check("存的是共用鍵", CFG.get("farm.tg_id") == "-100123456"
      and CFG.get("farm.notify") == "telegram",
      f"實得 {CFG.get('farm.tg_id')} / {CFG.get('farm.notify')}")
check("還是沒有 per-account 的通知鍵",
      not CFG.has("farm.甲.tg_id") and not CFG.has("farm.乙.tg_id"))

print("③ 「啟用通知」也是共用的")
a.notify_cb.setChecked(False)
check("乙頁的勾勾跟著放掉", not b.notify_cb.isChecked())
check("共用鍵是 False", CFG.get("farm.notify_on") is False)
a.notify_cb.setChecked(True)

print("④ 同步不會反過來覆蓋（乙頁被套用時不准回存舊畫面）")
b.tg_id.setText("-999")
b.tg_id.editingFinished.emit()                 # 換乙頁改
check("甲頁跟著變成 -999", a.tg_id.text() == "-999", f"實得「{a.tg_id.text()}」")
check("共用鍵就是 -999", CFG.get("farm.tg_id") == "-999",
      f"實得 {CFG.get('farm.tg_id')}")

print("⑤ 從舊版升上來：每個角色一份的設定要遷移成共用，不掉設定")
CFG2 = FakeConfig()
farm_tab.config = CFG2
CFG2.set("farm.丙.notify_on", False)
CFG2.set("farm.丙.notify", "telegram")
CFG2.set("farm.丙.tg_id", "-100777")
c = build_page("丙")
check("畫面吃到舊設定（Telegram）", c.rb_tg.isChecked())
check("畫面吃到舊的群組 ID", c.tg_id.text() == "-100777",
      f"實得「{c.tg_id.text()}」")
check("勾勾也照舊（關著）", not c.notify_cb.isChecked())
check("已經寫進共用鍵", CFG2.get("farm.tg_id") == "-100777"
      and CFG2.get("farm.notify") == "telegram"
      and CFG2.get("farm.notify_on") is False)
d = build_page("丁")                            # 後來才開的另一台
check("後開的分身直接吃共用值", d.rb_tg.isChecked()
      and d.tg_id.text() == "-100777",
      f"實得「{d.tg_id.text()}」")

print("⑥ 通知器讀的就是這一列（純讀，不真的響）")
method, room = c._notifier._settings()
check("讀到 Telegram＋共用群組 ID", (method, room) == ("telegram", "-100777"),
      f"實得 {(method, room)}")

print("⑦ 「啟用通知」關掉就真的不送")
fired: list[tuple[str, str]] = []
fake_notifier = types.SimpleNamespace(
    fire=lambda who, msg: fired.append((who, msg)) or "假通知")
e = build_page("戊", notifier=fake_notifier)
e.notify_cb.setChecked(False)
e.notify("裝備壞了")
check("關著 → 一則都沒送", fired == [], f"實得 {fired}")
e.notify_cb.setChecked(True)
e.notify("裝備壞了")
check("開著 → 送出去了", len(fired) == 1, f"實得 {fired}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
