# _patchCheck（改版體檢）— 遊戲更新後，自動找出哪裡壞了並修好

天使之戀官方改版之後跑這一支。使用者不必再解釋一次背景，照下面做。

**核心工具是 `tools/patch_doctor.py`** —— 它做完診斷、產生「已驗過唯一」的
新特徵，你只要判斷＋套用＋驗證。原理與這套機制怎麼來的見 memory
`patch-2026-08-11`、`aob-auto-locate`。

---

## 0. 先確認真的改版了

```
Get-Item "D:\AngelsOnline\Angels Online Global\angel.dat" | Select LastWriteTime
Get-Content "D:\AngelsOnline\Angels Online Global\patch.log" -Tail 6
```

⚠ `patch.log` 寫 `no update needed` **不能當作沒改版** —— 要看 angel.dat 的檔案時間。
⚠ 順便看 `UPDATE.PAK` 的時間：它也變了 → 從資源包抄來的寫死表要重新核對（見第 6 步）。

## 1. 跑體檢

遊戲開著（登入畫面就夠）：

```
$env:PYTHONUTF8=1; py tools\patch_doctor.py --dump
```

`--dump` 會把映像存到 `reports/angel_image.bin`，**之後遊戲關了也能離線查**
（`--image reports\angel_image.bin`）。這很重要：診斷常常要來回好幾趟，
不要每次都叫使用者開遊戲。

主控台只印結論；全量在 `reports/patch_doctor.txt`，可貼的修法在
`reports/patch_doctor_fix.md`。

## 2. 讀懂它的四種判定

| 判定 | 意思 | 要做什麼 |
|---|---|---|
| **位移** | 特徵自己跟上了，值變了 | **正常，不用管**。改版當天大部分都長這樣 |
| **壞：沒中** | 函式被重新編譯／程式碼改寫 | 看 fix 檔的「最接近」候選＋反組譯，套新 pattern |
| **壞：模糊** | 特徵不夠獨特，撞到別人 | **先挑對是哪一個**（見下），再照建議往後延伸 |
| **⚠ 只有『只遮目標』那層唯一** | 還沒壞，但**下次改版一定壞** | 順手修掉，fix 檔有現成建議 |

★ 「模糊」最危險：`data` 類會**安靜地保留舊值**，症狀離現場很遠
（2026-08-11 就是這樣 → 自動登入卡在「等遊戲開好」）。

## 3. 「模糊」要怎麼挑對候選

報告裡每個候選都附反組譯。判斷順序：

1. **字串內容**當錨（最強）—— 附近有 `push "某字串"` 就用 `str_at`/`str_val`。
   怪物表 vs 寵物表（`Npc`/`Pet`）、技能表（`Magic`）、`lua.CTX_PTR`
   （`UpdateEquipPetNew`）都是這樣分的。字串位址會移，**內容不會**。
2. **名字定位**（Lua 綁定表）—— 體檢報告的「名字定位交叉驗證」那一段。
   遊戲的 Lua 綁定表是 `{名字, 函式}` 成對，20 支 fn 裡 8 支查得到。
3. **指令骨架**往後延伸 —— fix 檔會算好「多蓋幾 bytes 才唯一」。
   ⚠ 別延伸到跨過 `ret` 進到下一支函式（那不穩）。

⛔ **絕對不要**把要解出來的位址留在特徵裡當錨（tautology）——
那等於拿答案比對答案，`.data` 一位移就整段失敗。這條是硬規則。

## 4. 套用 → 驗證（一定要做完）

改 `app/game/locate.py` 的 SIGS。**如果新特徵的 pattern 位元組是照現在的映像
重抄的，`known` 也要一起改成現在的值**（`_auto_mask` 靠 `known` 認出「哪個
4-byte 視窗是目標」，兩者必須一致）。同一個模組裡寫死的那份退路常數
（例如 `lua.CTX_PTR = 0x...`）也一起更新。

```
py tools\patch_doctor.py --image reports\angel_image.bin   # 應該 0 壞
py tools\verify_sigs.py                                    # 遊戲開著；唯一性＋模擬改版
```

## 5. 結構偏移（版面）壞掉的話

症狀是特徵好好的、功能卻不對。這類**不是位址位移**，是官方在物件裡加減成員。
做法：找一處遊戲自己用那個偏移的程式碼，把 disp32 讀回來。

已知會動的（2026-08-11 那次整批 −0x60）：
`quickbar.TABLE_OFF`、`player.VT_OFF_FROM_MGR`、`robot.ROBOT_READY_OFF`。
前兩個已經做成 `kind="off"` 的特徵會自動跟上；有新的就照那個樣子加。
⚠ `off` 類的目標值不在模組範圍內 → `_auto_mask` 不會自動遮，**pattern 裡要
自己寫 `??`**，不然又是拿答案當錨。

## 6. 進遊戲驗欄位（位址修好之後）

位址對了不代表**版面**對了。登入一台再跑：

```
py tools\selfcheck.py        # 13 項：狀態物件／玩家／座標／地圖／怪物表／背包…
py tools\patch_doctor.py     # 進遊戲後它會自己把 selfcheck 那段跑進報告
```

需要實際登入時：帳密在設定檔（`app.config.config` 的 `accounts`，
`config.deobfuscate()` 解），流程照 `app/game/login.py`
（`sign_in` → `pick_channel` → 等 `character()` → `enter_game`）。
⚠ 動使用者的分身前要有授權；沒說過就先問。

## 7. 資源包（寫死資料表）

`UPDATE.PAK` 換了就要重新解包 —— **那一步是 GUI（`D:\RPGViewer`），只能請使用者做**。
解完之後：

```
py tools\build_item_names.py ; py tools\build_jumpmap.py
py tools\build_skills.py ; py tools\build_skill_names.py ; py tools\build_skill_range.py
py tools\stamp_tables.py        # 核對過才蓋章，否則等於把警示靜音
```

★ **技能射程表可以不等解包就驗**：遊戲把技能範本載進記憶體了，
拿 `assets/skill_range.tsv.gz` 逐筆對 `[TABLE_PTR]+ID*4` 的 +0x50/+0x54
（2026-08-11 實測 19312/19312 全中）。能用記憶體驗的表就別等解包。

## 8. 收尾

* commit（訊息要寫清楚「哪一段、為什麼壞、怎麼修」，改版紀錄很值錢）
* 更新 memory `patch-2026-08-11` 那類的紀錄（新的改版就新開一份 `patch-YYYY-MM-DD`）
* 要發版的話走 `/_pushReleases`（⚠ 建 Release 前要先問過使用者）
