"""被動監聽與遊戲伺服器往來的封包，判斷是明文還是加密（診斷版）。

純被動:raw socket 只『收』本機封包，不碰遊戲、不送封包。需系統管理員。

用法(系統管理員終端機、專案根目錄；跑的時候去遊戲走動/打怪產生流量)：
    py tools/sniff.py
    py tools/sniff.py 20.255.63.119        # 只指定伺服器 IP
"""
from __future__ import annotations

import math
import socket
import struct
import sys
import time
from collections import Counter, defaultdict

SERVER = sys.argv[1] if len(sys.argv) > 1 else "20.255.63.119"
SECONDS = 60


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((SERVER, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    c = Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def hexdump(data: bytes, width: int = 16, maxlen: int = 80) -> str:
    out = []
    for i in range(0, min(len(data), maxlen), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {i:04X}  {hexs:<{width*3}} {text}")
    if len(data) > maxlen:
        out.append(f"    …(共 {len(data)} bytes)")
    return "\n".join(out)


def main() -> int:
    ip = local_ip()
    print(f"本機 {ip}，監聽與 {SERVER} 往來的所有封包（TCP/UDP，需管理員）", flush=True)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
        s.bind((ip, 0))
        s.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
    except (PermissionError, OSError) as e:
        print(f"✗ 建立 raw socket 失敗：{e}\n請以『系統管理員』身分執行。")
        return 1

    s.settimeout(1.0)
    total = 0
    server_pkts = 0
    flows = defaultdict(lambda: [0, 0])  # (proto,dir,port) -> [count, bytes]
    s2c_payload = bytearray()
    samples = []
    t0 = time.time()
    last_beat = t0
    print(f"開始監聽 {SECONDS} 秒…請切到遊戲走動/打怪。\n", flush=True)
    try:
        while time.time() - t0 < SECONDS:
            try:
                pkt = s.recv(65535)
            except socket.timeout:
                pkt = None
            if pkt and len(pkt) >= 20:
                total += 1
                ihl = (pkt[0] & 0x0F) * 4
                proto = pkt[9]
                src = ".".join(str(b) for b in pkt[12:16])
                dst = ".".join(str(b) for b in pkt[16:20])
                if SERVER in (src, dst) and proto in (6, 17):
                    server_pkts += 1
                    hdr = pkt[ihl:]
                    if proto == 6 and len(hdr) >= 20:  # TCP
                        sport, dport = struct.unpack(">HH", hdr[0:4])
                        thl = (hdr[12] >> 4) * 4
                        payload = hdr[thl:]
                        pname = "TCP"
                    elif proto == 17 and len(hdr) >= 8:  # UDP
                        sport, dport = struct.unpack(">HH", hdr[0:4])
                        payload = hdr[8:]
                        pname = "UDP"
                    else:
                        payload = b""; sport = dport = 0; pname = "?"
                    if src == SERVER:
                        direction, port = "伺服器→我", sport
                        if payload:
                            s2c_payload += payload
                            if len(samples) < 6 and len(payload) > 0:
                                samples.append((pname, len(payload), payload))
                    else:
                        direction, port = "我→伺服器", dport
                    key = (pname, direction, port)
                    flows[key][0] += 1
                    flows[key][1] += len(payload)
            now = time.time()
            if now - last_beat >= 3:
                print(f"  …已收封包 {total}（與伺服器相關 {server_pkts}）", flush=True)
                last_beat = now
    finally:
        try:
            s.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
        except Exception:
            pass
        s.close()

    print("\n" + "=" * 64)
    print(f"總收封包 {total}，與伺服器相關 {server_pkts}")
    if total == 0:
        print("→ 一個封包都沒收到：raw socket 沒在這介面生效(可能有VPN/多網卡)。")
        return 0
    if server_pkts == 0:
        print("→ 有收到其他封包，但沒有與遊戲伺服器往來的 → 遊戲流量可能走別的介面/IP。")
        return 0

    print("\n流向統計(協定/方向/埠 → 封包數, 資料量bytes)：")
    for (pname, d, port), (c, b) in sorted(flows.items(), key=lambda x: -x[1][0]):
        print(f"  {pname:4} {d}  埠{port:<6} → {c} 個, {b} bytes")

    print("\n伺服器→我 的封包樣本：")
    for pname, ln, pl in samples:
        print(f"  [{pname} {ln} bytes 亂度{entropy(pl):.2f}/8]", flush=True)
        print(hexdump(pl))

    if s2c_payload:
        ent = entropy(bytes(s2c_payload))
        printable = sum(1 for x in s2c_payload if 32 <= x < 127) / len(s2c_payload)
        zeros = s2c_payload.count(0) / len(s2c_payload)
        print(f"\n伺服器→我 總資料 {len(s2c_payload)} bytes：亂度 {ent:.2f}/8，"
              f"可列印 {printable*100:.0f}%，零位元組 {zeros*100:.0f}%")
        if ent > 7.5 and zeros < 0.02:
            print("→ 判斷：高亂度、無結構 → 很可能『加密/壓縮』。")
        else:
            print("→ 判斷：有結構 → 很可能『明文或輕度混淆』，可以解析！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
