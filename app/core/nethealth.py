"""網路健檢：分辨「本機網路斷了」跟「遊戲伺服器那頭關門」。

為什麼需要
----------
斷線警報只看得到「跟遊戲伺服器的 TCP 連線消失了」（app/core/netstat.py），
但那個訊號分不出是誰的錯：家裡網路斷線、伺服器維修、單台被踢，看起來全一樣。
自動回連要對症下藥 —— 斷網要等網路回來、維修要等伺服器開門 —— 所以得當場
發探測拿證據，不能用「同時斷幾台」這種猜的（本機斷網也是全部同時斷）。

兩個探測都只是普通的 TCP connect + close，跟瀏覽器開網頁、登入機連伺服器
是同一種動作，不碰遊戲記憶體、不送任何遊戲封包。

⚠ 這些函式會**阻塞到逾時**（最多幾秒），不要在 UI 執行緒直接叫 ——
  自動回連是包在背景執行緒裡用的（見 login_tab 的 _kick_probe）。
"""
from __future__ import annotations

import os
import re
import socket

# 「網際網路通不通」的探測目標：三家公共 DNS 的 TCP 53。用 IP 直連（不查 DNS，
# 免得 DNS 壞掉被誤判成整個斷網）、多家互為備援（單一家故障不會誤報斷網）。
_NET_TARGETS = (("1.1.1.1", 53), ("8.8.8.8", 53), ("208.67.222.222", 53))
_NET_TIMEOUT = 2.0
# 探測遊戲登入伺服器的逾時。伺服器活著時實測連線是毫秒級；給到 3 秒是
# 留給跨海線路抖動 —— 逾時就當「沒開門」，寧可多等一輪也不要誤判活著。
_SRV_TIMEOUT = 3.0


def _tcp_ok(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def internet_up() -> bool:
    """本機連得上網際網路嗎？三個目標任何一個通就算通。"""
    return any(_tcp_ok(h, p, _NET_TIMEOUT) for h, p in _NET_TARGETS)


def login_server_addr(game_dir: str, server_name: str) -> tuple[str, int] | None:
    """某個伺服器（用畫面上的名字，例如「雅典娜」）的登入端點 (ip, port)。

    讀遊戲資料夾的 server.xml —— 跟 login._servers_from_xml 讀同一份檔案，
    但這裡要的是 ip/port 欄位（記憶體那份陣列要遊戲開著才讀得到，而自動回連
    探測時分身可能全關了，所以只能走檔案）。查不到回 None，呼叫端把「不知道」
    當成「跳過這道閘門」而不是猜一個位址去連。
    """
    try:
        with open(os.path.join(game_dir, "server.xml"),
                  "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return None
    # <名稱 編號="1" 繁="雅典娜" …> → 先把顯示名對回編號
    names = {t: n for n, t in re.findall(
        r'<名稱\s+編號="(\d+)"\s+繁="([^"]*)"', raw)}
    sid = names.get(server_name)
    if sid is None:
        return None
    # <伺服器 名稱="1" … ip="…" port="…" …>（「名稱」欄放的其實是編號）
    m = re.search(
        r'<伺服器\s+名稱="' + re.escape(sid) + r'"[^>]*?\bip="([^"]+)"'
        r'[^>]*?\bport="(\d+)"', raw)
    if not m:
        return None
    ip, port = m.group(1), int(m.group(2))
    return (ip, port) if 1 <= port <= 65535 else None


def server_up(addr: tuple[str, int]) -> bool:
    """遊戲登入伺服器開著門嗎？維修中通常整個端口都不應答。"""
    return _tcp_ok(addr[0], addr[1], _SRV_TIMEOUT)
