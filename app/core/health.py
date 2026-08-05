"""自我監察：確認「讀遊戲記憶體」的那一套還準不準。

遊戲改版會讓寫死的位址與版面失效。`app/game/locate.py` 的 AOB 能自動追上
「位址整批位移」，但追不上「函式本體被重寫」或「物件欄位偏移改變」——
那兩種只能重新逆向。這支的工作就是**在使用者被錯誤資料誤導之前先發現**。

## ⚠⚠ 最重要的設計：寧可漏報，不可誤報

誤報的代價很高 —— 會跳出「請等作者出新版」然後把程式關掉。所以：

* **一個項目要在「所有分身」上都失敗才算壞掉。** 遊戲改版會讓每一台都壞；
  只有一台失敗多半是那台在讀取畫面、剛換地圖、或正在重連。
* **沒有任何分身時直接跳過**（沒開遊戲不是壞掉）。
* 每個檢查都是**純讀取**，而且只認「明確讀不到」，不對數值內容做判斷。

`tools/selfcheck.py` 用的是同一份檢查，所以指令列與程式內看到的結果一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core import charname, preload
from app.core.memory import MemoryScanner


@dataclass
class ClientResult:
    """一台分身的檢查結果：項目名 -> (過了嗎, 說明)。"""

    account: str
    name: str
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)


@dataclass
class Report:
    clients: list[ClientResult] = field(default_factory=list)
    # 全域項目（跟分身無關），目前只有 AOB 定位
    locate_failed: list[str] = field(default_factory=list)
    locate_moved: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        """沒有分身可檢查 —— 什麼結論都不該下。"""
        return not self.clients

    @property
    def broken(self) -> list[str]:
        """真的壞掉的項目：AOB 失敗，或某個項目在**每一台**上都失敗。"""
        out = [f"位址定位失敗：{n}" for n in self.locate_failed]
        if not self.clients:
            return out
        names = {k for c in self.clients for k in c.checks}
        for key in sorted(names):
            got = [c.checks.get(key) for c in self.clients]
            got = [g for g in got if g is not None]
            if got and all(not ok for ok, _ in got):
                out.append(key)
        return out


def check_client(sc, hwnd: int, account: str,
                 deep: bool = False, should_stop=None) -> ClientResult:
    """跑完一台分身的所有檢查。純讀取。

    deep=True 才做「背包物品陣列」那一項 —— 它要全記憶體掃兩次，
    **一台就要 1.5 秒**（五台 8 秒）。開程式時的背景監察不能這麼慢：
    那條執行緒會一直吃 GIL，主視窗就遲遲畫不出來。
    手動跑 `tools/selfcheck.py` 時才開。

    should_stop: 可選 callable，回傳 True 就盡快收手（關程式時用）。
        中途收手的報告是不完整的，呼叫端不該拿它下「壞掉了」的結論。
    """
    from app.game import channel, energy, entity, locate, monsters
    from app.game import player, scene

    locate.warm(sc)
    res = ClientResult(account=account, name=account)

    def put(key, ok, detail=""):
        res.checks[key] = (bool(ok), detail)

    # ⚠ 成功也要記這一項：broken 是拿**同名項目**跨分身聚合的（見 Report.broken），
    #   成功的分身若沒有這個鍵，只要一台 OpenProcess 失敗（剛關遊戲、正在登入），
    #   聚合起來就是「所有有這鍵的分身都失敗」→ 誤報壞掉 → 整個程式被關掉。
    put("讀取記憶體", True)

    st = pl = None
    try:
        st, pl, ents, _r, _e = entity.snapshot(sc, should_stop=should_stop)
    except Exception as exc:                   # noqa: BLE001
        ents = []
        put("怪物掃描", False, str(exc))
    else:
        put("怪物掃描", True, f"{len([e for e in ents if e.is_monster])} 隻")
    put("狀態物件", st is not None, st and hex(st) or "")
    put("玩家物件", pl is not None, pl and hex(pl) or "")
    pos = entity.read_pos(sc, pl) if pl else None
    put("玩家座標", bool(pos), str(pos) if pl else "")
    mons = [e for e in ents if e.is_monster]
    put("怪物死活狀態", all(m.state for m in mons) if mons else True,
        f"{sum(1 for m in mons if m.dead)} 具屍體")

    here = scene.current(sc)
    put("目前地圖", here is not None, str(here))

    idx = monsters.index_base(sc)
    put("怪物範本表", idx is not None,
        str(monsters.info(sc, mons[0].type_id, idx)) if (mons and idx) else "")

    if should_stop is not None and should_stop():
        return res                 # 被叫停：剩下的都是全記憶體掃描，別再開始

    # ⚠ channel.count() 是全記憶體掃描，只做一次（以前條件式和字串各叫一次，
    #   等於每台分身白掃一輪）。
    cur_ch = channel.current(hwnd)
    n_ch = channel.count(sc, hwnd, should_stop=should_stop)
    put("分流資訊", cur_ch is not None and n_ch is not None,
        f"{cur_ch} / {n_ch}")

    es = energy.read(sc, st) if st else None
    put("能量晶化欄位", es is not None, f"能量 {es.energy}" if es else "")
    put("屬性名稱", len(energy.attr_names(sc)) == energy.ATTR_COUNT)

    if should_stop is not None and should_stop():
        return res

    base = player.locate(sc, should_stop=should_stop)
    stats = player.read(sc, base) if base else None
    put("角色屬性", stats is not None,
        f"Lv{stats.level}" if stats else "")

    # ★ 背包要能「走得完」，不是只找得到表頭。
    #   踩過的坑：表頭找到了，但物品陣列剛好貼著記憶體區段的開頭，
    #   `_walk` 往前多讀 24 格就整批失敗 → 所有物品都數成 0
    #   → 判定「藥水沒了」而且「沒有回程道具」，掛機被停掉（見 inventory._walk）。
    #   數量會因為找不到表頭而是 None，那是「還沒定位」，不算壞。
    if not deep:
        return res                 # 淺層到此為止（開程式時的背景監察走這條）
    try:
        from app.game import aob, inventory
        hits = aob.scan(sc, aob.SKILL_EXP_BALL, limit=4096,
                        should_stop=should_stop)
        head = inventory.locate(sc, {a - inventory.ITEM_BALL_OFF for a in hits})
        n = sum(1 for _ in inventory._walk(sc, head)) if head else None
    except Exception as exc:                   # noqa: BLE001
        head, n = None, None
        put("背包物品陣列", False, str(exc))
    else:
        put("背包物品陣列", head is None or bool(n),
            f"{n} 件" if head else "還沒定位")
    return res


def check(deep: bool = False, should_stop=None) -> Report:
    """檢查所有開著的分身。沒有分身時回傳空報告（`skipped` 為 True）。

    should_stop: 可選 callable，回傳 True 就盡快收手（關程式時用）。
        ⚠ 被叫停的報告不完整，呼叫端要自己丟掉、不要拿去判斷「壞掉了」。
    """
    from app.game import locate

    rep = Report()
    wins = preload.windows()
    if not wins:
        return rep
    for w in wins:
        if should_stop is not None and should_stop():
            break
        acc = charname.account_from_title(w.title) or ""
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            got = check_client(sc, w.hwnd, acc, deep=deep,
                               should_stop=should_stop)
            got.name = preload.name_of(w.pid, sc, acc)
            rep.clients.append(got)
        except Exception as exc:               # noqa: BLE001
            bad = ClientResult(account=acc, name=acc)
            bad.checks["讀取記憶體"] = (False, str(exc))
            rep.clients.append(bad)
        finally:
            sc.close()
    rep.locate_failed = locate.failed()
    rep.locate_moved = locate.moved()
    return rep
