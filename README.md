# Angels-Online-toolbox（天使之戀工具箱）

天使之戀多功能 Python 工具箱，採分頁式介面，各功能獨立成一個分頁。

- GUI：PySide6（Qt6）
- 分頁自動載入：在 `app/tabs/` 丟一個 `BaseTab` 子類別的檔案即可，不必改主程式。
- 版本號唯一來源：根目錄 `VERSION` 檔（打包成 exe 時會一併收進去）。

## 功能分頁

| 分頁 | 說明 |
|------|------|
| 自動登入 | 全背景自動登入（只吃打字、不佔用滑鼠鍵盤） |
| 記憶體掃描 | 掃描 / 字串搜尋（開發自用工具） |
| 監控技能經驗球 | 用 AOB 特徵碼定位並監控經驗球數值 |
| 封包 / 登入攔截 | 封包攔截、注入與帳號管理 |
| 視窗診斷 | 列出遊戲視窗、座標等診斷資訊 |

## 安裝

需要 Python 3.12+。**用 `py` 執行（不是 `python`）**。

```
py -m pip install -r requirements.txt
```

## 執行（開發模式）

```
py main.py               # 正常啟動 GUI
py main.py --selftest    # 不開視窗，離線建立主視窗並檢查分頁有無載入（成功 exit 0）
```

## 本地編譯與測試 — `build_local.py`

把程式編成 `.exe` 並在本機驗證，**不會上傳 GitHub**，純本機用。
每個模式編完都會自動跑 `exe --selftest` 冒煙測試，確認分頁真的載入（防「白屏」）。

```
py build_local.py            # 編正式版（無主控台的 GUI）＋ 冒煙測試 → 確認這顆 exe 不會白屏
py build_local.py --debug    # 編「帶主控台」的除錯版（AngelsOnlineToolbox-debug.exe），
                             #   白屏／閃退時可直接在主控台看到 traceback
py build_local.py --run      # 冒煙測試通過後，真的把 GUI 開起來讓你眼睛確認
```

- `--debug` 與 `--run` 可併用：`py build_local.py --debug --run`。
- 產物在 `dist/`；正式版是 `AngelsOnlineToolbox.exe`，除錯版是 `AngelsOnlineToolbox-debug.exe`。
- 亦可雙擊 `build_local.bat`（等同 `py build_local.py`，可帶參數）。

> **為什麼需要它**：本程式的分頁是「動態 import」載入的，PyInstaller 靜態分析看不到，
> 若沒把 `app.*` 子模組收齊，打包後會開出「一片白」的空視窗且不報錯。
> `AngelsOnlineToolbox.spec` 已用 `collect_submodules('app')` 修正，`build_local.py` 則負責
> 在本機把關——編完立刻跑 selftest，分頁沒載入就直接判失敗。

## 發布 — `release.py`

一鍵發布到 GitHub Releases：讀 `VERSION` → 若該版本還沒發過，就用同一份 spec 編 `.exe`、
跑冒煙測試、打 tag、上傳。

```
py release.py            # 或雙擊 release.bat
```

需要先安裝並登入 GitHub CLI：`gh auth login`。

換版本：改根目錄 `VERSION` 檔 → push 到 main → 再跑 `py release.py`。

## 疑難排解

- **打包後的 exe 開起來一片白**：分頁沒被收進 exe。請確認 `AngelsOnlineToolbox.spec` 內有
  `collect_submodules('app')`，並改用 `py build_local.py` 重編（會自動冒煙測試）。
  想看實際錯誤就用 `py build_local.py --debug` 編除錯版。
- **想看崩潰原因**：未捕捉的例外會寫到 `%APPDATA%\AngelsOnlineToolbox\crash.log`，
  正式版也會彈出錯誤訊息框。
- **主控台中文變亂碼**：終端機是 cp950，輸出 Unicode 前設 `PYTHONUTF8=1`。
