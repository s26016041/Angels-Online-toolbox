# _pushReleases — push 並發布最新版 Release

把目前的變更 push 上 GitHub，並確保 GitHub Releases 的最新版跟上。
使用者打這個指令＝已授權建 Release，不必再另外確認。

## 通用流程（任何專案都照這個順序判斷）

1. **盤點現況**（三件事並行查）：
   - 本地版號：找專案的版號來源（`VERSION` 檔、`package.json` 的 version、
     `pyproject.toml`、`Cargo.toml`……擇一，以專案實際用的為準）。
   - GitHub 最新 Release：`gh release list --limit 5`。
   - `git status` / `git log origin/main..HEAD`：有沒有未 commit、未 push 的東西。
2. **判斷版號要不要改**：
   - 本地版號 ≤ GitHub 最新 Release 的版號，且這次有新變更 → **要 bump**
     （沒特別說就 patch +1）。
   - 本地版號已經比 Release 新（例如上次改了沒發）→ 不用再改，直接用它。
   - 版號變更要 commit（訊息慣例：`chore: 版號 X.Y.Z`）。
3. **建置＋驗證**：找專案的建置／測試腳本先跑過（本專案見下方）。
   **驗證沒過就停下來回報，不准 push 也不准發 Release。**
4. **Push**：`git push origin main`（或該專案的主幹分支）。
5. **建 Release**：優先用專案自己的發布腳本；沒有的話
   `gh release create v<版號> --title v<版號> --notes <重點變更>`，
   有建置產物就一併上傳。
6. **收尾回報**：Release 網址＋這版包含哪些 commit。

## 本專案（Angels-Online-toolbox）的具體指令

```
1. 改 VERSION（無結尾換行，格式如 0.4.5）→ git commit
2. py build_local.py            # 編譯 + 冒煙測試，必須 ✅ 才能往下
3. git push origin main
4. py release.py                # 讀 VERSION → 編 exe → gh 建 Release（含上傳兩份 exe）
```

注意事項（踩過的坑，詳見 memory `packaging-and-release`）：
- 主控台是 cp950，跑 Python 一律 `PYTHONUTF8=1`＋用 `py` 不是 `python`。
- `release.py` 有 y/N 互動確認，PowerShell 背景餵不進去；
  要用 **Bash 工具**跑：`echo y | PYTHONUTF8=1 py release.py`。
- 建置輸出很長：導到 log 檔（scratchpad），只看結尾判定，別整份倒進對話。
- Release 頁上的 `AO.exe` 是 GitHub 濾掉中文檔名的常態，不是上傳壞掉。
- 平常另外要發版前必須先問過使用者（memory `ask-before-release`）；
  唯有使用者主動下這個指令時視同已同意。
