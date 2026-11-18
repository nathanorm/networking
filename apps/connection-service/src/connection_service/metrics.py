from prometheus_client import Counter, Gauge, disable_created_metrics

disable_created_metrics()  # drop the *_created timestamp series: noise for our purposes

RX_FRAMES = Counter("edge_rx_frames", "Valid frames received", ["transport", "msg_type"])
CRC_ERRORS = Counter("edge_crc_errors", "Frames dropped for bad CRC", ["transport"])
FRAMING_ERRORS = Counter("edge_framing_errors", "Malformed frames / stream desyncs", ["transport"])
UDP_DROPPED = Counter("edge_udp_dropped_frames", "UDP frames missing from seq gaps")
UDP_LATE = Counter("edge_udp_late_frames", "UDP frames arriving out of order or duplicated")
UDP_OVERFLOW = Counter("edge_udp_overflow", "UDP frames dropped because the buffer queue was full")
LINK_STATE = Gauge("edge_link_state", "0=OK 1=DEGRADED 2=LOST", ["transport"])
BUFFER_PENDING = Gauge("edge_buffer_pending_depth", "Rows waiting for backend ack")
BUFFER_EVICTED = Counter("edge_buffer_evicted", "PENDING rows evicted by the buffer cap")
UPSTREAM_CONNECTED = Gauge("edge_upstream_connected", "1 if the last upload succeeded")
UPLOAD_BATCHES = Counter("edge_upload_batches", "Upload attempts", ["result"])
EMULATOR_CONNECTS = Counter("edge_emulator_connects", "Successful TCP connects to the emulator")
