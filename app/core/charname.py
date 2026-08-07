"""從記憶體的存檔路徑字串解出「目前角色名」。純讀。

遊戲把角色存檔命名為：
    …\\Angels Online Global\\save\\data\\RobotData_1_{帳號}_{角色名}.data
帳號可從視窗標題取得（見 account_from_title），於是搜這個字串就能純記憶體解出角色名
——本尊專屬、重開不會壞、連在登入/選角畫面都抓得到。

改名處理（目前角色分辨）：
    改名後舊存檔仍留在硬碟，記憶體會同時出現新舊名。目前的名字會出現在「活的」地方
    （UI／名冊／顯示），舊名只剩在存檔路徑字串裡。所以多個候選時，取「總出現次數扣掉
    存檔路徑次數（=活的出現次數）最多」的那個為目前角色。
"""
from __future__ import annotations

import collections
import re


def account_from_title(title: str) -> str:
    """'Angels Online Global - fred26016041(雅典娜-3)' → 'fred26016041'。"""
    tail = title.split(" - ", 1)[1] if " - " in title else title
    return tail.split("(", 1)[0].strip()


def channel_from_title(title: str) -> str:
    """'Angels Online Global - fred26016041(雅典娜-3)' → '雅典娜-3'；無括號回傳 ''。

    頻道（伺服器名＋頻道號）就在標題最後的括號裡；玩家切頻道時遊戲會改這串，
    所以只要即時重讀標題就能反映最新頻道。取最後一組括號，避免帳號/角色名裡的括號干擾。
    """
    l = title.rfind("(")
    r = title.rfind(")")
    if 0 <= l < r:
        return title[l + 1:r].strip()
    return ""


def _iter_raw(sc):
    """一段一段吐出區段內容（bytes）。

    ⚠ `_read_region` 回的是指向共用緩衝的 memoryview（下一輪就被蓋掉），
      而且沒有 `.find`／`.count`／`.decode`。這裡的兩個消費者都要用那些
      方法，所以當場轉成 bytes —— 成本跟以前一樣（以前那份複製是在
      `_read_region` 裡面做的）。
    """
    for base, size in sc._iter_regions(writable_only=False):
        raw = sc._read_region(base, size)
        if raw:
            yield bytes(raw)


def read_character_name(sc, account: str) -> str | None:
    """解出該帳號目前的角色名；找不到回傳 None。"""
    if not account:
        return None
    acc_pat = (account + "_").encode("utf-16-le")
    name_re = re.compile(re.escape(account) + r'_([^\\/:*?"<>|.\x00]{1,16})\.')

    # 第一輪：從 {帳號}_{角色名}.xxx 路徑抓候選角色名 + 各自的路徑出現次數
    path_names: collections.Counter = collections.Counter()
    for raw in _iter_raw(sc):
        s = 0
        while True:
            i = raw.find(acc_pat, s)
            if i < 0:
                break
            s = i + 1
            txt = raw[i:i + 120].decode("utf-16-le", errors="ignore").split("\x00")[0]
            m = name_re.match(txt)
            if m:
                path_names[m.group(1)] += 1

    if not path_names:
        return None
    if len(path_names) == 1:
        return next(iter(path_names))

    # 第二輪（僅改名並存時）：算每個候選的總出現次數，扣掉路徑次數 = 活的出現次數，取最多者。
    name_bytes = {nm: nm.encode("utf-16-le") for nm in path_names}
    total: collections.Counter = collections.Counter()
    for raw in _iter_raw(sc):
        for nm, nb in name_bytes.items():
            total[nm] += raw.count(nb)
    return max(path_names, key=lambda nm: total[nm] - path_names[nm])
