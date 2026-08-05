"""查某個行程有沒有「還連著」的 TCP 連線。斷線偵測靠這個。

為什麼需要這個
--------------
實測把某一台分身的網路切掉之後：**視窗還在、玩家物件照樣讀得到，數值只是完全凍住**
（HP、經驗、技能球全部不動），而且解除封鎖後遊戲也不會自己重連。
也就是說「讀不到記憶體」根本不會發生，拿它當斷線訊號等於永遠不會觸發。
會變的只有一件事：**那條連到遊戲伺服器的 TCP 連線消失了。**

用 iphlpapi 的 GetExtendedTcpTable 直接列出連線表再依 PID 過濾。純查詢、不改任何
系統狀態；一次呼叫是微秒等級，每輪叫也不心疼。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
MIB_TCP_STATE_ESTAB = 5



class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _iphlpapi():
    return ctypes.WinDLL("iphlpapi.dll")


def established_pids() -> set[int]:
    """回傳目前有「已建立(ESTABLISHED)」TCP 連線的所有 PID。

    查一次就把整張表拿回來，所以監控多台分身時只要呼叫一次、大家共用結果。
    出任何差錯就回傳空集合 —— 呼叫端會把「查不到」當成不確定而不是斷線，
    寧可漏報也不要因為 API 失敗就對每台狂發假警報。
    """
    try:
        dll = _iphlpapi()
        size = wintypes.DWORD(0)
        # 第一次呼叫只是為了問「要多大的緩衝區」
        dll.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                TCP_TABLE_OWNER_PID_ALL, 0)
        buf = ctypes.create_string_buffer(size.value)
        rc = dll.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                     TCP_TABLE_OWNER_PID_ALL, 0)
        if rc != 0:
            return set()
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(
            ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
            ctypes.POINTER(MIB_TCPROW_OWNER_PID * n)).contents
        return {int(r.dwOwningPid) for r in rows
                if r.dwState == MIB_TCP_STATE_ESTAB}
    except Exception:
        return set()
