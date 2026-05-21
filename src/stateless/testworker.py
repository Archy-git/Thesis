import os
import sys
import socket
import time
import struct
import threading
import subprocess
import json
import csv
import hmac
import hashlib
import re
from collections import deque

from schedule import TEST_DURATION, get_jammed_channels, schedule_for_mode

# ===========================================================================
# ARTCHO TEST WORKER
# ===========================================================================
# Runs on both nodes. Sends 20 Hz UDP test packets via SO_BINDTODEVICE on
# the protocol-designated active iface. Logs every TX and every RX to two
# CSV files. Also runs the scripted local jammer that applies tc per the
# schedule. Writes a JSON status file every 0.5s for the dashboard.
# ===========================================================================

# Constants (must match artcho.py)
FREQUENCIES = [
    "2422", "2427", "2432", "2437", "2442",
    "2447", "2452", "2457", "2462",
]
FREQ_TO_CH = {
    "2422": "3", "2427": "4", "2432": "5", "2437": "6", "2442": "7",
    "2447": "8", "2452": "9", "2457": "10", "2462": "11",
}
SHARED_SECRET   = b"University_Thesis_Secret_Key_2026"
BUCKET_SECONDS  = 2.0
INTERFACES      = ["wlan0", "wlan1"]
BROADCAST_ADDR  = "192.168.200.255"
TEST_PORT       = 5002
BLACKLIST_FILE  = "/tmp/blacklist.json"

PACKET_RATE_HZ   = 20
PACKET_INTERVAL  = 1.0 / PACKET_RATE_HZ
PACKET_SIZE      = 256

# Env config
TEST_EPOCH      = float(os.environ.get("TEST_EPOCH", "0"))
BUCKET_EPOCH    = float(os.environ.get("BUCKET_EPOCH", "0"))
if BUCKET_EPOCH == 0 and TEST_EPOCH > 0:
    BUCKET_EPOCH = TEST_EPOCH
NODE_ID         = os.environ.get("NODE_ID", "?")
MODE            = os.environ.get("MODE", "L3").upper()
RUN_ID          = os.environ.get("RUN_ID", time.strftime("%Y%m%d_%H%M%S"))
STATIC_CHANNEL  = os.environ.get("STATIC_CHANNEL", "6")
TRANSPORT       = os.environ.get("TRANSPORT", "unicast").lower()  # "unicast" or "broadcast"

# Mode flags — must match artcho.py
#   L1 static, L2 dual+no-BL, L3 dual+BL+detect, L4 single+no-BL, L5 dual+no-BL
USE_BL          = MODE in ("L3",)
DUAL_RADIO      = MODE in ("L2", "L3", "L5")
STATIC          = (MODE == "L1")

# Destination address for test packets.
#   unicast (default): peer IP -> 802.11 unicast frame, ACKed, rate-adapted, retried up to 7x
#   broadcast:         255 IP   -> 802.11 broadcast frame at 1 Mbps, no ACK, no retry
if NODE_ID == "A":
    PEER_IP = "192.168.200.2"
elif NODE_ID == "B":
    PEER_IP = "192.168.200.1"
else:
    PEER_IP = None
DEST_ADDR = PEER_IP if TRANSPORT == "unicast" else BROADCAST_ADDR

# OUTPUT_DIR is where CSVs go. testdash sets this to the per-window folder
# (final_experiment/<MODE>/window_<NAME>/) on Node A and /tmp on Node B.
# STATUS_FILE always stays in /tmp because it's transient state the dashboard
# polls; it's not part of the persistent run output.
OUTPUT_DIR  = os.environ.get("OUTPUT_DIR", "/tmp")
TX_CSV      = f"{OUTPUT_DIR}/test_{MODE}_{RUN_ID}_{NODE_ID}_tx.csv"
RX_CSV      = f"{OUTPUT_DIR}/test_{MODE}_{RUN_ID}_{NODE_ID}_rx.csv"
STATUS_FILE = f"/tmp/testworker_status_{NODE_ID}.json"


# ===========================================================================
# Protocol state recomputation (independent of artcho.py)
# ===========================================================================

def read_blacklist():
    try:
        with open(BLACKLIST_FILE, "r") as f:
            data = json.load(f)
        now = time.time()
        return {ch: exp for ch, exp in data.get("channels", {}).items() if exp > now}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def compute_totp(bucket, blacklist):
    h_bytes = bucket.to_bytes(8, byteorder="big", signed=True)
    h_int = int(hmac.new(SHARED_SECRET, h_bytes, hashlib.sha256).hexdigest(), 16)
    base_index = h_int % len(FREQUENCIES)
    for offset in range(len(FREQUENCIES)):
        idx = (base_index + offset) % len(FREQUENCIES)
        freq = FREQUENCIES[idx]
        ch = FREQ_TO_CH[freq]
        if ch not in blacklist:
            return freq, ch
    return FREQUENCIES[base_index], FREQ_TO_CH[FREQUENCIES[base_index]]


def get_active_iface(bucket):
    if not DUAL_RADIO:
        return "wlan0"
    return "wlan0" if bucket % 2 == 0 else "wlan1"


def compute_bucket():
    """Must match artcho's compute_bucket — uses BUCKET_EPOCH, not TEST_EPOCH."""
    if BUCKET_EPOCH > 0:
        return int((time.time() - BUCKET_EPOCH) / BUCKET_SECONDS)
    return int(time.time() / BUCKET_SECONDS)


def get_protocol_channel(bucket):
    if STATIC:
        return STATIC_CHANNEL
    bl = read_blacklist() if USE_BL else {}
    _, ch = compute_totp(bucket, bl)
    return ch


# ===========================================================================
# Iface channel inspection
# ===========================================================================

def get_iface_channel(iface):
    try:
        out = subprocess.check_output(
            f"iw dev {iface} info",
            shell=True, text=True, stderr=subprocess.DEVNULL, timeout=0.5,
        )
        m = re.search(r"channel\s+(\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


# ===========================================================================
# Scripted jammer (per-node local tc)
# ===========================================================================

def apply_jamming(iface):
    """Apply 100% egress loss netem + ingress drop filter."""
    subprocess.run(
        f"sudo tc qdisc replace dev {iface} root netem loss 100% 2>/dev/null",
        shell=True, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        f"sudo tc qdisc add dev {iface} handle ffff: ingress 2>/dev/null || true",
        shell=True, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        f"sudo tc filter replace dev {iface} parent ffff: protocol all matchall action drop 2>/dev/null",
        shell=True, stderr=subprocess.DEVNULL,
    )


def release_jamming(iface):
    subprocess.run(
        f"sudo tc qdisc del dev {iface} root 2>/dev/null",
        shell=True, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        f"sudo tc qdisc del dev {iface} ingress 2>/dev/null",
        shell=True, stderr=subprocess.DEVNULL,
    )


def jammer_loop():
    """Poll schedule + iface channels, apply/release tc to local ifaces."""
    iface_jammed_on = {iface: None for iface in INTERFACES}
    while True:
        if TEST_EPOCH <= 0:
            time.sleep(0.1)
            continue
        test_t = time.time() - TEST_EPOCH
        if test_t < 0:
            time.sleep(min(0.1, -test_t))
            continue
        if test_t > TEST_DURATION + 5:
            # Done — make sure everything is released
            for iface in INTERFACES:
                if iface_jammed_on[iface] is not None:
                    release_jamming(iface)
                    iface_jammed_on[iface] = None
            return

        jammed_chs = get_jammed_channels(test_t, MODE)
        for iface in INTERFACES:
            ch = get_iface_channel(iface)
            should_jam_on = ch if (ch is not None and ch in jammed_chs) else None
            if should_jam_on != iface_jammed_on[iface]:
                if should_jam_on:
                    apply_jamming(iface)
                else:
                    release_jamming(iface)
                iface_jammed_on[iface] = should_jam_on
        time.sleep(0.1)


# ===========================================================================
# Sockets
# ===========================================================================

tx_sock_per_iface = {}
rx_sock = None


def setup_sockets():
    global rx_sock
    for iface in INTERFACES:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        except PermissionError:
            print(f"[FATAL] SO_BINDTODEVICE requires CAP_NET_RAW. Run with sudo.")
            sys.exit(1)
        tx_sock_per_iface[iface] = s

    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx_sock.bind(("0.0.0.0", TEST_PORT))


# ===========================================================================
# Packet format
# ===========================================================================
# [8 bytes src_id (str, null-padded)] [4 bytes seq, network order] [8 bytes send_ts_ns]
# [remaining bytes: zero padding to PACKET_SIZE]
HEADER_LEN = 20


def pack_packet(seq, send_ts_ns):
    src_id = NODE_ID.encode().ljust(8, b"\x00")[:8]
    header = src_id + struct.pack("!IQ", seq & 0xFFFFFFFF, send_ts_ns)
    padding = b"\x00" * (PACKET_SIZE - len(header))
    return header + padding


def unpack_packet(data):
    if len(data) < HEADER_LEN:
        return None
    src_id = data[:8].rstrip(b"\x00").decode(errors="ignore")
    seq, send_ts_ns = struct.unpack("!IQ", data[8:HEADER_LEN])
    return src_id, seq, send_ts_ns


# ===========================================================================
# Shared state for dashboard
# ===========================================================================

state_lock = threading.Lock()
tx_count = 0
rx_count = 0
recent_latencies = deque(maxlen=400)
last_rx_ts_ns = 0


# ===========================================================================
# Sender
# ===========================================================================

def sender_loop():
    global tx_count

    # Wait for TEST_EPOCH
    while time.time() < TEST_EPOCH:
        time.sleep(0.01)

    seq = 0
    next_send_at = TEST_EPOCH

    f = open(TX_CSV, "w", newline="", buffering=1)
    writer = csv.writer(f)
    writer.writerow(["wall_ts_ns", "test_t_sec", "bucket", "seq", "iface", "channel", "mode"])

    while True:
        test_t = time.time() - TEST_EPOCH
        if test_t >= TEST_DURATION:
            break

        now = time.time()
        if now < next_send_at:
            slack = next_send_at - now
            if slack > 0.001:
                time.sleep(slack)
            continue

        bucket  = compute_bucket()
        iface   = get_active_iface(bucket)
        channel = get_protocol_channel(bucket)

        send_ts_ns = time.time_ns()
        packet = pack_packet(seq, send_ts_ns)

        try:
            tx_sock_per_iface[iface].sendto(packet, (DEST_ADDR, TEST_PORT))
            with state_lock:
                tx_count += 1
        except OSError:
            pass

        writer.writerow([send_ts_ns, f"{test_t:.6f}", bucket, seq, iface, channel, MODE])

        seq += 1
        next_send_at += PACKET_INTERVAL

    f.close()
    print(f"[TX] sender done, {seq} packets emitted")


# ===========================================================================
# Receiver
# ===========================================================================

def receiver_loop():
    global rx_count, last_rx_ts_ns

    while time.time() < TEST_EPOCH:
        time.sleep(0.01)

    f = open(RX_CSV, "w", newline="", buffering=1)
    writer = csv.writer(f)
    writer.writerow(["wall_ts_ns", "test_t_sec", "bucket", "seq", "src",
                     "send_ts_ns", "latency_ns", "iface", "channel", "mode"])

    rx_sock.settimeout(0.5)
    n = 0
    while True:
        test_t = time.time() - TEST_EPOCH
        if test_t >= TEST_DURATION + 10:
            break

        try:
            data, addr = rx_sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            continue

        recv_ts_ns = time.time_ns()
        parsed = unpack_packet(data)
        if parsed is None:
            continue
        src_id, seq, send_ts_ns = parsed
        if src_id == NODE_ID:
            continue

        latency_ns = recv_ts_ns - send_ts_ns
        bucket  = compute_bucket()
        iface   = get_active_iface(bucket)
        channel = get_protocol_channel(bucket)

        with state_lock:
            rx_count += 1
            last_rx_ts_ns = recv_ts_ns
            recent_latencies.append(latency_ns)
        n += 1

        writer.writerow([recv_ts_ns, f"{test_t:.6f}", bucket, seq, src_id,
                         send_ts_ns, latency_ns, iface, channel, MODE])

    f.close()
    print(f"[RX] receiver done, {n} packets received")


# ===========================================================================
# Status writer (for dashboard)
# ===========================================================================

def status_writer_loop():
    while True:
        try:
            with state_lock:
                tx = tx_count
                rx = rx_count
                last_rx = last_rx_ts_ns
                lats = sorted(recent_latencies)

            if lats:
                p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
                mean_lat = sum(lats) / len(lats)
            else:
                p95 = 0
                mean_lat = 0

            bl = read_blacklist()
            bucket = compute_bucket()
            test_t = (time.time() - TEST_EPOCH) if TEST_EPOCH > 0 else 0

            status = {
                "node_id":         NODE_ID,
                "mode":            MODE,
                "run_id":          RUN_ID,
                "test_epoch":      TEST_EPOCH,
                "test_t":          test_t,
                "bucket":          bucket,
                "tx_count":        tx,
                "rx_count":        rx,
                "last_rx_ts_ns":   last_rx,
                "latency_p95_ns":  p95,
                "latency_mean_ns": mean_lat,
                "blacklist":       sorted(bl.keys(), key=lambda x: int(x)),
                "active_iface":    get_active_iface(bucket),
                "active_channel":  get_protocol_channel(bucket),
                "iface_channels":  {iface: get_iface_channel(iface) for iface in INTERFACES},
                "jammed_now":      get_jammed_channels(test_t, MODE) if test_t >= 0 else [],
                "transport":       TRANSPORT,
                "dest_addr":       DEST_ADDR,
                "updated_at":      time.time(),
            }

            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(status, f)
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass

        time.sleep(0.5)


# ===========================================================================
# Main
# ===========================================================================

def main():
    print("="*60)
    print(f" TESTWORKER — NODE_ID={NODE_ID} MODE={MODE} RUN_ID={RUN_ID}")
    print("="*60)
    print(f" TEST_EPOCH  : {TEST_EPOCH}")
    print(f" BUCKET_EPOCH: {BUCKET_EPOCH}   (offset {TEST_EPOCH - BUCKET_EPOCH:.0f}s)")
    print(f" TRANSPORT   : {TRANSPORT}  (DEST_ADDR = {DEST_ADDR}:{TEST_PORT})")
    print(f" OUTPUT_DIR  : {OUTPUT_DIR}")
    print(f" TX CSV      : {TX_CSV}")
    print(f" RX CSV      : {RX_CSV}")
    print("="*60 + "\n")

    if TEST_EPOCH == 0:
        print("[FATAL] TEST_EPOCH not set.")
        sys.exit(1)
    if NODE_ID not in ("A", "B"):
        print(f"[FATAL] NODE_ID must be A or B (got '{NODE_ID}')")
        sys.exit(1)
    if TRANSPORT not in ("unicast", "broadcast"):
        print(f"[FATAL] TRANSPORT must be 'unicast' or 'broadcast' (got '{TRANSPORT}')")
        sys.exit(1)
    if TRANSPORT == "unicast" and DEST_ADDR is None:
        print(f"[FATAL] TRANSPORT=unicast but PEER_IP could not be inferred from NODE_ID={NODE_ID!r}")
        sys.exit(1)
    if not os.path.isdir(OUTPUT_DIR):
        print(f"[FATAL] OUTPUT_DIR does not exist: {OUTPUT_DIR}")
        sys.exit(1)

    setup_sockets()

    # Clean any leftover tc state from a previous aborted run
    for iface in INTERFACES:
        release_jamming(iface)

    threads = [
        threading.Thread(target=sender_loop,        daemon=True, name="sender"),
        threading.Thread(target=receiver_loop,      daemon=True, name="receiver"),
        threading.Thread(target=jammer_loop,        daemon=True, name="jammer"),
        threading.Thread(target=status_writer_loop, daemon=True, name="status"),
    ]
    for t in threads:
        t.start()

    # Wait for TEST_EPOCH
    while time.time() < TEST_EPOCH:
        time.sleep(0.2)

    # Run for the test duration + a little drain time
    while time.time() - TEST_EPOCH < TEST_DURATION + 12:
        time.sleep(0.5)

    print("[TESTWORKER] Test complete, cleaning up tc...")
    for iface in INTERFACES:
        release_jamming(iface)

    time.sleep(2)
    print("[TESTWORKER] Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        for iface in INTERFACES:
            release_jamming(iface)
        print("[TESTWORKER] Interrupted, tc released.")
