"""解析 pktmon 匯出的 pcapng，抽出『伺服器→我』的 TCP 封包，判斷明文/加密。

用法：
    py tools/parse_cap.py cap.pcapng
    py tools/parse_cap.py cap.pcapng 20.255.63.119 18205
"""
from __future__ import annotations

import math
import struct
import sys
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else "cap.pcapng"
SERVER = sys.argv[2] if len(sys.argv) > 2 else "20.255.63.119"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 18205


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    c = Counter(data); n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def hexdump(data: bytes, width=16, maxlen=96) -> str:
    out = []
    for i in range(0, min(len(data), maxlen), width):
        ch = data[i:i + width]
        h = " ".join(f"{b:02X}" for b in ch)
        t = "".join(chr(b) if 32 <= b < 127 else "." for b in ch)
        out.append(f"    {i:04X}  {h:<{width*3}} {t}")
    if len(data) > maxlen:
        out.append(f"    …(共 {len(data)} bytes)")
    return "\n".join(out)


def iter_packets(buf: bytes):
    """產出 (linktype, packet_bytes)。支援 pcapng。"""
    if buf[:4] != b"\x0a\x0d\x0d\x0a":
        raise ValueError("不是 pcapng 檔（pktmon etl2pcap 產出的應該是）")
    # 位元組序由 SHB magic 判定
    endian = "<"
    off = 0
    linktype = 1
    while off + 8 <= len(buf):
        btype, blen = struct.unpack_from(endian + "II", buf, off)
        if btype == 0x0A0D0D0A:  # SHB
            magic = struct.unpack_from("<I", buf, off + 8)[0]
            endian = "<" if magic == 0x1A2B3C4D else ">"
            btype, blen = struct.unpack_from(endian + "II", buf, off)
        elif btype == 0x00000001:  # IDB
            linktype = struct.unpack_from(endian + "H", buf, off + 8)[0]
        elif btype in (0x00000006, 0x00000003):  # EPB / SPB
            if btype == 0x00000006:
                caplen = struct.unpack_from(endian + "I", buf, off + 20)[0]
                data = buf[off + 28: off + 28 + caplen]
            else:
                origlen = struct.unpack_from(endian + "I", buf, off + 8)[0]
                caplen = min(origlen, blen - 16)
                data = buf[off + 12: off + 12 + caplen]
            yield linktype, data
        if blen < 12:
            break
        off += (blen + 3) & ~3


def parse_ip_tcp(pkt: bytes, linktype: int):
    """回傳 (src_ip, dst_ip, sport, dport, payload) 或 None。"""
    # 依 linktype 定位 IP 標頭
    if linktype == 1:  # Ethernet
        if len(pkt) < 14:
            return None
        eth = struct.unpack(">H", pkt[12:14])[0]
        if eth != 0x0800:
            return None
        ip = pkt[14:]
    elif linktype in (101, 12, 228, 0):  # RAW / IPv4
        ip = pkt
    else:
        ip = pkt  # 賭一把當 raw IP
    if len(ip) < 20 or (ip[0] >> 4) != 4:
        return None
    ihl = (ip[0] & 0x0F) * 4
    proto = ip[9]
    if proto != 6:
        return None
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    tcp = ip[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack(">HH", tcp[0:4])
    thl = (tcp[12] >> 4) * 4
    return src, dst, sport, dport, tcp[thl:]


def main() -> int:
    with open(PATH, "rb") as f:
        buf = f.read()
    print(f"讀取 {PATH}（{len(buf)} bytes），伺服器 {SERVER}:{PORT}", flush=True)

    s2c = bytearray()
    samples = []
    n_pkt = n_s2c = n_c2s = 0
    for linktype, pkt in iter_packets(buf):
        r = parse_ip_tcp(pkt, linktype)
        if not r:
            continue
        src, dst, sport, dport, payload = r
        if src == SERVER and sport == PORT:
            n_s2c += 1
            if payload:
                s2c += payload
                if len(samples) < 8:
                    samples.append(payload)
        elif dst == SERVER and dport == PORT:
            n_c2s += 1
        n_pkt += 1

    print(f"TCP 封包：伺服器→我 {n_s2c} 個，我→伺服器 {n_c2s} 個", flush=True)
    if not s2c:
        print("沒有『伺服器→我』的資料。確認 pktmon 有抓、IP/埠正確。")
        return 0

    print("\n伺服器→我 封包樣本：")
    for pl in samples:
        print(f"  [{len(pl)} bytes 亂度{entropy(pl):.2f}/8]")
        print(hexdump(pl))

    ent = entropy(bytes(s2c))
    printable = sum(1 for b in s2c if 32 <= b < 127) / len(s2c)
    zeros = s2c.count(0) / len(s2c)
    print(f"\n伺服器→我 總資料 {len(s2c)} bytes：亂度 {ent:.2f}/8，"
          f"可列印 {printable*100:.0f}%，零位元組 {zeros*100:.0f}%")
    print("=" * 60)
    if ent > 7.5 and zeros < 0.02:
        print("→ 判斷：高亂度、無結構 → 很可能『加密/壓縮』。")
    else:
        print("→ 判斷：有結構(低亂度/多零/可列印) → 很可能『明文或輕度混淆』，可解析！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
