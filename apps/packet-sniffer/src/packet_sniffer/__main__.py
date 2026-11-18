"""Sniff the connection-service's interfaces and decode edgelab frames on the wire.

Runs inside the connection-service's network namespace, so it sees exactly what that container
sends and receives on both the field and uplink networks.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter

from edgelab_common import env_float, env_int, setup_logging
from edgelab_protocol import HEADER_SIZE, MAGIC, FrameError, decode
from scapy.all import IP, TCP, UDP, AsyncSniffer, PcapWriter, get_if_list

log = logging.getLogger("sniffer")

TCP_PORT = env_int("EMULATOR_TCP_PORT", 9000)
UDP_PORT = env_int("UDP_PORT", 9001)
VERBOSE = os.environ.get("VERBOSE") == "1"
MAGIC_BYTES = MAGIC.to_bytes(2, "big")

stats: Counter[str] = Counter()
lock = threading.Lock()


def on_packet(pkt) -> None:
    if IP not in pkt:
        return
    src, dst = pkt[IP].src, pkt[IP].dst

    if UDP in pkt and pkt[UDP].dport == UDP_PORT:
        data = bytes(pkt[UDP].payload)
        try:
            f = decode(data)
            bump("udp_frames")
            if VERBOSE:
                log.info(
                    "UDP %s -> %s %s seq=%d (%d B)", src, dst, f.msg_type.name, f.seq, len(data)
                )
        except FrameError as e:
            bump("udp_bad")
            log.warning("UDP %s -> %s undecodable: %s", src, dst, e)
        return

    if TCP in pkt:
        tcp = pkt[TCP]
        data = bytes(tcp.payload)
        flags = str(tcp.flags)
        if TCP_PORT not in (tcp.sport, tcp.dport):
            bump("tcp_other")  # e.g. HTTP uplink to the backend
            if any(f in flags for f in "SFR"):
                log.info("TCP %s:%d -> %s:%d [%s] (uplink)", src, tcp.sport, dst, tcp.dport, flags)
            return
        if any(f in flags for f in "SFR"):  # connection lifecycle: SYN / FIN / RST
            log.info("TCP %s:%d -> %s:%d [%s]", src, tcp.sport, dst, tcp.dport, flags)
        if not data:
            return
        bump("tcp_segments")
        # A segment that doesn't start with MAGIC begins mid-frame: TCP split our message.
        aligned = data[:2] == MAGIC_BYTES
        bump("tcp_aligned" if aligned else "tcp_mid_frame")
        if VERBOSE:
            note = "frame start" if aligned and len(data) >= HEADER_SIZE else "MID-FRAME fragment"
            log.info("TCP %s -> %s len=%d [%s] %s", src, dst, len(data), flags, note)


def bump(key: str) -> None:
    with lock:
        stats[key] += 1


def main() -> None:
    setup_logging()
    ifaces = [i for i in get_if_list() if i.startswith("eth")]  # skip lo + tunnel devices
    writer = None
    if os.environ.get("PCAP") == "1":
        os.makedirs("/captures", exist_ok=True)
        path = f"/captures/sniff-{time.strftime('%Y%m%d-%H%M%S')}.pcap"
        writer = PcapWriter(path, sync=True)
        log.info("writing pcap to captures/%s", os.path.basename(path))

    def handle(pkt) -> None:
        if writer:
            writer.write(pkt)
        on_packet(pkt)

    log.info("sniffing %s (tcp:%d, udp:%d); Ctrl-C to stop", ifaces, TCP_PORT, UDP_PORT)
    sniffer = AsyncSniffer(iface=ifaces, prn=handle, store=False)
    sniffer.start()
    interval = env_float("SUMMARY_INTERVAL_S", 5)
    try:
        while True:
            time.sleep(interval)
            with lock:
                snapshot = dict(stats)
                stats.clear()
            log.info("last %.0fs: %s", interval, snapshot or "no traffic")
    except KeyboardInterrupt:
        pass
    finally:
        sniffer.stop()
        if writer:
            writer.close()


if __name__ == "__main__":
    main()
