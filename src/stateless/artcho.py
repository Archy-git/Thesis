import socket
import json
import time
import threading
import hmac
import hashlib
import subprocess
import os
import sys
import tempfile
import datetime
import re

# ===========================================================================
# ARTCHO UNIFIED NODE  (with MODE dispatch for thesis test harness)
# ===========================================================================
#
# Modes (set via MODE env var, default L3):
#   L1 — pin wlan0 to STATIC_CHANNEL, no hopping, no BL, no detection
#   L2 — TOTP hopping + dual-radio prep, BL learning disabled
#   L3 — full design (TOTP + BL + dual radio + kernel TX-drop detect)
#   L4 — TOTP + BL, single radio only (wlan0), retunes at bucket boundary
#
# Reproducibility: if TEST_EPOCH is set (Unix time), bucket counter starts
# at 0 at that wall-clock moment. Both nodes set the same TEST_EPOCH so
# their channel sequence is byte-identical across runs.
#
# Each node MUST have a unique NODE_ID:
#   NODE_ID=A python3 artcho.py
#   NODE_ID=B python3 artcho.py
# ===========================================================================

# --- Constants -------------------------------------------------------------

FREQUENCIES = [
    "2422", "2427", "2432", "2437", "2442",
    "2447", "2452", "2457", "2462",
]
FREQ_TO_CH = {
    "2422": "3", "2427": "4", "2432": "5", "2437": "6", "2442": "7",
    "2447": "8", "2452": "9", "2457": "10", "2462": "11",
}
CH_TO_FREQ = {v: k for k, v in FREQ_TO_CH.items()}

NETWORK_NAME  = "TacticalMesh"
CELL_BSSID    = "02:11:22:33:44:55"
SHARED_SECRET = b"University_Thesis_Secret_Key_2026"

BLACKLIST_FILE  = "/tmp/blacklist.json"
BROADCAST_ADDR  = "192.168.200.255"
PIGGYBACK_PORT  = 5001
BUCKET_SECONDS  = 2.0

BROADCAST_START    = 0.10
BROADCAST_INTERVAL = 0.05
BROADCAST_END      = 1.85
DETECTION_DEADLINE = 1.90

BLACKLIST_DURATION_BUCKETS = 30
BLACKLIST_DURATION_SECONDS = BLACKLIST_DURATION_BUCKETS * BUCKET_SECONDS

JAMMING_DROP_THRESHOLD = 5

INTERFACES = ["wlan0", "wlan1"]

NODE_ID = os.environ.get("NODE_ID", socket.gethostname())
MODE    = os.environ.get("MODE", "L3").upper()
STATIC_CHANNEL = os.environ.get("STATIC_CHANNEL", "6")
TEST_EPOCH     = float(os.environ.get("TEST_EPOCH", "0"))
# BUCKET_EPOCH is the reference moment for the TOTP bucket counter.
# It can differ from TEST_EPOCH (the real wall-clock start) so we can
# simulate "as if it were 00:00 / 05:00 / etc." without touching the system
# clock. Default to TEST_EPOCH for back-compat with old launches.
BUCKET_EPOCH   = float(os.environ.get("BUCKET_EPOCH", "0"))
if BUCKET_EPOCH == 0 and TEST_EPOCH > 0:
    BUCKET_EPOCH = TEST_EPOCH

# Mode-derived behavior flags.
#   L1  static channel,        no BL, no detect, no jamming-aware hop
#   L2  hop (TOTP),  dual radio, no BL, no detect, with jamming schedule
#   L3  hop (TOTP),  dual radio, BL+detect, with jamming schedule
#   L4  hop (TOTP), single radio (retune at boundary), no BL, no detect, no jam
#   L5  hop (TOTP),  dual radio, no BL, no detect, no jam
USE_BL          = MODE in ("L3",)
DETECT_ENABLED  = MODE in ("L3",)
DUAL_RADIO      = MODE in ("L2", "L3", "L5")
STATIC          = (MODE == "L1")


def compute_bucket():
    """Bucket counter — offset by BUCKET_EPOCH so simulated time windows
    produce independent TOTP sequences. BUCKET_EPOCH == TEST_EPOCH for
    plain runs; differs when testdash sets a simulated time window."""
    if BUCKET_EPOCH > 0:
        return int((time.time() - BUCKET_EPOCH) / BUCKET_SECONDS)
    return int(time.time() / BUCKET_SECONDS)


def bucket_start_wall_time(bucket):
    """Wall-clock time at the start of `bucket` (used for BL expiry math)."""
    if BUCKET_EPOCH > 0:
        return BUCKET_EPOCH + bucket * BUCKET_SECONDS
    return bucket * BUCKET_SECONDS


# ===========================================================================
# ArtchoNode
# ===========================================================================

class ArtchoNode:

    def __init__(self):
        self.lock = threading.Lock()

        self.bucket            = -1
        self.bucket_start_time = 0.0

        self.active_iface       = None
        self.active_channel     = None
        self.bg_iface           = None
        self.bg_target_channel  = None

        self.confirmed_bl = {}

        self.peer_heard_this_bucket = False
        self.next_broadcast_at      = -1.0
        self.detection_fired        = False
        self.frames_this_bucket     = 0

        self.tx_sock_per_iface = {}
        self.rx_sock           = None

    # ----- File I/O -------------------------------------------------------

    @staticmethod
    def read_blacklist_file():
        try:
            with open(BLACKLIST_FILE, "r") as f:
                data = json.load(f)
            now = time.time()
            return {ch: exp for ch, exp in data.get("channels", {}).items() if exp > now}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def write_blacklist_atomic(bl):
        fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=".blacklist_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"channels": bl}, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, BLACKLIST_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def filter_expired(bl):
        now = time.time()
        return {ch: exp for ch, exp in bl.items() if exp > now}

    # ----- Kernel TX-Drop Signal -----------------------------------------

    @staticmethod
    def get_egress_dropped(iface):
        try:
            out = subprocess.check_output(
                f"tc -s qdisc show dev {iface} root",
                shell=True, text=True, stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
            for line in out.split("\n"):
                m = re.search(r"\bdropped\s+(\d+)", line)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return 0

    # ----- TOTP (skip-pattern) -------------------------------------------

    @staticmethod
    def compute_totp(bucket, blacklist):
        h_bytes = bucket.to_bytes(8, byteorder="big", signed=True)
        h_int   = int(hmac.new(SHARED_SECRET, h_bytes, hashlib.sha256).hexdigest(), 16)
        base_index = h_int % len(FREQUENCIES)
        for offset in range(len(FREQUENCIES)):
            idx  = (base_index + offset) % len(FREQUENCIES)
            freq = FREQUENCIES[idx]
            ch   = FREQ_TO_CH[freq]
            if ch not in blacklist:
                return freq, ch
        freq = FREQUENCIES[base_index]
        return freq, FREQ_TO_CH[freq]

    @staticmethod
    def role_for_bucket(bucket):
        if not DUAL_RADIO:
            return ("wlan0", "wlan1")
        return ("wlan0", "wlan1") if bucket % 2 == 0 else ("wlan1", "wlan0")

    # ----- Radio ----------------------------------------------------------

    @staticmethod
    def swap_route(iface):
        subprocess.run(
            f"sudo ip route replace 192.168.200.0/24 dev {iface}",
            shell=True, stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def read_iface_channel(iface):
        """Read the current channel of iface from `iw dev <iface> info`.
        Returns the channel as a string (e.g. '6'), or None if unparseable."""
        try:
            out = subprocess.check_output(
                f"iw dev {iface} info",
                shell=True, text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            m = re.search(r"channel\s+(\d+)", out)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    @staticmethod
    def tune_iface(iface, freq, retries=2):
        """Move iface to `freq` in the project IBSS cell. Verifies success by
        reading back the channel from `iw dev`. Retries up to `retries` times
        on failure. Returns True on success, False if all attempts failed.
        Loud on failure — every retry prints to stdout so it shows up in the log.
        """
        expected_ch = FREQ_TO_CH.get(freq)
        if not expected_ch:
            print(f"[TUNE FAIL] {iface}: unknown freq {freq} (not in FREQ_TO_CH)")
            return False

        last_err = ""
        for attempt in range(retries + 1):
            # `ibss leave` is allowed to fail (iface might not be in IBSS yet).
            subprocess.run(
                f"sudo iw dev {iface} ibss leave",
                shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                timeout=3,
            )
            time.sleep(0.2)
            # `ibss join` is the load-bearing call.
            join = subprocess.run(
                f"sudo iw dev {iface} ibss join {NETWORK_NAME} {freq} "
                f"fixed-freq {CELL_BSSID}",
                shell=True, capture_output=True, text=True, timeout=5,
            )
            if join.returncode != 0:
                last_err = join.stderr.strip() or f"exit={join.returncode}"
                print(f"[TUNE WARN] {iface} attempt {attempt+1}/{retries+1}: "
                      f"ibss join exited {join.returncode}: {last_err}")

            # Give the kernel a moment to finish forming the cell, then verify.
            time.sleep(0.4)
            actual_ch = ArtchoNode.read_iface_channel(iface)
            if actual_ch == expected_ch:
                if attempt > 0:
                    print(f"[TUNE OK  ] {iface} -> ch{actual_ch} (attempt {attempt+1})")
                return True
            last_err = f"expected ch{expected_ch}, got ch{actual_ch!r}"
            print(f"[TUNE WARN] {iface} attempt {attempt+1}/{retries+1}: {last_err}")

        print(f"[TUNE FAIL] {iface}: could not be tuned to ch{expected_ch} "
              f"after {retries+1} attempts. Last error: {last_err}")
        print(f"[TUNE FAIL] {iface}: this iface will not work for the protocol.")
        return False

    # ----- HMAC -----------------------------------------------------------

    @staticmethod
    def _canonical(payload):
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def make_message(self, bucket, confirmed_bl):
        payload = {
            "node_id":      NODE_ID,
            "bucket":       bucket,
            "confirmed_bl": confirmed_bl,
            "mode":         MODE,
        }
        sig = hmac.new(SHARED_SECRET, self._canonical(payload), hashlib.sha256).hexdigest()
        return json.dumps({"payload": payload, "hmac": sig}).encode()

    @staticmethod
    def parse_message(data):
        try:
            msg          = json.loads(data.decode())
            payload      = msg["payload"]
            sig          = msg["hmac"]
            expected_sig = hmac.new(SHARED_SECRET, ArtchoNode._canonical(payload), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected_sig):
                return payload
        except Exception:
            pass
        return None

    # ----- Cold Start -----------------------------------------------------

    def cold_start(self):
        if STATIC:
            self._cold_start_l1()
            return
        if not DUAL_RADIO:
            self._cold_start_l4()
            return
        self._cold_start_dual()

    def _cold_start_l1(self):
        freq = CH_TO_FREQ[STATIC_CHANNEL]
        print(f"[INIT] L1 cold-start: pin wlan0 to ch{STATIC_CHANNEL} ({freq} MHz), wlan1 untouched")
        self.tune_iface("wlan0", freq)
        self.swap_route("wlan0")

        bucket = compute_bucket()
        with self.lock:
            self.bucket             = bucket
            self.bucket_start_time  = bucket_start_wall_time(bucket)
            self.active_iface       = "wlan0"
            self.active_channel     = STATIC_CHANNEL
            self.bg_iface           = "wlan1"
            self.bg_target_channel  = STATIC_CHANNEL
            self.peer_heard_this_bucket = False
            self.confirmed_bl       = {}

        # Wipe any stale blacklist file so L1 reports cleanly
        try:
            self.write_blacklist_atomic({})
        except Exception:
            pass

        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0
        print("[INIT] L1 cold-start complete.\n")

    def _cold_start_l4(self):
        # Single radio (wlan0). wlan1 is left idle.
        # In L4 (new definition), USE_BL is False so BL stays empty.
        self.confirmed_bl = self.read_blacklist_file() if USE_BL else {}
        bucket = compute_bucket()
        cur_freq, cur_ch = self.compute_totp(bucket, self.confirmed_bl)

        print(f"[INIT] L4 cold-start (bucket={bucket}): wlan0 -> ch{cur_ch} ({cur_freq} MHz), wlan1 idle")
        bl_str = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
        print(f"       confirmed blacklist: {bl_str}")

        self.tune_iface("wlan0", cur_freq)
        self.swap_route("wlan0")

        with self.lock:
            self.bucket             = bucket
            self.bucket_start_time  = bucket_start_wall_time(bucket)
            self.active_iface       = "wlan0"
            self.active_channel     = cur_ch
            self.bg_iface           = "wlan1"
            self.bg_target_channel  = None
            self.peer_heard_this_bucket = False

        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0
        print("[INIT] L4 cold-start complete.\n")

    def _cold_start_dual(self):
        # L2 / L3: dual-radio prep-tune
        self.confirmed_bl = self.read_blacklist_file() if USE_BL else {}
        bucket = compute_bucket()
        cur_freq, cur_ch = self.compute_totp(bucket,     self.confirmed_bl)
        nxt_freq, nxt_ch = self.compute_totp(bucket + 1, self.confirmed_bl)
        active, bg       = self.role_for_bucket(bucket)

        print(f"[INIT] {MODE} cold-start (bucket={bucket}):")
        print(f"       active  {active} -> ch{cur_ch} ({cur_freq} MHz)")
        print(f"       bg      {bg} -> ch{nxt_ch} ({nxt_freq} MHz)")
        bl_str = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
        print(f"       confirmed blacklist: {bl_str}")

        self.tune_iface(active, cur_freq)
        self.tune_iface(bg,     nxt_freq)
        self.swap_route(active)

        with self.lock:
            self.bucket             = bucket
            self.bucket_start_time  = bucket_start_wall_time(bucket)
            self.active_iface       = active
            self.active_channel     = cur_ch
            self.bg_iface           = bg
            self.bg_target_channel  = nxt_ch
            self.peer_heard_this_bucket = False

        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0
        print(f"[INIT] {MODE} cold-start complete.\n")

    # ----- Bucket Boundary ------------------------------------------------

    def on_bucket_boundary(self, new_bucket):
        if STATIC:
            self._boundary_l1(new_bucket)
            return
        if not DUAL_RADIO:
            self._boundary_l4(new_bucket)
            return
        self._boundary_dual(new_bucket)

    def _boundary_l1(self, new_bucket):
        # No physical retune. Just tick state.
        with self.lock:
            self.bucket = new_bucket
            self.bucket_start_time = bucket_start_wall_time(new_bucket)
            self.peer_heard_this_bucket = False
        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] HOP bucket={new_bucket} L1 static ch{STATIC_CHANNEL} wlan0")

    def _boundary_l4(self, new_bucket):
        # Single radio: retune wlan0 directly to new channel (LOSES ~700ms)
        with self.lock:
            if USE_BL:
                disk_bl = self.filter_expired(self.read_blacklist_file())
                for ch, exp in disk_bl.items():
                    if ch not in self.confirmed_bl or exp > self.confirmed_bl[ch]:
                        self.confirmed_bl[ch] = exp
                self.confirmed_bl = self.filter_expired(self.confirmed_bl)
                self.write_blacklist_atomic(self.confirmed_bl)

        cur_freq, cur_ch = self.compute_totp(new_bucket, self.confirmed_bl)

        with self.lock:
            self.bucket             = new_bucket
            self.bucket_start_time  = bucket_start_wall_time(new_bucket)
            self.active_iface       = "wlan0"
            self.active_channel     = cur_ch
            self.peer_heard_this_bucket = False

        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0

        ts     = datetime.datetime.now().strftime("%H:%M:%S")
        bl_str = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
        print(f"[{ts}] HOP bucket={new_bucket} L4 wlan0 -> ch{cur_ch}  (retuning, ~700ms loss)  | BL={bl_str}")

        self.tune_iface("wlan0", cur_freq)
        self.swap_route("wlan0")

    def _boundary_dual(self, new_bucket):
        with self.lock:
            if USE_BL:
                disk_bl = self.filter_expired(self.read_blacklist_file())
                for ch, exp in disk_bl.items():
                    if ch not in self.confirmed_bl or exp > self.confirmed_bl[ch]:
                        self.confirmed_bl[ch] = exp
                self.confirmed_bl = self.filter_expired(self.confirmed_bl)
                self.write_blacklist_atomic(self.confirmed_bl)
            else:
                # L2 / L5: dual-radio hopping without BL learning
                self.confirmed_bl = {}

        cur_freq, cur_ch = self.compute_totp(new_bucket,     self.confirmed_bl)
        nxt_freq, nxt_ch = self.compute_totp(new_bucket + 1, self.confirmed_bl)
        new_active, new_bg = self.role_for_bucket(new_bucket)

        physical_active_channel = self.bg_target_channel

        with self.lock:
            self.bucket             = new_bucket
            self.bucket_start_time  = bucket_start_wall_time(new_bucket)
            self.active_iface       = new_active
            self.active_channel     = physical_active_channel if physical_active_channel else cur_ch
            self.bg_iface           = new_bg
            self.bg_target_channel  = nxt_ch
            self.peer_heard_this_bucket = False

        self.next_broadcast_at  = BROADCAST_START
        self.detection_fired    = False
        self.frames_this_bucket = 0

        ts     = datetime.datetime.now().strftime("%H:%M:%S")
        bl_str = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
        lag    = ""
        if physical_active_channel and physical_active_channel != cur_ch:
            lag = f"  (LAG: totp_says=ch{cur_ch}, physical=ch{physical_active_channel})"
        print(f"[{ts}] HOP bucket={new_bucket} {MODE} active={new_active} ch{self.active_channel}{lag}  | prep {new_bg} ch{nxt_ch}  | BL={bl_str}")

        self.swap_route(new_active)
        self.tune_iface(new_bg, nxt_freq)

    # ----- TX (via SO_BINDTODEVICE) --------------------------------------

    def maybe_broadcast(self, now):
        if self.next_broadcast_at > BROADCAST_END:
            return
        elapsed = now - self.bucket_start_time
        if elapsed < self.next_broadcast_at:
            return

        with self.lock:
            msg = self.make_message(self.bucket, self.confirmed_bl)
            bl_keys = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
            bucket  = self.bucket
            iface   = self.active_iface
            ch      = self.active_channel

        tx_sock = self.tx_sock_per_iface.get(iface)
        if tx_sock is None:
            return

        try:
            tx_sock.sendto(msg, (BROADCAST_ADDR, PIGGYBACK_PORT))
            self.frames_this_bucket += 1
            if self.frames_this_bucket == 1:
                print(f"[TX  ->  *] bucket={bucket} {MODE} active={iface} ch{ch}  BL={bl_keys}")
        except OSError:
            pass

        self.next_broadcast_at += BROADCAST_INTERVAL

    # ----- Detection — silence + kernel TX-drop --------------------------

    def maybe_detect(self, now):
        if not DETECT_ENABLED:
            return
        if self.detection_fired:
            return
        elapsed = now - self.bucket_start_time
        if elapsed < DETECTION_DEADLINE:
            return
        self.detection_fired = True

        with self.lock:
            heard  = self.peer_heard_this_bucket
            ch     = self.active_channel
            iface  = self.active_iface
            bucket = self.bucket
            frames = self.frames_this_bucket

        if ch is None or heard:
            return

        drops = self.get_egress_dropped(iface)

        if drops >= JAMMING_DROP_THRESHOLD:
            expiry = bucket_start_wall_time(bucket + BLACKLIST_DURATION_BUCKETS)
            with self.lock:
                if ch in self.confirmed_bl:
                    return
                self.confirmed_bl[ch] = expiry
                self.write_blacklist_atomic(self.confirmed_bl)
                bl_str = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))
            exp_bucket = bucket + BLACKLIST_DURATION_BUCKETS
            print(f"[!! JAMMED  ] bucket={bucket} {MODE} active={iface} ch{ch}  silent + egress_drops={drops}  -> BL until bucket={exp_bucket}  BL={bl_str}")
        else:
            print(f"[?? SOFT    ] bucket={bucket} {MODE} active={iface} ch{ch}  silent across {frames} frames but egress_drops={drops}  (no tc evidence)")

    # ----- Listener -------------------------------------------------------

    def listener_loop(self):
        last_seen_peer_bucket = {}
        while True:
            try:
                data, addr = self.rx_sock.recvfrom(8192)
            except OSError:
                continue

            payload = self.parse_message(data)
            if payload is None:
                continue

            peer_id = payload.get("node_id", "?")
            if peer_id == NODE_ID:
                continue

            peer_bucket        = int(payload.get("bucket", -1))
            peer_confirmed_raw = payload.get("confirmed_bl", {}) or {}
            peer_confirmed     = {str(k): float(v) for k, v in peer_confirmed_raw.items()}

            propagated = []

            with self.lock:
                if USE_BL:
                    for ch, exp in peer_confirmed.items():
                        if ch not in self.confirmed_bl or exp > self.confirmed_bl[ch]:
                            if ch not in self.confirmed_bl:
                                propagated.append(ch)
                            self.confirmed_bl[ch] = exp
                    self.confirmed_bl = self.filter_expired(self.confirmed_bl)
                    self.write_blacklist_atomic(self.confirmed_bl)

                if abs(peer_bucket - self.bucket) <= 1:
                    self.peer_heard_this_bucket = True

                bl_list = sorted(self.confirmed_bl.keys(), key=lambda x: int(x))

            if last_seen_peer_bucket.get(peer_id) != peer_bucket:
                last_seen_peer_bucket[peer_id] = peer_bucket
                print(f"[RX  <- {peer_id}] bucket={peer_bucket}  peer_BL={list(peer_confirmed.keys())}  my_BL={bl_list}")

            for ch in propagated:
                exp_bucket = int(self.confirmed_bl[ch] / BUCKET_SECONDS)
                print(f"[<= MERGED  ] ch{ch} from peer's confirmed -> expires bucket={exp_bucket}")

    # ----- Sockets --------------------------------------------------------

    def setup_sockets(self):
        for iface in INTERFACES:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
            except PermissionError:
                print(f"[FATAL] SO_BINDTODEVICE requires CAP_NET_RAW. Run with sudo.")
                sys.exit(1)
            self.tx_sock_per_iface[iface] = s

        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rx_sock.bind(("0.0.0.0", PIGGYBACK_PORT))

    # ----- Run ------------------------------------------------------------

    def run(self):
        print("="*60)
        print(f" ARTCHO NODE — MODE={MODE} — NODE_ID={NODE_ID}")
        print("="*60)
        print(f" Bucket duration : {BUCKET_SECONDS}s")
        print(f" TEST_EPOCH      : {TEST_EPOCH if TEST_EPOCH > 0 else 'wall-clock (no offset)'}")
        print(f" BUCKET_EPOCH    : {BUCKET_EPOCH if BUCKET_EPOCH > 0 else 'wall-clock (no offset)'}")
        if BUCKET_EPOCH > 0 and TEST_EPOCH > 0:
            sim_offset = TEST_EPOCH - BUCKET_EPOCH
            print(f" sim offset      : {sim_offset:.0f}s  ({sim_offset/3600:.2f}h)")
        if STATIC:
            print(f" STATIC_CHANNEL  : {STATIC_CHANNEL}")
        print(f" Use blacklist   : {USE_BL}")
        print(f" Detect enabled  : {DETECT_ENABLED}")
        print(f" Dual radio      : {DUAL_RADIO}")
        if NODE_ID in ("raspberrypi", "localhost", ""):
            print()
            print(" !! WARNING: NODE_ID looks like a default hostname.")
            print(" !! Re-launch with:  NODE_ID=A sudo python3 artcho.py")
        print("="*60 + "\n")

        self.setup_sockets()

        # If TEST_EPOCH is in the future, wait for it
        if TEST_EPOCH > 0:
            wait_s = TEST_EPOCH - time.time()
            if wait_s > 0:
                print(f"[WAIT] sleeping {wait_s:.1f}s until TEST_EPOCH...")
                time.sleep(wait_s)

        self.cold_start()
        threading.Thread(target=self.listener_loop, daemon=True).start()

        last_bucket = self.bucket

        while True:
            now    = time.time()
            bucket = compute_bucket()

            if bucket != last_bucket:
                last_bucket = bucket
                self.on_bucket_boundary(bucket)

            self.maybe_broadcast(now)
            self.maybe_detect(now)

            time.sleep(0.02)


def main():
    node = ArtchoNode()
    try:
        node.run()
    except KeyboardInterrupt:
        print("\n[*] ARTCHO node terminated.")


if __name__ == "__main__":
    main()
