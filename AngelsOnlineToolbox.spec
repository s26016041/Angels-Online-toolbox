# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 打包設定（單一 .exe）。
#
# 重點：本程式的分頁是「動態 import」載入的（app/main_window.py 用 importlib 掃
# app/tabs/ 底下的模組）。PyInstaller 是「靜態」分析，看不到動態 import，所以
# 必須用 collect_submodules('app') 明確把 app.* 全部收進來，否則打包後分頁一個都
# 進不了 exe → 開起來會是「一片白」的空視窗。
#
# 另外：設環境變數 AOT_CONSOLE=1 可以編出「帶主控台的除錯版」（會顯示 traceback、
# 名稱加 -debug 後綴），方便在本機抓打包問題。build_local.py 會用到。
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('VERSION', '.')]
binaries = []
hiddenimports = []

# 動態載入的分頁 / 核心模組（連同它們的相依）都要明確收進來。
hiddenimports += collect_submodules('app')

# 這幾個套件是 lazy import / 有原生 DLL，需要 collect_all 才收得齊。
for pkg in ('keystone', 'pymem', 'pefile'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


DEBUG_CONSOLE = os.environ.get('AOT_CONSOLE', '0') == '1'
APP_NAME = 'AngelsOnlineToolbox' + ('-debug' if DEBUG_CONSOLE else '')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 除錯版帶主控台（看得到 traceback）；正式版無主控台（GUI）。
    console=DEBUG_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
