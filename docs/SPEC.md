# edgelab: Spec

A small Python system for learning the networking and reliability ideas that come up in **edge / IoT / test-rig** interviews. Build it, then break it on purpose.

```text
┌────────────┐  TCP :9000 (telemetry + heartbeat)  ┌────────────────────┐  HTTP :8080   ┌───────────────────┐
│  emulator  │ ──────────────────────────────────▶ │ connection-service │ ────────────▶ │ backend-collector │
│ (test rig) │  UDP :9001 (high-rate, lossy)       │ ingest → SQLite →  │  POST /ingest │ idempotent store  │
│            │ ──────────────────────────────────▶ │ batch uplink       │               │                   │
└────────────┘                                     └────────────────────┘               └───────────────────┘
   network: field (internal)                         ▲ dual-homed            network: uplink
                                       packet-sniffer shares its network namespace
```

Everything runs in Docker Compose behind a root `Makefile`. The host needs only Docker and `make`.

## 1. Layout

```text
apps/                     # one deployable per folder: pyproject.toml, Dockerfile, src/, tests/
  emulator/               # mock sensor / test rig
  connection-service/     # edge gateway: ingest, buffer, uplink
  backend-collector/      # mock cloud
  packet-sniffer/         # scapy sniffer that decodes our frames on the wire
shared/                   # libraries used by 2+ apps (never runnable on their own)
  protocol/               # edgelab_protocol: wire format, seq tracking (stdlib only)
  common/                 # edgelab_common: env config, logging, backoff, signal handling
docs/                     # this spec, later concept notes
```

- **uv workspace** with a single `uv.lock`. Each app installs only its own deps plus the shared libraries it declares (`uv sync --package <app>`).
- Apps never import each other. Shared code goes in `shared/` once a second app needs it.
- Every app Dockerfile builds from the repo root (it needs `shared/` and `uv.lock`).

## 2. Wire protocol (`shared/protocol`)

A frame is **header (28 B) + payload + CRC32 (4 B)**, all big-endian (`struct` prefix `!`).

| Field | Type | Notes |
|---|---|---|
| `magic` | u16 | `0xCA01`. A mismatch means the stream is out of sync |
| `version` | u8 | `1` |
| `msg_type` | u8 | 1 HELLO, 2 TELEMETRY, 3 HEARTBEAT, 4 COMMAND, 5 COMMAND_ACK |
| `device_id` | u32 | Fixed per device |
| `boot_id` | u32 | Random each time the emulator starts. Tells a reboot apart from a sequence-number glitch |
| `seq` | u32 | Per (boot, stream), counts up from 0. Compared with wraparound-safe arithmetic |
| `timestamp_ns` | u64 | Sender wall clock (`time.time_ns()`) |
| `payload_len` | u32 | `≤ MAX_PAYLOAD_SIZE` (4096), checked **before** reading, so a corrupt length can't cause an out-of-memory crash |

The CRC is `crc32(header + payload)`. TELEMETRY payload is `!8f` (accel xyz, gyro xyz, temp, bus voltage), so a frame is 64 B.

**TCP:** `readexactly(28)` → validate → `readexactly(payload_len + 4)`. Never assume one `recv` is one message.

- CRC mismatch: count it and carry on (the length was fine, so frame boundaries are still aligned).
- Bad magic or length: the stream is out of sync, so reconnect.

**UDP:** one datagram is one frame. Track `seq` per device: a jump forward means loss, a step back means reordering or a duplicate. Count both and keep going.

## 3. Apps

### emulator

- TCP server `:9000`. On connect it sends HELLO, then TELEMETRY at `SAMPLE_RATE_HZ` plus a HEARTBEAT every second. Sets `TCP_NODELAY`.
- Sends UDP TELEMETRY to `connection-service:9001` at the same rate, whether or not anyone is listening.
- Faults, set via env: `FAULT_UDP_DROP_PCT`, `FAULT_CRC_CORRUPT_PCT`, `FAULT_TCP_FRAGMENT_BYTES` (writes each frame N bytes at a time).

### connection-service

- **TCP ingest:** connects to the emulator with exponential backoff and full jitter. Every read has a timeout (`TCP_READ_TIMEOUT_S`), so a link that dies silently is caught by missing heartbeats.
- **UDP ingest:** a `DatagramProtocol` on `:9001` with sequence tracking. It never blocks: if the buffer queue is full, the frame is dropped and counted.
- **Watchdog:** for each transport, the link is OK, `DEGRADED` after 500 ms with no valid frame, and `LOST` after 2 s. Uses monotonic time.
- **Buffer:** SQLite in WAL mode with `synchronous=NORMAL`. **One thread owns the connection.** Rows go `PENDING → ACKED` only after the backend confirms. `MAX_BUFFER_ROWS` caps the total (oldest ACKED rows are evicted first, then oldest PENDING, which is counted and logged).
- **Backpressure:** TCP ingest awaits a bounded queue, so a slow disk stops reads and TCP flow control pushes back on the emulator. UDP has no flow control, so it drops instead.
- **Uplink:** sends batches of `BATCH_MAX_ROWS` (50), or whatever has arrived after `BATCH_MAX_WAIT_MS` (250), to `POST /ingest`. Retries with backoff and full jitter. Rows stay PENDING until acknowledged, so a crash means a replay, never a loss.
- **HTTP `:9100`:** `/healthz`, `/metrics` (Prometheus).
- **Shutdown:** on SIGTERM it stops ingest, drains the queue into SQLite, closes cleanly and exits 0.

### backend-collector

- `POST /ingest`: `INSERT OR IGNORE` keyed on `(device_id, boot_id, stream, seq)`, so replays are harmless. Returns `{inserted, duplicates}`.
- `GET /stats` lists the count, min and max seq, and gaps for each stream. `POST /chaos {"fail_rate": 0.5}` returns 503s. Also serves `/healthz` and `/metrics`.

### packet-sniffer

- Runs in the connection-service's network namespace (`network_mode: service:connection-service`, `NET_RAW`).
- Decodes UDP frames, flags TCP segments that don't start on a frame boundary (fragmentation and coalescing made visible), and logs SYN/FIN/RST.
- Prints a summary every 5 s. Set `VERBOSE=1` for every packet, and `PCAP=1` to also write `captures/*.pcap` for Wireshark.

## 4. Key metrics (connection-service)

`edge_rx_frames_total{transport,msg_type}`, `edge_crc_errors_total{transport}`, `edge_framing_errors_total{transport}`, `edge_udp_dropped_frames_total`, `edge_udp_late_frames_total`, `edge_udp_overflow_total`, `edge_link_state{transport}`, `edge_buffer_pending_depth`, `edge_buffer_evicted_total`, `edge_upstream_connected`, `edge_upload_batches_total{result}`, `edge_emulator_connects_total`.

## 5. Make targets

| Target | Does |
|---|---|
| `make up` / `down` / `logs [S=svc]` / `ps` | Stack lifecycle |
| `make build [S=svc]` | Build images |
| `make stop\|start\|restart S=svc`, `make kill S=svc [SIG=KILL]` | Process control for experiments |
| `make metrics` / `make stats` | Show connection-service metrics / backend per-stream stats |
| `make sniff [VERBOSE=1] [PCAP=1]` | Run the packet sniffer in the foreground |
| `make test` / `lint` / `fmt` / `lock` / `shell` | Dev tasks in a throwaway uv container |
| `make clean` | `down -v` and remove captures |

Faults are passed through the environment, e.g. `FAULT_UDP_DROP_PCT=15 make up`.

## 6. Experiments (first pass, run by hand)

| # | Try | Expect | Concept |
|---|---|---|---|
| 1 | `FAULT_TCP_FRAGMENT_BYTES=1 make up`, then `make sniff` | Sniffer shows 1-byte segments, metrics show 0 framing errors | TCP is a byte stream |
| 2 | `make stop S=backend-collector`, wait 15 s, `make start S=…` | Pending depth rises, then drains to 0. `make stats` shows no gaps on the TCP stream | Store-and-forward, backoff |
| 3 | `make kill S=connection-service`, then `make start S=…` | PENDING rows survive and drain. The only gap is the downtime | WAL durability, at-least-once |
| 4 | `FAULT_UDP_DROP_PCT=15 make up` | Dropped count ≈ 15 %. Service stays responsive | UDP loss, seq tracking |
| 5 | `make restart S=emulator` twice | Reconnects. New `boot_id`s in `make stats`, each starting at seq 0 | Session identity |
| 6 | `curl -XPOST localhost:8080/chaos -d '{"fail_rate":0.5}'` | Retries, then duplicates appear in `/stats` totals while rows stay unique | Idempotency |
| 7 | `make kill S=connection-service SIG=TERM` | Exits 0 and logs the shutdown steps | Graceful shutdown, PID 1 |

## 7. Roadmap

- **Pass 1 (now):** everything above.
- **Pass 2:** gRPC uplink and a device-initiated command stream (SET_SAMPLE_RATE end to end, with deadline propagation). Byte-scan resync instead of reconnecting. A chaos overlay (Toxiproxy for TCP, `tc netem` for UDP, `docker network disconnect` for half-open links). An automated verifier and `make chaos-*` tests.
- **Later:** MQTT uplink comparison, mTLS device identity, a Wireshark Lua dissector, MTU/IP fragmentation, a fleet simulation showing the thundering herd with and without jitter, systemd unit with `sd_notify`.

## 8. Interview concept map

| Concept | Where | Question it answers |
|---|---|---|
| Framing, endianness, CRC | `shared/protocol` | "Design a binary protocol for a sensor." |
| TCP byte stream, `TCP_NODELAY` | `protocol.read_frame`, emulator | "Why can't one `recv` = one message?" |
| Timeouts, heartbeats vs keepalive | connection-service TCP ingest | "How do you detect a pulled cable?" |
| UDP loss and reordering, seq numbers, boot IDs | UDP ingest, `protocol.seq` | "UDP vs TCP for telemetry? Device rebooted, now what?" |
| Watchdog, null vs zero | watchdog | "Sensor reads 0 vs sensor gone?" |
| Backpressure | queue + TCP ingest | "Producer faster than consumer?" |
| Store-and-forward, WAL | buffer | "Device offline for 6 h?" |
| At-least-once + idempotency | uplink + backend | "How do you get exactly-once?" |
| Backoff + jitter | `edgelab_common.backoff` | "10k devices reconnect at once?" |
| Signals, PID 1, graceful shutdown | `edgelab_common.wait_for_shutdown`, `init: true` | "How do you run this as a service?" |
| Network segmentation | compose networks | "How do you isolate field devices?" |
| Packet capture | packet-sniffer | "Walk me through debugging a flaky link." |
