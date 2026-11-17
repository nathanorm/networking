# edgelab: Tech Spec

A hands-on Python sandbox for learning the networking and reliability principles used in **edge / IoT / test-rig integration** roles, built as one small system you can break on purpose.

```text
┌──────────────────┐   TCP (ctrl + telemetry)  ┌──────────────────┐   gRPC (HTTP/2)    ┌───────────────────┐
│  hardware-sim    │ ────────────────────────▶ │   edge-daemon    │ ─────────────────▶ │ backend-collector │
│  mock test rig / │   UDP (high-rate IMU)     │  ingest → SQLite │   UploadBatch      │  idempotent store │
│  vehicle sensor  │ ────────────────────────▶ │  WAL → uplink    │ ◀───────────────── │  command router   │
└──────────────────┘ ◀──── COMMAND frames ──── └──────────────────┘   Control.Connect  └───────────────────┘
        network: field (internal)                 dual-homed               network: uplink
```

Everything runs in Docker Compose. A root `Makefile` is the only entry point. The host needs only `docker`, `docker compose` (v2) and `make`.

---

## 1. Goals & Non-Goals

### Goals

1. **Learn by building.** Every component exists to exercise a specific networking concept that comes up in edge/IoT interviews (see the [concept map](#9-concept-map-interview-prep)).
2. **Learn by breaking.** Each concept is paired with a reproducible chaos test (`make chaos-*`) that has objective pass/fail criteria.
3. **Observable.** Every failure mode is visible through metrics, logs, or packet captures, so you can *show* behaviour rather than assert it.
4. **One command per action.** `make up`, `make chaos-outage`, `make capture`, and so on. No hand-typed `docker` incantations.

### Non-Goals

- Production security hardening (mTLS is a stretch module, not core).
- Real hardware, CAN bus, or serial. Docker Desktop on macOS can't load `vcan`; the stretch section covers what to say about it instead.
- Kubernetes or multi-host deployment.
- High throughput. The target is roughly 50 Hz × a handful of streams. Correctness under failure matters more than speed.

---

## 2. Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python **3.13** | Pinned in `.python-version` and the Dockerfile base image |
| Packaging | **uv workspace** (`apps/*` + `shared/*`, one `uv.lock`) | Per-app installs via `uv sync --package`; see §3 |
| Async | `asyncio` (stdlib); `uvloop` optional | All I/O services are single-event-loop |
| Binary protocol | `struct`, `zlib.crc32` | Network byte order (`!`) |
| RPC | `grpcio` / `grpc.aio`, `grpcio-tools` | Protobuf stubs generated at build time |
| Local buffer | `sqlite3` (stdlib), WAL mode | Single dedicated writer thread |
| HTTP (health/metrics/admin) | `aiohttp` | Runs on the same loop as the service |
| Metrics | `prometheus-client` | Text exposition at `/metrics` |
| Logging | stdlib `logging`, JSON formatter | Every line carries `device_id`, `boot_id` |
| Tests | `pytest`, `pytest-asyncio`, `hypothesis` | Hypothesis for framing/fragmentation property tests |
| Lint / format / types | `ruff` (lint + format), `mypy --strict` | Run in the `tools` container |
| Network chaos | **Toxiproxy** (TCP), **tc netem** via `nicolaka/netshoot` (UDP/IP) | Pin both images by tag or digest |
| Packet capture | `tcpdump` in `netshoot`, sharing the target's network namespace | Writes `.pcap` to `./captures/` for Wireshark |

---

## 3. Repository Layout

A **uv workspace monorepo**:

- `apps/` holds one directory per deployable application. Each app has its own `pyproject.toml`, `Dockerfile`, `src/` and `tests/`.
- `shared/` holds library code imported by two or more apps. Nothing in `shared/` is runnable on its own.
- Each app is its own Python package, so apps never import from one another. Anything two apps need lives in `shared/`.

```text
.
├── Makefile                          # single entry point (see §7)
├── compose.yaml                      # core stack: sim, daemon, backend, tools
├── compose.chaos.yaml                # overlay: toxiproxy + netem sidecar, rewires daemon → toxiproxy
├── compose.obs.yaml                  # overlay (stretch): prometheus + grafana
├── .env.example                      # all tunables (§6); copied to .env by `make init`
├── pyproject.toml                    # workspace root only: [tool.uv.workspace], shared ruff/mypy/pytest config
├── uv.lock / .python-version         # ONE lockfile for the whole workspace
│
├── apps/
│   ├── hardware-sim/
│   │   ├── pyproject.toml            # name = "hardware-sim"; deps: edgelab-protocol, edgelab-common
│   │   ├── Dockerfile
│   │   ├── src/hardware_sim/         # __main__.py, tcp_server.py, udp_sender.py, faults.py, admin_http.py
│   │   └── tests/
│   ├── edge-daemon/
│   │   ├── pyproject.toml            # deps: edgelab-protocol, edgelab-common, edgelab-proto
│   │   ├── Dockerfile
│   │   ├── src/edge_daemon/          # __main__.py, ingest_tcp.py, ingest_udp.py, watchdog.py,
│   │   │                             #   buffer.py (sqlite writer), uplink.py, control.py, http.py
│   │   └── tests/
│   ├── backend-collector/
│   │   ├── pyproject.toml            # deps: edgelab-common, edgelab-proto
│   │   ├── Dockerfile
│   │   ├── src/backend_collector/    # __main__.py, grpc_server.py, store.py, admin_http.py
│   │   └── tests/
│   └── tools/                        # dev/ops container: never deployed to a "device"
│       ├── pyproject.toml            # deps: all shared libs + dev group (pytest, hypothesis, ruff, mypy)
│       ├── Dockerfile                # whole workspace + docker CLI (for the chaos runner)
│       ├── src/edgelab_tools/        # verify.py (invariants), cmd.py (operator CLI), chaos/ helpers
│       └── tests/
│
├── shared/
│   ├── protocol/                     # pkg "edgelab-protocol" → import edgelab_protocol
│   │   ├── src/edgelab_protocol/     # header.py, codec.py, crc.py, msg_types.py, decoder.py, seq.py
│   │   └── tests/                    # unit + Hypothesis property tests (A1–A5)
│   ├── common/                       # pkg "edgelab-common" → import edgelab_common
│   │   ├── src/edgelab_common/       # config.py (env), backoff.py, logging.py, metrics.py,
│   │   │                             #   signals.py, probe.py (healthcheck CLI), sd_notify.py
│   │   └── tests/
│   └── proto/                        # pkg "edgelab-proto" → import edgelab_proto
│       ├── proto/edgelab/v1/edgelab.proto
│       └── src/edgelab_proto/v1/     # generated *_pb2.py / *_pb2_grpc.py / *.pyi (COMMITTED, see §7.1)
│
├── tests/                            # cross-app tests only (per-package unit tests live beside their package)
│   ├── integration/                  # in-process: sim ↔ daemon ↔ backend on localhost
│   └── chaos/                        # drives docker compose; one file per chaos test (§8)
├── docs/
│   ├── concepts/                     # one page per concept: explanation, where in code, how to observe, interview Qs
│   └── runbook.md                    # how to run each experiment and read the output
├── deploy/systemd/edge-daemon.service    # reference unit (Type=notify, WatchdogSec) for "how would you deploy it?"
├── wireshark/edgelab.lua             # stretch: dissector for the custom protocol
└── captures/                         # gitignored pcap output
```

#### Workspace wiring

```toml
# /pyproject.toml (root: not itself a package)
[tool.uv.workspace]
members = ["apps/*", "shared/*"]

# apps/edge-daemon/pyproject.toml
[project]
name = "edge-daemon"
dependencies = ["edgelab-protocol", "edgelab-common", "edgelab-proto", "aiohttp", "grpcio", "prometheus-client"]

[tool.uv.sources]
edgelab-protocol = { workspace = true }
edgelab-common   = { workspace = true }
edgelab-proto    = { workspace = true }
```

#### Dependency rules

Enforced by an import-linter contract in `make lint`:

| Package | May import |
|---|---|
| `shared/protocol` | stdlib only. Pure functions, no I/O, no asyncio |
| `shared/common` | stdlib, `prometheus-client`, `aiohttp` |
| `shared/proto` | `protobuf`, `grpcio` |
| `apps/*` | `shared/*` + third-party. **Never another app** |
| `apps/tools` | everything (it's the test/ops harness) |

Code moves into `shared/` only when a second app actually needs it. Until then it stays inside its app.

---

## 4. Components

### 4.1 `hardware-sim`: mock test rig / vehicle sensor

- **TCP server** on `:9000`. The daemon connects *to* the sim, the same way you'd connect to an instrument (SCPI on 5025, Modbus TCP on 502).
  - On accept: send `HELLO`, then `TELEMETRY` at `SAMPLE_RATE_HZ` and a `HEARTBEAT` every 1 s.
  - Accepts `COMMAND` frames and replies with `COMMAND_ACK`. Deduplicates by `command_id`.
  - Telemetry generated while no client is connected is dropped and counted (`sim_tcp_dropped_no_client_total`).
- **UDP sender** to `${DAEMON_HOST}:9001`: one frame per datagram at `SAMPLE_RATE_HZ`, regardless of whether anyone is listening.
- **Identity:** `DEVICE_ID` comes from env. `boot_id` is a random `uint32` generated at process start. Seq counters start at 0 for each stream (TCP and UDP independently).
- **Fault injection.** Set via env at start, or changed at runtime through an admin HTTP endpoint on `:9080` (`POST /faults`):

| Fault | Env | Effect |
|---|---|---|
| Fragmented writes | `FAULT_TCP_FRAGMENT_BYTES=1`, `FAULT_TCP_FRAGMENT_DELAY_MS=5` | Writes each frame N bytes at a time with `drain()` + sleep between |
| Coalesced writes | `FAULT_TCP_COALESCE=8` | Packs N frames into one `write()` (the opposite edge case) |
| UDP drop | `FAULT_UDP_DROP_PCT=15` | Randomly skips sending (the seq still increments) |
| UDP reorder | `FAULT_UDP_REORDER_PCT=5` | Holds a datagram back and sends it after the next one |
| UDP duplicate | `FAULT_UDP_DUP_PCT=1` | Sends the same datagram twice |
| Corrupt CRC | `FAULT_CRC_CORRUPT_PCT=1` | Flips one bit in the payload after computing the CRC |
| Corrupt length | `FAULT_BAD_LENGTH_PCT=0` | Writes a garbage `payload_len` (tests `MAX_FRAME_SIZE` and resync) |
| Disconnect | `FAULT_DISCONNECT_EVERY_S=0` | Closes the TCP connection abruptly (RST via `SO_LINGER=0`) |

> **Why two layers of fault injection?** App-level faults (above) are deterministic and unit-testable. Network-level faults (Toxiproxy, netem; see §8) hit the real kernel stack. Interviewers like asking how you'd tell the two apart in the field.

### 4.2 `edge-daemon`: ingestion and store-and-forward

A single asyncio process made of these tasks:

| Task | Responsibility |
|---|---|
| `tcp_ingest` | Connects to the sim with backoff. Stream-decodes frames, handles `HELLO`/`HEARTBEAT`/`TELEMETRY`/`COMMAND_ACK`, and enqueues telemetry to the writer |
| `udp_ingest` | `DatagramProtocol` on `:9001`. Validates each frame, tracks seq gaps and reordering, feeds the watchdog, enqueues telemetry |
| `watchdog` | Per-transport link state machine: `OK → LINK_DEGRADED → COMMS_LOST` |
| `buffer_writer` | **The only SQLite writer.** A dedicated thread with its own connection, fed by a bounded queue. Group-commits inserts and applies ACK/evict updates |
| `uplink` | Batches `PENDING` rows and calls `UploadBatch`. Marks rows `ACKED` on success. Backs off on failure |
| `control` | Keeps the device-initiated `Control.Connect` bidi stream to the backend open, and relays commands to the sim over TCP within the deadline |
| `http` | `/healthz` (liveness), `/readyz` (readiness), `/metrics`, `/debug/state` on `:9100` |

**Internal backpressure (a core lesson):**
- The ingest → writer queue is bounded (`INGEST_QUEUE_MAX`).
- **TCP:** when the queue is full, `tcp_ingest` awaits `queue.put()` and stops reading. The kernel receive buffer fills, the TCP window shrinks to 0, and the sim's `drain()` blocks. Backpressure reaches the producer for free.
- **UDP:** UDP has no flow control, so when the queue is full, drop the frame and increment `edge_udp_overflow_dropped_total`. Do not block the loop.

### 4.3 `backend-collector`: mock cloud / ground backend

- gRPC server on `:50051`, implementing `Telemetry.UploadBatch` and `Control.Connect`/`Control.SendCommand`.
- **Idempotent store:** SQLite with `UNIQUE(device_id, boot_id, stream, seq)`, written with `INSERT … ON CONFLICT DO NOTHING`. The whole batch goes in one transaction. The response reports `inserted` and `duplicates`.
- **Admin HTTP** on `:9101`: `/healthz`, `/metrics`, `GET /debug/keys` (for the verifier), and `POST /chaos` to change the knobs below at runtime.

| Chaos knob | Effect |
|---|---|
| `FAIL_RATE` (0–1) | Return `UNAVAILABLE` before writing (like a 503) |
| `ACK_LOSS_RATE` (0–1) | **Commit, then** return `UNAVAILABLE` (the "lost ack" case that makes idempotency necessary) |
| `MAX_INFLIGHT` | Concurrent `UploadBatch` limit. Above it, return `RESOURCE_EXHAUSTED` (backpressure) |
| `LATENCY_MS` | Sleep before responding (forces client deadlines to fire) |

- It can be killed or restarted at any point (`make kill S=backend-collector`).

---

## 5. Protocols

### 5.1 Wire frame (sim ↔ daemon, TCP and UDP)

All integers are big-endian (network byte order, `struct` prefix `!`). A frame is **header + payload + CRC32 trailer**.

**Header:** `HEADER_FMT = "!HBBIIIQI"`, `HEADER_SIZE = 28`

| Offset | Field | Type | Notes |
|---|---|---|---|
| 0 | `magic` | u16 | `0xCA01`. A mismatch means stream desync |
| 2 | `version` | u8 | `1`. Reject unknown major versions |
| 3 | `msg_type` | u8 | See below |
| 4 | `device_id` | u32 | Static per device |
| 8 | `boot_id` | u32 | Random per sim process start. Disambiguates seq resets |
| 12 | `seq` | u32 | Monotonic per (boot, stream), starting at 0. Wraps at 2³² (handle it with serial-number arithmetic, RFC 1982) |
| 16 | `timestamp_ns` | u64 | Sender `CLOCK_REALTIME` in ns (`time.time_ns()`), used for cross-host correlation. The daemon also records its own `monotonic_ns()` receive time, which drives the watchdog and timeouts |
| 24 | `payload_len` | u32 | Must be `≤ MAX_PAYLOAD_SIZE` (default 4096) |

**Trailer:** `crc32: u32`. This is `zlib.crc32(header ‖ payload) & 0xFFFFFFFF`.

**Message types**

| `msg_type` | Name | Direction | Payload |
|---|---|---|---|
| `0x01` | `HELLO` | sim → daemon | `!H` sample_rate_hz |
| `0x02` | `TELEMETRY` | sim → daemon | `!8f`: ax, ay, az, gx, gy, gz, temp_c, bus_v (32 B, so the whole frame is **64 B**) |
| `0x03` | `HEARTBEAT` | sim → daemon | empty |
| `0x04` | `COMMAND` | daemon → sim | `!IBI`: command_id, cmd_type, arg |
| `0x05` | `COMMAND_ACK` | sim → daemon | `!IB`: command_id, status (0 = ok, 1 = rejected, 2 = unknown) |

**TCP stream decoding rules**
1. Never assume one `recv()` holds one message. Use `readexactly(HEADER_SIZE)` → validate → `readexactly(payload_len + 4)`.
2. Validate in this order: `magic` → `version` → `payload_len ≤ MAX_PAYLOAD_SIZE` (the OOM guard) → CRC.
3. **CRC mismatch:** drop the frame, increment `edge_crc_errors_total{transport="tcp"}`, and continue. Framing is still aligned because the length was trusted.
4. **Bad magic or oversize length:** the stream is desynced. Increment `edge_framing_errors_total{reason}` and **resync** by scanning byte-by-byte for `magic`, up to `RESYNC_MAX_BYTES`. If that fails, close the connection and reconnect with backoff.
5. Every `readexactly` is wrapped in `asyncio.wait_for(..., TCP_READ_TIMEOUT_S)`. With heartbeats at 1 s, a 3 s timeout means the link is dead: close and reconnect. This catches **half-open connections** that never deliver a FIN or RST.

**UDP rules**
- One datagram is one frame. Require `len(datagram) == HEADER_SIZE + payload_len + 4`, otherwise count `malformed`.
- Seq tracking per `(device_id, boot_id)`:
  - `seq == last + 1`: in order.
  - `seq > last + 1`: `edge_udp_dropped_frames_total += seq - last - 1`.
  - `seq ≤ last`: out of order. Increment `edge_udp_reordered_frames_total` (or `…_duplicate_…` when already seen inside a small window). Still store it; dedupe is the backend's job.
  - A new `boot_id` resets tracking and increments `edge_boot_changes_total`.

**Socket tuning**
- `TCP_NODELAY` on the sim connection, because it carries small, latency-sensitive command/ack frames. `docs/concepts/nagle.md` explains Nagle + delayed-ACK stalls.
- `SO_KEEPALIVE` with `TCP_KEEPIDLE/INTVL/CNT` set. The docs contrast this with app-level heartbeats: keepalive detects dead peers in minutes by default, heartbeats in seconds.
- `asyncio.wait_for` around every connect and read. gRPC calls always set a `timeout=` deadline.

### 5.2 gRPC (daemon ↔ backend): `shared/proto/proto/edgelab/v1/edgelab.proto`

```proto
syntax = "proto3";
package edgelab.v1;

enum Stream { STREAM_UNSPECIFIED = 0; STREAM_TCP = 1; STREAM_UDP = 2; }

message RecordKey {
  uint32 device_id = 1;
  uint32 boot_id   = 2;
  Stream stream    = 3;
  uint32 seq       = 4;
}

message TelemetryRecord {
  RecordKey key        = 1;
  uint64 timestamp_ns  = 2;
  bytes  payload       = 3;   // raw wire payload, opaque to the backend
}

message UploadBatchRequest  { string batch_id = 1; repeated TelemetryRecord records = 2; }
message UploadBatchResponse { uint32 inserted = 1; uint32 duplicates = 2; }

service Telemetry {
  // Idempotent: replaying a batch (or any subset) is always safe.
  rpc UploadBatch(UploadBatchRequest) returns (UploadBatchResponse);
}

enum CommandType {
  COMMAND_UNSPECIFIED = 0;
  SET_SAMPLE_RATE     = 1;   // arg = Hz, 1..200
  ARM_TEST_STAND      = 2;   // rejected if already armed
  DISARM_TEST_STAND   = 3;
}

message Command       { uint32 command_id = 1; CommandType type = 2; uint32 arg = 3; uint64 deadline_unix_ms = 4; }
message CommandResult { uint32 command_id = 1; bool ok = 2; string error = 3; }
message DeviceHello   { uint32 device_id = 1; uint32 boot_id = 2; }
message DeviceMessage { oneof msg { DeviceHello hello = 1; CommandResult result = 2; } }
message SendCommandRequest { uint32 device_id = 1; Command command = 2; }

service Control {
  // Device-initiated bidi stream. Edge devices sit behind NAT/firewalls and can
  // only dial out, so the backend pushes commands down a connection the device opened.
  rpc Connect(stream DeviceMessage) returns (stream Command);
  // Operator-facing. Routed onto the device's open Connect stream; fails fast
  // with UNAVAILABLE if the device isn't connected.
  rpc SendCommand(SendCommandRequest) returns (CommandResult);
}
```

**Command path and deadline propagation:**
`make cmd` → `SendCommand` (2 s deadline) → backend pushes the `Command` with `deadline_unix_ms` → daemon writes a `COMMAND` frame to the sim with the *remaining* budget → `COMMAND_ACK` → `CommandResult` → operator. If any hop exceeds the budget, the result is `DEADLINE_EXCEEDED`. Nobody waits forever.

**gRPC channel settings:** `grpc.keepalive_time_ms`, `grpc.keepalive_timeout_ms`, and `grpc.http2.max_pings_without_data` are set explicitly. `docs/concepts/grpc.md` covers HTTP/2 multiplexing, deadlines vs timeouts, and status-code retry classes.

**Retry classes (uplink):**

| Status | Action |
|---|---|
| `OK` | Mark the rows `ACKED` |
| `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED` | Retry the same rows with backoff |
| `INVALID_ARGUMENT` | Poison batch. Bisect to find the bad row(s), mark them `QUARANTINED`, alert. Never retry forever |

---

## 6. Functional Requirements

Each requirement has an ID so tests and docs can reference it.

### A. Wire protocol and framing

- **A1** Pack and unpack the 28-byte header exactly as in §5.1. Unit tests assert `struct.calcsize(HEADER_FMT) == 28` and round-trip every msg type.
- **A2** The TCP stream decoder reassembles frames from arbitrary chunk boundaries. A Hypothesis property test feeds random splits *and* coalesced chunks of valid frame streams and expects identical output.
- **A3** `MAX_PAYLOAD_SIZE` guard: a corrupted length never allocates more than the limit. A test feeds `payload_len = 0xFFFFFFFF`.
- **A4** CRC mismatch increments `edge_crc_errors_total` and never raises out of the ingest task.
- **A5** Resync recovers from injected garbage bytes mid-stream.
- **A6** `TCP_NODELAY`, keepalive, and connect/read timeouts are set (asserted via `getsockopt` in an integration test).

### B. UDP telemetry

- **B1** Seq gap, reorder, and duplicate counting per §5.1. None of it blocks the loop: the tracker is O(1) per datagram.
- **B2** Watchdog: `LINK_DEGRADED` when no *valid* frame arrives for `WATCHDOG_DEGRADED_MS` (500), `COMMS_LOST` at `WATCHDOG_LOST_MS` (2000). The state is exported as `edge_link_state{transport}` (0/1/2) and every transition is logged.
- **B3** Null vs zero: when the link is not `OK`, `/debug/state` reports the last sensor values as `null` with `stale: true`, never as the stale numbers or `0.0`.

### C. Crash-safe local buffer (SQLite)

- **C1** Connection setup: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `auto_vacuum=INCREMENTAL` (set before table creation).
- **C2** Schema:
  ```sql
  CREATE TABLE frames (
    id           INTEGER PRIMARY KEY,
    device_id    INTEGER NOT NULL,
    boot_id      INTEGER NOT NULL,
    stream       INTEGER NOT NULL,            -- 1=tcp, 2=udp
    seq          INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    payload      BLOB    NOT NULL,
    status       INTEGER NOT NULL DEFAULT 0,  -- 0=PENDING 1=ACKED 2=QUARANTINED
    UNIQUE (device_id, boot_id, stream, seq)
  );
  CREATE INDEX frames_pending ON frames(status, id);
  ```
- **C3** Single writer: one thread owns the write connection. Async code submits operations through a queue. Group commit: flush every `WRITER_FLUSH_MS` (50) or `WRITER_FLUSH_ROWS` (200), whichever comes first.
- **C4** Lifecycle: rows go `PENDING → ACKED` only after `UploadBatch` returns `OK`. In-flight state is **not** persisted. After a crash, every unacked row is still `PENDING` and gets resent; the backend's idempotency absorbs the duplicates.
- **C5** Bounded retention: delete `ACKED` rows older than `ACKED_RETENTION_S`. If `PENDING` rows exceed `MAX_BUFFER_ROWS`, or free disk drops below `MIN_FREE_BYTES` (checked via `os.statvfs`), evict the **oldest PENDING** rows, increment `edge_buffer_evicted_total{status="pending"}`, and log at `ERROR`. Run `PRAGMA wal_checkpoint(TRUNCATE)` and `incremental_vacuum` periodically so the files actually shrink.
- **C6** Documented durability boundary (`docs/concepts/durability.md`): WAL with `synchronous=NORMAL` survives **process** crashes (`kill -9`) but can lose the last few transactions on **power loss or kernel panic**. `synchronous=FULL` closes that gap at the cost of an fsync per commit. Make it configurable via `SQLITE_SYNCHRONOUS`.

### D. Uplink and backpressure

- **D1** Batch trigger: `BATCH_MAX_ROWS` (50) or `BATCH_MAX_WAIT_MS` (250), whichever comes first. One batch in flight at a time (keeps ordering simple; documented trade-off).
- **D2** Exponential backoff with **full jitter**: `sleep = random.uniform(0, min(cap, base * 2 ** attempt))`. Reset on success. Shared helper in `edgelab_common.backoff`, used for the uplink, sim reconnect, and the control stream. Unit tests use a seeded RNG.
- **D3** Every record carries `(device_id, boot_id, stream, seq)`. The backend dedupes on that key.
- **D4** `edge_upstream_connected` is 1 only after a successful RPC or an established `Connect` stream, and 0 on any transport failure.

### E. Control plane, observability, shutdown

- **E1** `Control.Connect` is opened by the daemon and reconnected with backoff. Commands are relayed per §5.2 with deadline propagation.
- **E2** `/healthz` returns 200 if the event loop is responsive. `/readyz` returns 200 only once the SQLite buffer is open (the daemon is ready to accept data even when the backend is down, which is the point of store-and-forward).
- **E3** Metrics (minimum set, Prometheus names):

  | Metric | Type |
  |---|---|
  | `edge_rx_frames_total{transport,msg_type}` | counter |
  | `edge_crc_errors_total{transport}` | counter |
  | `edge_framing_errors_total{reason}` | counter |
  | `edge_udp_dropped_frames_total` / `_reordered_` / `_duplicate_` / `edge_udp_overflow_dropped_total` | counter |
  | `edge_link_state{transport}` | gauge |
  | `edge_buffer_pending_depth` | gauge |
  | `edge_buffer_evicted_total{status}` | counter |
  | `edge_upstream_connected` | gauge |
  | `edge_upload_batches_total{result}` | counter |
  | `edge_upload_duration_seconds` | histogram |
  | `edge_sim_reconnects_total`, `edge_boot_changes_total` | counter |

  The backend exports `backend_rows_inserted_total`, `backend_rows_duplicate_total`, `backend_rpc_total{method,code}`, and `backend_connected_devices`.
- **E4** Graceful shutdown on `SIGTERM`/`SIGINT` via `loop.add_signal_handler`, in this order: stop accepting reads → drain the ingest queue into SQLite → final commit and checkpoint → attempt one last upload (bounded by `SHUTDOWN_UPLOAD_BUDGET_S`) → close sockets and gRPC channel → exit 0. Must finish inside the Compose `stop_grace_period` (10 s).
- **E5** Optional `sd_notify` (`READY=1`, `WATCHDOG=1`) when `NOTIFY_SOCKET` is set. It's a no-op in Docker but demonstrated by `deploy/systemd/edge-daemon.service`.

### Configuration (`.env.example`)

| Var | Default | Used by |
|---|---|---|
| `DEVICE_ID` | `1001` | sim |
| `SAMPLE_RATE_HZ` | `50` | sim |
| `SIM_HOST` / `SIM_TCP_PORT` | `hardware-sim` / `9000` | daemon (rewired to `toxiproxy` in the chaos overlay) |
| `UDP_LISTEN_PORT` | `9001` | daemon |
| `BACKEND_TARGET` | `backend-collector:50051` | daemon |
| `MAX_PAYLOAD_SIZE` | `4096` | both |
| `TCP_READ_TIMEOUT_S` | `3` | daemon |
| `WATCHDOG_DEGRADED_MS` / `WATCHDOG_LOST_MS` | `500` / `2000` | daemon |
| `BATCH_MAX_ROWS` / `BATCH_MAX_WAIT_MS` | `50` / `250` | daemon |
| `BACKOFF_BASE_MS` / `BACKOFF_CAP_MS` | `100` / `10000` | daemon |
| `MAX_BUFFER_ROWS` / `MIN_FREE_BYTES` | `100000` / `50MB` | daemon |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | daemon |
| `INGEST_QUEUE_MAX` | `1000` | daemon |
| `FAULT_*` | `0` | sim (§4.1) |
| `FAIL_RATE`, `ACK_LOSS_RATE`, `MAX_INFLIGHT`, `LATENCY_MS` | `0`, `0`, `4`, `0` | backend |

---

## 7. Docker & Make

### 7.1 Images

One image per app, built from `apps/<app>/Dockerfile`. The **build context is always the repo root**, because each app needs `shared/`, the root `pyproject.toml` and `uv.lock`. All three runtime Dockerfiles follow the same two-stage pattern:

```dockerfile
# apps/edge-daemon/Dockerfile  (context: repo root)
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:<pinned> /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# 1) deps layer: only lock + manifests, so code edits don't bust the cache
COPY pyproject.toml uv.lock ./
COPY apps/edge-daemon/pyproject.toml apps/edge-daemon/
COPY shared/protocol/pyproject.toml shared/protocol/
COPY shared/common/pyproject.toml   shared/common/
COPY shared/proto/pyproject.toml    shared/proto/
RUN uv sync --frozen --no-dev --package edge-daemon --no-install-workspace
# 2) source layer
COPY shared/ shared/
COPY apps/edge-daemon/ apps/edge-daemon/
RUN uv sync --frozen --no-dev --package edge-daemon --no-editable

FROM python:3.13-slim AS runtime
RUN useradd --system --uid 10001 app && mkdir -p /var/lib/edgelab && chown app /var/lib/edgelab
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER app
CMD ["python", "-m", "edge_daemon"]
```

- `uv sync --package <app>` installs only that app and the shared libs it declares. The backend image never contains the wire-protocol decoder, and the sim image never contains gRPC.
- `--no-editable` copies the packages into the venv, so the runtime stage needs only `.venv`.
- `apps/tools/Dockerfile` instead runs `uv sync --all-packages` (with dev deps) and installs the Docker CLI and Compose plugin. It is used for lint, typecheck, tests, codegen and the chaos runner.
- A `.dockerignore` at the root excludes `.venv`, `captures/`, `.git`, `**/__pycache__`.

**Protobuf stubs are committed.** `make proto` runs `grpc_tools.protoc` (with `mypy-protobuf` for `.pyi`) in the `tools` container, writing to `shared/proto/src/edgelab_proto/v1/`. `make check` regenerates them and fails if `git diff` shows drift. This keeps the runtime images free of `grpcio-tools` and gives the IDE working imports with no extra step.

### 7.2 `compose.yaml` (sketch)

```yaml
name: edgelab

x-app: &app
  init: true                 # tini as PID 1: reaps zombies, forwards signals
  stop_grace_period: 10s
  env_file: [.env]

services:
  hardware-sim:
    <<: *app
    build: { context: ., dockerfile: apps/hardware-sim/Dockerfile }
    image: edgelab/hardware-sim:dev
    networks: [field]
    cap_add: [NET_ADMIN]     # lets netem sidecar shape this container's egress
    ports: ["9080:9080"]     # fault admin
    develop:
      watch:
        - { action: sync+restart, path: apps/hardware-sim/src, target: /app/apps/hardware-sim/src }
        - { action: rebuild, path: shared/ }
        - { action: rebuild, path: uv.lock }

  edge-daemon:
    <<: *app
    build: { context: ., dockerfile: apps/edge-daemon/Dockerfile }
    image: edgelab/edge-daemon:dev
    networks: [field, uplink]      # the only dual-homed service, like a real gateway
    volumes: [edge-buffer:/var/lib/edgelab]
    ports: ["9100:9100"]
    healthcheck:
      test: ["CMD", "python", "-m", "edgelab_common.probe", "http://localhost:9100/readyz"]
      interval: 5s
    # Deliberately NO depends_on backend: the daemon must tolerate it being absent.
    # develop.watch: same pattern as hardware-sim

  backend-collector:
    <<: *app
    build: { context: ., dockerfile: apps/backend-collector/Dockerfile }
    image: edgelab/backend-collector:dev
    networks: [uplink]
    volumes: [backend-data:/var/lib/edgelab]
    ports: ["50051:50051", "9101:9101"]

  tools:
    build: { context: ., dockerfile: apps/tools/Dockerfile }
    image: edgelab/tools:dev
    profiles: [tools]
    working_dir: /app
    networks: [field, uplink]
    volumes:
      - .:/app
      - edge-buffer:/data/edge
      - backend-data:/data/backend
      - /var/run/docker.sock:/var/run/docker.sock   # chaos runner drives compose (root-equivalent: dev only)

networks:
  field:  { internal: true }   # device segment: no route out, can't reach the backend
  uplink: {}

volumes:
  edge-buffer: {}
  backend-data: {}
```

Design notes:
- **Named volumes, not bind mounts, for SQLite.** On macOS, bind mounts go through virtiofs, where POSIX locking and WAL shared memory are unreliable.
- **Network segmentation** (`field` vs `uplink`) mirrors a real edge topology. `hardware-sim` can't reach `backend-collector`. Verify with `make shell` → `ping`.
- `compose.chaos.yaml` adds `toxiproxy` (on `field`, with `SIM_HOST=toxiproxy` for the daemon) and a `netem` sidecar (`nicolaka/netshoot`, `network_mode: service:hardware-sim`, `cap_add: [NET_ADMIN]`).
- `make dev` uses `docker compose watch`. Edits to an app's `src/` sync into its container and restart it. Edits under `shared/` or to `uv.lock` rebuild every app image that depends on them.
- **Watch caveat:** `sync` only works if the app is installed in editable mode. The runtime image uses `--no-editable`, so either (a) use `action: rebuild` everywhere (simple, about 5 s per change thanks to layer caching), or (b) add a `dev` target to each app Dockerfile that installs editable. Start with (a) and only move to (b) if rebuilds feel slow.
- The `tools` container bind-mounts the repo at `/app` and keeps its own `.venv` in an anonymous volume, so the host's `.venv` (if any) never collides with the Linux one.

### 7.3 Makefile targets

Every target is a thin wrapper over `docker compose`. `make` with no target prints help, generated from `## comments`.

| Target | Purpose |
|---|---|
| `help` | List targets (default) |
| `init` | Copy `.env.example` → `.env` if missing; create `captures/` |
| `build [APP=edge-daemon]` | Build all app images, or just one |
| `up` / `down` / `ps` / `logs [S=svc]` | Lifecycle |
| `dev` | `docker compose watch` hot-reload loop |
| `restart S=svc` / `kill S=svc [SIG=KILL]` / `stop S=svc` / `start S=svc` | Targeted process control |
| `shell [S=tools]` | Interactive shell in a container |
| `proto` | Regenerate committed stubs in `shared/proto/src/edgelab_proto/v1/` |
| `fmt` / `lint` / `typecheck` | Run in `tools` across the whole workspace (`lint` includes the import-linter contracts) |
| `test [PKG=apps/edge-daemon]` | Unit tests for every package (`apps/*/tests`, `shared/*/tests`), or just one |
| `test-integration` | Cross-app in-process tests (`tests/integration`) |
| `check` | `lint typecheck test` + proto drift check, the pre-commit gate |
| `lock` | `uv lock` in `tools` (only after editing any `pyproject.toml`) |
| `health` / `metrics [S=edge-daemon]` | curl the endpoints, filtering metrics to `edge_`/`backend_` lines |
| `cmd TYPE=SET_SAMPLE_RATE ARG=20` | Operator command via `Control.SendCommand` |
| `faults ARGS='udp_drop_pct=15'` | Change sim faults at runtime |
| `backend-chaos ARGS='ack_loss_rate=0.3'` | Change backend chaos knobs at runtime |
| `netem ARGS='loss 15% delay 20ms 5ms reorder 5%'` / `netem-clear` | Kernel-level impairment on sim egress |
| `toxic ARGS='slicer …'` / `toxic-clear` | Toxiproxy toxics on the sim TCP link |
| `partition S=edge-daemon NET=uplink` / `heal …` | `docker network disconnect/connect`, a silent link loss with no FIN/RST |
| `capture S=edge-daemon SECS=30` | tcpdump in the service's netns → `captures/<svc>-<ts>.pcap` |
| `verify` | Run `edgelab_tools.verify` and print the invariant report (§8.1) |
| `chaos-<name>` | One chaos scenario (§8) |
| `chaos` | All chaos scenarios, in sequence, with a summary table |
| `obs-up` | (stretch) Start the stack with the Prometheus + Grafana overlay |
| `clean` | `down -v`, remove captures |

Skeleton:

```make
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE       := docker compose -f compose.yaml
CHAOS_COMPOSE := $(COMPOSE) -f compose.chaos.yaml
TOOLS         := $(COMPOSE) run --rm tools
APPS          := hardware-sim edge-daemon backend-collector
S   ?= edge-daemon
PKG ?= apps shared

help: ## Show targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build app images (APP=<name> for one)
	$(COMPOSE) build $(APP)

up: init ## Start core stack
	$(COMPOSE) up -d --build

test: ## Unit tests (PKG=apps/edge-daemon for one package)
	$(TOOLS) uv run pytest $(PKG) -q

proto: ## Regenerate committed gRPC stubs
	$(TOOLS) uv run python -m grpc_tools.protoc -I shared/proto/proto \
	  --python_out=shared/proto/src --grpc_python_out=shared/proto/src --mypy_out=shared/proto/src \
	  shared/proto/proto/edgelab/v1/edgelab.proto

kill: ## Send a signal to a service (SIG=KILL|TERM)
	$(COMPOSE) kill -s $(or $(SIG),KILL) $(S)

chaos-%: ## Run one chaos scenario
	$(CHAOS_COMPOSE) up -d --build
	$(TOOLS) uv run pytest tests/chaos/test_$*.py -v -s
```

---

## 8. Chaos Test Suite

Each scenario is a pytest file in `tests/chaos/` that runs in the `tools` container. It drives Compose through the Docker socket, scrapes `/metrics`, and finishes by calling the verifier. Every scenario starts from a clean state (`down -v`).

### 8.1 Verifier invariants (`make verify`)

The verifier reads the daemon buffer and the backend store from their volumes, after quiescence (`edge_buffer_pending_depth == 0`, or a timeout). For each `(device_id, boot_id, stream)` it checks:
- **I1 (no duplicates):** the backend has exactly one row per key. The UNIQUE constraint guarantees this, so the real check is that `backend_rows_duplicate_total` behaves as expected.
- **I2 (no loss after commit):** every key the daemon committed to SQLite (excluding `QUARANTINED` and evicted rows) exists in the backend.
- **I3 (TCP continuity):** for TCP streams, backend seqs are contiguous from `0..max`, except inside windows where the daemon was down. Those windows are reported, not hidden.
- **I4 (accounting):** for UDP, `sim_sent − backend_rows == edge_udp_dropped_frames_total + edge_udp_overflow_dropped_total` (± frames in flight at the end).

### 8.2 Scenarios

| # | Target | Setup → Action | Pass criteria | Concept |
|---|---|---|---|---|
| **X1** | `chaos-fragmentation` | `FAULT_TCP_FRAGMENT_BYTES=1`, `DELAY_MS=5`, 64-byte frames. Repeat with the Toxiproxy `slicer` toxic and with `FAULT_TCP_COALESCE=8` | 100% of frames decoded intact. `edge_framing_errors_total == 0`. I3 holds | TCP is a byte stream; framing |
| **X2** | `chaos-outage` | 50 Hz. `stop backend-collector` for 15 s while the sim streams, then `start` | `edge_buffer_pending_depth` rises, then drains to 0. I1–I3 hold: **0 lost, 0 duplicated**. Backend reconnect timestamps show jitter, not lockstep | Store-and-forward, backoff + jitter |
| **X3** | `chaos-sigkill` | `kill -s KILL edge-daemon` mid-stream, wait 5 s, `start` | Restart recovers PENDING rows from the WAL. Depth drains to 0. I1, I2 hold. I3 reports exactly one gap window, matching the downtime | WAL durability, at-least-once |
| **X4** | `chaos-udp-loss` | Run twice: `FAULT_UDP_DROP_PCT=15` (app level), then `netem loss 15% reorder 5%` (kernel level). 60 s each | Loop never stalls (`/healthz` p99 < 50 ms). Measured drop rate is 15 % ± 2 %. Metrics keep updating. I4 holds | UDP semantics, seq tracking |
| **X5** | `chaos-reconnect` | Restart `hardware-sim` twice, 10 s apart | `edge_sim_reconnects_total ≥ 2`, `edge_boot_changes_total == 2`, three `boot_id`s in the backend, each with contiguous TCP seqs from 0. The watchdog passes through `COMMS_LOST → OK` | Reconnect logic, session identity |
| **X6** | `chaos-halfopen` | `partition S=hardware-sim NET=field` (no FIN/RST is sent), wait 10 s, `heal` | Daemon detects the dead link within `TCP_READ_TIMEOUT_S` + 1 s, not after the kernel keepalive default (about 2 h). It reconnects after heal. The pcap shows no FIN | Half-open connections, heartbeats vs keepalive |
| **X7** | `chaos-lost-ack` | `ACK_LOSS_RATE=0.3` for 60 s | `backend_rows_duplicate_total > 0` (replays happened), yet I1 and I2 hold | At-least-once + idempotency = effectively-once |
| **X8** | `chaos-disk-cap` | `MAX_BUFFER_ROWS=2000`, backend stopped for 120 s | Pending depth plateaus at the cap. `edge_buffer_evicted_total{status="pending"}` > 0. An ERROR log appears. The daemon stays healthy. After the backend restarts, I2 holds for non-evicted rows | Bounded resources, ENOSPC avoidance |
| **X9** | `chaos-sigterm` | `kill -s TERM edge-daemon` mid-stream | Exits **0** within the grace period. Logs show the shutdown steps in order. No committed row is lost. Restart resumes cleanly | Signals, graceful shutdown, PID 1 |
| **X10** | `chaos-backpressure` | Backend `MAX_INFLIGHT=1`, `LATENCY_MS=500`, sim at 200 Hz | Daemon gets `RESOURCE_EXHAUSTED`, backs off, buffer grows within bounds. TCP ingest shows a zero window in the pcap if the writer queue fills | Backpressure end to end |
| **X11** | `chaos-crc` | `FAULT_CRC_CORRUPT_PCT=2`, `FAULT_BAD_LENGTH_PCT=0.5` | `edge_crc_errors_total` ≈ the expected rate. Resync events are counted. No crash, no OOM | Integrity, defensive parsing |

X1–X5 are the original five scenarios. X6–X11 extend them to cover the follow-up questions interviewers usually ask next.

---

## 9. Concept Map (Interview Prep)

Each row gets a page in `docs/concepts/` with: a one-paragraph explanation, where it lives in the code, how to observe it (`make` target / metric / pcap filter), and 3–5 likely interview questions with crisp answers.

| Concept | Where | Observe with | Typical question |
|---|---|---|---|
| TCP is a byte stream, not messages | `shared/protocol/…/decoder.py` | X1, pcap | "Why can't you assume one `recv` = one message?" |
| Length-prefix vs delimiter framing; OOM guard | `shared/protocol/` | X11 | "How do you design a binary protocol for a sensor?" |
| Endianness, `struct`, alignment | `shared/protocol/…/header.py` | unit tests | "What does `!` mean in `struct`? Why network order?" |
| CRC vs cryptographic hash | `shared/protocol/…/crc.py` | X11 | "Is CRC32 enough for integrity? For security?" |
| Nagle, delayed ACK, `TCP_NODELAY` | `edge-daemon/…/ingest_tcp.py` | pcap timing | "Why are my small command packets delayed 40 ms?" |
| Timeouts everywhere; deadline propagation | all clients, control path | X6, `make cmd` | "What happens if the far end just stops responding?" |
| Half-open connections; keepalive vs heartbeats | `edge-daemon/…/ingest_tcp.py` | X6 | "How do you detect a pulled Ethernet cable?" |
| UDP semantics: loss, reorder, dup, no flow control | `edge-daemon/…/ingest_udp.py` | X4 | "When would you choose UDP over TCP for telemetry?" |
| Sequence numbers, wraparound, boot/session IDs | `shared/protocol/…/seq.py` | X5 | "Device rebooted and seq reset: how does the backend know?" |
| Watchdogs and link state; null vs zero | `edge-daemon/…/watchdog.py` | X4, X6 | "How do you tell 'sensor reads 0' from 'sensor gone'?" |
| Flow control and backpressure (TCP window, queues, `RESOURCE_EXHAUSTED`) | `edge-daemon/…/buffer.py`, `edge-daemon/…/uplink.py` | X10 | "Producer is faster than consumer. What happens?" |
| Store-and-forward; WAL; fsync durability | `edge-daemon/…/buffer.py` | X2, X3 | "Device loses connectivity for 6 h. What's your design?" |
| At-least-once + idempotency | `edge-daemon/…/uplink.py`, `backend-collector/…/store.py` | X7 | "How do you get exactly-once delivery?" (trick question) |
| Exponential backoff with full jitter; thundering herd | `shared/common/…/backoff.py` | X2 | "10k devices reconnect at once after an outage…" |
| Outbound-only connectivity; NAT; device-initiated streams | `edge-daemon/…/control.py` | `make cmd` | "How does the cloud send a command to a device behind NAT?" |
| gRPC/HTTP2: deadlines, status codes, keepalive | `edge-daemon/…/uplink.py`, `edge-daemon/…/control.py` | X7, X10 | "gRPC vs REST vs MQTT for edge?" |
| Signals, PID 1, graceful shutdown, systemd readiness | `shared/common/…/signals.py`, `deploy/systemd` | X9 | "How do you deploy and supervise this on a Linux device?" |
| Liveness vs readiness | `edge-daemon/…/http.py` | `make health` | "Why separate `/healthz` and `/readyz`?" |
| Network segmentation | `compose.yaml` | `make shell` + ping | "How would you isolate field devices from the internet?" |
| Bounded resources; disk-full behaviour | `edge-daemon/…/buffer.py` | X8 | "What happens when the disk fills up?" |
| Packet capture and reading a pcap | `make capture` | Wireshark | "Walk me through debugging a flaky link." |

---

## 10. Milestones

Each milestone ends with `make check` green and a short entry in `docs/runbook.md`.

| # | Milestone | Done when |
|---|---|---|
| **M0** | Scaffolding: Makefile, uv workspace (`apps/*`, `shared/*`), per-app Dockerfiles, `compose.yaml`, ruff/mypy/pytest/import-linter wired into `tools` | `make up` starts three hello-world apps, each importing a trivial function from `shared/common`. `make check` passes. **Spike:** confirm `tc netem` works in Docker Desktop's VM kernel (fallback: Toxiproxy for TCP, app-level faults for UDP) |
| **M1** | Wire protocol: header, codec, CRC, stream decoder, seq tracker, backoff helper | A1–A5 unit and property tests pass |
| **M2** | `hardware-sim` + daemon TCP/UDP ingest + watchdog + `/metrics` | `make up` → `make metrics` shows rx counters climbing. B1–B3 hold |
| **M3** | SQLite buffer: writer thread, group commit, retention, eviction | C1–C6 unit tests pass. A manual `kill -9` keeps the rows |
| **M4** | gRPC uplink + idempotent backend + backoff + chaos knobs | `make verify` is green at steady state. D1–D4 hold |
| **M5** | Control plane + graceful shutdown + readiness | `make cmd TYPE=SET_SAMPLE_RATE ARG=10` changes the rate end to end. E1–E5 hold |
| **M6** | Chaos overlay (Toxiproxy, netem), verifier, `make capture`, X1–X11 | `make chaos` prints all-green |
| **M7** | Concept docs (§9), each with questions and answers | One page per concept-map row |

### Stretch modules (pick any, after M6)

- **S1 Application-level ack and resume.** The sim keeps a ring buffer. The daemon's reply to `HELLO` carries `last_committed_seq`, and the sim replays from there. This closes the X3 gap window and makes the point that **a TCP ACK ≠ an application ACK**.
- **S2 MQTT uplink** (Mosquitto container): an alternative uplink using QoS 1, persistent sessions, Last Will and Testament, and retained messages. Compare with gRPC semantics in the docs.
- **S3 mTLS:** `make certs` generates a CA plus device certs in a container. gRPC with mTLS; device identity comes from the cert CN; covers cert rotation.
- **S4 Observability overlay:** Prometheus + a provisioned Grafana dashboard (`make obs-up`).
- **S5 Wireshark dissector:** `wireshark/edgelab.lua` decodes captured frames.
- **S6 MTU and IP fragmentation:** send UDP payloads larger than 1472 B, observe fragmentation in a pcap, then set DF and observe drops. Discuss PMTUD.
- **S7 Fleet / thundering herd:** a `fleet-sim` mode runs N virtual devices in one process. Plot backend reconnect timestamps with and without jitter.
- **S8 Time:** inject sim clock skew and show why the watchdog uses monotonic time while correlation uses realtime. Discuss NTP vs PTP.
- **S9 CAN / serial (talk-track only):** document how SocketCAN/`vcan` or a pty-based serial sim would replace the TCP link on a Linux host.

---

## 11. Design Decisions

| Decision | Rationale |
|---|---|
| Daemon dials the sim (sim is the server) | Mirrors real instruments. Puts reconnect and backoff logic on the gateway, where it belongs |
| `device_id` + `boot_id` in every frame header | UDP is connectionless, so session identity can't live in a TCP handshake. `boot_id` disambiguates seq resets after a reboot |
| `stream` in the idempotency key | TCP and UDP have independent seq spaces |
| gRPC over REST for the uplink | Typed contract, deadlines, bidi streaming for device-initiated control. REST/MQTT trade-offs are covered in the docs |
| In-flight state not persisted | Simpler, and crash-correct: replay + idempotent receiver beats trying to track in-flight rows |
| One batch in flight | Ordering and reasoning stay simple. Pipelining is a documented possible extension |
| SQLite on the backend too | Keeps the focus on networking. Postgres is a trivial swap if wanted |
| No `depends_on` from daemon to backend | Startup ordering is a runtime concern the daemon must handle, not something Compose should hide |
| `apps/` + `shared/` uv workspace, one image per app | Each app has its own dependencies and image, and apps can't import each other, so the boundaries mirror real deployables (the sim ≈ firmware, the daemon ≈ gateway, the backend ≈ cloud). One `uv.lock` keeps shared versions consistent |
| Build context = repo root for every app | Apps need `shared/` and the lockfile. Per-app manifest-only `COPY` layers keep caching effective |
| Generated proto stubs committed | Runtime images don't need `grpcio-tools`, IDE imports work immediately, and the drift check in `make check` keeps stubs honest |

## 12. Risks & Open Questions

- **netem on Docker Desktop (macOS):** expected to work in the LinuxKit VM, but verify in M0. Fallback is listed there.
- **Docker socket in `tools`:** root-equivalent access to the Docker daemon. Acceptable for a local learning repo; call it out in the README.
- **Timing flakiness in chaos tests:** use polling with timeouts (`wait_until(metric, predicate, timeout)`), never fixed sleeps, for assertions.
- **`ACK_LOSS_RATE` vs deadlines:** make sure the daemon's `UploadBatch` deadline is longer than the backend's `LATENCY_MS` in tests that aren't about deadlines.
