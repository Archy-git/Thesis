#!/usr/bin/env python3
"""
ARTCHO TEST DASHBOARD — Node A orchestrator + multi-window experiment driver.

Runs all 5 simulated time windows for ONE level (L1..L5) back-to-back.
Output goes to ./final_experiment/<MODE>/window_<NAME>/ and is chowned
to the invoking user at the end of each window.

Usage:
    sudo python3 testdash.py          # pick mode from menu

Env overrides:
    STATIC_CHANNEL=7   # only meaningful for L1; default ch7
    TRANSPORT=unicast  # or 'broadcast'; default unicast
"""

import os
import sys
import subprocess
import time
import json
import threading
import curses
import shutil

from schedule import (
    SCHEDULE_JAMMED, SCHEDULE_CLEAN, TEST_DURATION, WINDOWS,
    get_jammed_channels, describe_schedule, schedule_for_mode,
)

# ===========================================================================
# Config
# ===========================================================================

NODE_B_USER     = "rpi5-14"
NODE_B_IP       = "192.168.200.2"
NODE_B_PASSWORD = "123456"
NODE_B_HOME     = f"/home/{NODE_B_USER}"

FILES_TO_PUSH = ["artcho.py", "testworker.py", "schedule.py"]

BUCKET_SECONDS = 2.0
PACKET_RATE_HZ = 20

STATUS_FILE_A = "/tmp/testworker_status_A.json"

# Base directory for all experiment outputs, in CWD
BASE_OUTPUT_DIR = "final_experiment"

# Cooldown between windows (let radio + tc state fully settle)
INTER_WINDOW_COOLDOWN_S = 30


MODE_DESCRIPTIONS = {
    "L1": "Static channel (no hopping, no BL) — worst-case floor, with jamming",
    "L2": "TOTP hopping, no BL — blind hopping baseline, with jamming",
    "L3": "TOTP + BL + dual radio — full design, with jamming",
    "L4": "Single radio hopping (retunes at boundary) — NO jamming",
    "L5": "Dual radio hopping (prep-tune) — NO jamming",
}


# ===========================================================================
# Misc helpers
# ===========================================================================

def sudo_user():
    """The non-root user who invoked sudo (or None)."""
    return os.environ.get("SUDO_USER")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def chown_to_user(path):
    """Recursively chown `path` to SUDO_USER if defined."""
    user = sudo_user()
    if not user or not os.path.exists(path):
        return
    try:
        subprocess.run(["chown", "-R", f"{user}:{user}", path],
                       check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def check_sshpass():
    if not shutil.which("sshpass"):
        print("[FATAL] sshpass not installed. Run:  sudo apt install sshpass")
        sys.exit(1)


def require_root():
    if os.geteuid() != 0:
        print("[FATAL] testdash must be run with sudo (it manages tc / pkill / etc).")
        print("        Try:  sudo -E python3 testdash.py")
        sys.exit(1)


# ===========================================================================
# SSH / scp helpers
# ===========================================================================

def ssh_cmd(cmd, timeout=15, capture=False, check=True):
    full = [
        "sshpass", "-p", NODE_B_PASSWORD,
        "ssh",
        "-T",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
        "-o", "LogLevel=ERROR",
        "-o", "ServerAliveInterval=5",
        f"{NODE_B_USER}@{NODE_B_IP}",
        cmd,
    ]
    if capture:
        return subprocess.check_output(full, text=True, timeout=timeout)
    res = subprocess.run(full, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"SSH failed: {cmd}")
    return res


def scp_to_b(local_path, remote_path):
    subprocess.run([
        "sshpass", "-p", NODE_B_PASSWORD,
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        local_path,
        f"{NODE_B_USER}@{NODE_B_IP}:{remote_path}",
    ], check=True, timeout=60)


def scp_from_b(remote_path, local_path, timeout=60):
    subprocess.run([
        "sshpass", "-p", NODE_B_PASSWORD,
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{NODE_B_USER}@{NODE_B_IP}:{remote_path}",
        local_path,
    ], check=True, timeout=timeout)


# ===========================================================================
# Per-level pre-flight (runs once before all 5 windows)
# ===========================================================================

def preflight_once():
    """Runs once at start of a level. SSH/sudo/clock check, push scripts."""

    print("[PREFLIGHT] Checking SSH to Node B ...")
    try:
        out = ssh_cmd("echo ok", capture=True)
        assert "ok" in out
    except Exception as e:
        print(f"[FATAL] SSH to B failed: {e}")
        sys.exit(1)
    print("[PREFLIGHT]   SSH OK")

    print("[PREFLIGHT] Testing sudo on Node B ...")
    try:
        out = ssh_cmd(
            f'echo "{NODE_B_PASSWORD}" | sudo -S -p "" -k -- whoami 2>&1',
            capture=True, timeout=10,
        )
        if "root" not in out:
            print(f"[FATAL] sudo on B did not return root, got: {out!r}")
            sys.exit(1)
        print("[PREFLIGHT]   sudo OK")
    except subprocess.TimeoutExpired:
        print("[FATAL] sudo test on B hung — verify NODE_B_PASSWORD is correct.")
        sys.exit(1)

    print("[PREFLIGHT] Checking clock skew (NTP midpoint method) ...")
    try:
        samples = []
        for _ in range(3):
            t1 = time.time()
            remote_ts = float(ssh_cmd("date +%s.%N", capture=True, timeout=10).strip())
            t4 = time.time()
            midpoint = (t1 + t4) / 2.0
            samples.append((remote_ts - midpoint, t4 - t1))
        samples.sort(key=lambda x: x[0])
        offset_s, rtt_s = samples[len(samples) // 2]
        skew_ms = abs(offset_s) * 1000.0
        rtt_ms  = rtt_s * 1000.0
        print(f"[PREFLIGHT]   clock skew: {skew_ms:.1f} ms  (RTT {rtt_ms:.0f} ms)")
        if skew_ms > 50 and skew_ms > rtt_ms / 4:
            print(f"[WARN]   clock skew {skew_ms:.0f}ms > 50ms — sudo chronyc makestep on BOTH nodes")
            ans = input("        Continue anyway? [y/N] ").strip().lower()
            if ans != "y":
                sys.exit(0)
    except Exception as e:
        print(f"[WARN] clock check failed: {e}")

    print("[PREFLIGHT] Pushing scripts to Node B ...")
    for f in FILES_TO_PUSH:
        if not os.path.exists(f):
            print(f"[FATAL] {f} not in current directory.")
            sys.exit(1)
        scp_to_b(f, f"{NODE_B_HOME}/{f}")
        print(f"[PREFLIGHT]   pushed {f}")

    print("[PREFLIGHT] Done.\n")


# ===========================================================================
# Per-window cleanup (runs before each window)
# ===========================================================================

def per_window_cleanup():
    """Kill stale processes, clear tc, wipe stale status files. Runs before
    every window so each starts clean even if the previous one crashed."""
    pw = NODE_B_PASSWORD
    # Remote
    cleanup_remote = (
        f'echo "{pw}" | sudo -S -p "" -- bash -c "'
        f'pkill -9 -f artcho.py 2>/dev/null; pkill -9 -f testworker.py 2>/dev/null; '
        f'sleep 1; '
        f'for i in wlan0 wlan1; do tc qdisc del dev \\$i root 2>/dev/null; '
        f'tc qdisc del dev \\$i ingress 2>/dev/null; done; '
        f'rm -f /tmp/blacklist.json /tmp/testworker_status_*.json; '
        f'true"'
    )
    try:
        ssh_cmd(cleanup_remote, check=False, timeout=15)
    except Exception:
        pass
    # Local
    subprocess.run(["pkill", "-9", "-f", "artcho.py"], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", "testworker.py"], stderr=subprocess.DEVNULL)
    time.sleep(1)
    for iface in ("wlan0", "wlan1"):
        subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"],
                       stderr=subprocess.DEVNULL)
        subprocess.run(["tc", "qdisc", "del", "dev", iface, "ingress"],
                       stderr=subprocess.DEVNULL)
    subprocess.run(
        ["rm", "-f",
         "/tmp/blacklist.json",
         "/tmp/testworker_status_A.json",
         "/tmp/testworker_status_B.json"],
        stderr=subprocess.DEVNULL,
    )


# ===========================================================================
# Launching nodes for one window
# ===========================================================================

def launch_node_b(mode, test_epoch, bucket_epoch, run_id, static_channel,
                  transport, remote_output_dir):
    env_parts = [
        f"NODE_ID=B",
        f"MODE={mode}",
        f"TEST_EPOCH={test_epoch}",
        f"BUCKET_EPOCH={bucket_epoch}",
        f"RUN_ID={run_id}",
        f"TRANSPORT={transport}",
        f"OUTPUT_DIR={remote_output_dir}",
    ]
    if static_channel:
        env_parts.append(f"STATIC_CHANNEL={static_channel}")
    env_str = " ".join(env_parts)

    artcho_log = f"{remote_output_dir}/artcho_B.log"
    worker_log = f"{remote_output_dir}/testworker_B.log"
    pw = NODE_B_PASSWORD

    inner_a = (
        f'echo "{pw}" | sudo -S -E -p "" '
        f'{env_str} python3 artcho.py > {artcho_log} 2>&1'
    )
    inner_w = (
        f'echo "{pw}" | sudo -S -E -p "" '
        f'{env_str} python3 testworker.py > {worker_log} 2>&1'
    )

    remote = (
        f'mkdir -p {remote_output_dir} ; '
        f'cd {NODE_B_HOME} ; '
        f"nohup bash -c '{inner_a}' </dev/null >/dev/null 2>&1 & "
        f'sleep 0.5 ; '
        f"nohup bash -c '{inner_w}' </dev/null >/dev/null 2>&1 & "
        f'echo B-LAUNCHED'
    )

    try:
        out = ssh_cmd(remote, capture=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("[FATAL] launch_node_b timed out — verify NODE_B_PASSWORD and sudo on B.")
        raise

    if "B-LAUNCHED" not in out:
        print(f"[FATAL] launch_node_b: unexpected response: {out!r}")
        raise RuntimeError("Node B launch failed")

    # Verify both processes are alive
    time.sleep(2.0)
    try:
        out = ssh_cmd(
            f'echo "{pw}" | sudo -S -p "" pgrep -af "python3 (artcho|testworker)" || true',
            capture=True, timeout=8,
        )
        running = []
        if "artcho.py" in out:
            running.append("artcho")
        if "testworker.py" in out:
            running.append("testworker")
        if len(running) == 2:
            print(f"[NODE_B]   processes alive ({', '.join(running)})")
        else:
            print(f"[NODE_B]   WARNING: only running: {running or 'NONE'}")
            print(f"[NODE_B]   pgrep output: {out!r}")
    except Exception as e:
        print(f"[NODE_B]   could not verify processes: {e}")


def launch_node_a(mode, test_epoch, bucket_epoch, run_id, static_channel,
                  transport, window_dir):
    env = os.environ.copy()
    env.update({
        "NODE_ID":      "A",
        "MODE":         mode,
        "TEST_EPOCH":   str(test_epoch),
        "BUCKET_EPOCH": str(bucket_epoch),
        "RUN_ID":       run_id,
        "TRANSPORT":    transport,
        "OUTPUT_DIR":   window_dir,
    })
    if static_channel:
        env["STATIC_CHANNEL"] = str(static_channel)

    artcho_log = open(f"{window_dir}/artcho_A.log", "w")
    worker_log = open(f"{window_dir}/testworker_A.log", "w")

    p1 = subprocess.Popen(
        ["python3", "artcho.py"],
        env=env, stdout=artcho_log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    p2 = subprocess.Popen(
        ["python3", "testworker.py"],
        env=env, stdout=worker_log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    print(f"[NODE_A]   artcho pid={p1.pid}, testworker pid={p2.pid}")
    return p1, p2


# ===========================================================================
# Pull Node B's files into window_dir at end of window
# ===========================================================================

def pull_b_files(mode, run_id, window_dir, remote_output_dir):
    print("[PULL]   pulling Node B's files ...")
    pw = NODE_B_PASSWORD
    files = [
        (f"{remote_output_dir}/test_{mode}_{run_id}_B_tx.csv", f"{window_dir}/test_{mode}_{run_id}_B_tx.csv"),
        (f"{remote_output_dir}/test_{mode}_{run_id}_B_rx.csv", f"{window_dir}/test_{mode}_{run_id}_B_rx.csv"),
        (f"{remote_output_dir}/artcho_B.log",                  f"{window_dir}/artcho_B.log"),
        (f"{remote_output_dir}/testworker_B.log",              f"{window_dir}/testworker_B.log"),
    ]
    for remote, local in files:
        for attempt in range(5):
            try:
                scp_from_b(remote, local)
                print(f"[PULL]     ok: {os.path.basename(remote)}")
                break
            except Exception as e:
                if attempt == 4:
                    print(f"[PULL]     FAILED: {os.path.basename(remote)}: {e}")
                else:
                    time.sleep(3)

    # Snapshot iface state on B for diagnostics
    try:
        iw_dump = ssh_cmd(
            f'echo "{pw}" | sudo -S -p "" -- bash -c "'
            f'echo === wlan0 ===; iw dev wlan0 info; '
            f'echo === wlan1 ===; iw dev wlan1 info; '
            f'echo === ip ===; ip -4 addr show wlan0; ip -4 addr show wlan1"',
            capture=True, timeout=10,
        )
        with open(f"{window_dir}/iw_dump_B.txt", "w") as f:
            f.write(iw_dump)
    except Exception as e:
        print(f"[PULL]   could not snapshot B's iface state: {e}")

    # Clean B's window output dir to keep B's /tmp tidy
    try:
        ssh_cmd(
            f'echo "{pw}" | sudo -S -p "" rm -rf {remote_output_dir}',
            check=False, timeout=8,
        )
    except Exception:
        pass


# ===========================================================================
# Live ncurses dashboard (one per window)
# ===========================================================================

def read_status_a(expected_run_id=None):
    try:
        with open(STATUS_FILE_A) as f:
            status = json.load(f)
        if expected_run_id is not None and status.get("run_id") != expected_run_id:
            return None
        return status
    except Exception:
        return None


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds//60:d}:{seconds%60:02d}"


def draw_dashboard(stdscr, mode, run_id, test_epoch, window_idx, window_total,
                   window_name):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(500)

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN,  -1)
        curses.init_pair(2, curses.COLOR_RED,    -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN,   -1)
        C_OK, C_BAD, C_WARN, C_INFO = 1, 2, 3, 4
    except curses.error:
        C_OK = C_BAD = C_WARN = C_INFO = 0

    aborted = False
    schedule = schedule_for_mode(mode)

    while True:
        try:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            now = time.time()
            test_t = now - test_epoch
            remaining = TEST_DURATION - test_t

            title = (f" ARTCHO  MODE={mode}  "
                     f"WINDOW {window_idx}/{window_total} ({window_name})  "
                     f"RUN={run_id} ")
            stdscr.addstr(0, 0, title.center(w, "="), curses.A_BOLD)

            row = 2
            if test_t < 0:
                stdscr.addstr(row, 0, f"Status: waiting for TEST_EPOCH in {-test_t:5.1f}s",
                              curses.color_pair(C_WARN))
                row += 2
            else:
                pct = min(1.0, max(0.0, test_t / TEST_DURATION))
                bar_w = max(10, w - 40)
                filled = int(bar_w * pct)
                bar = "#" * filled + "." * (bar_w - filled)
                stdscr.addstr(row, 0, f"Elapsed: {fmt_time(test_t)} / {fmt_time(TEST_DURATION)}  "
                                       f"Remaining: {fmt_time(remaining)}",
                              curses.color_pair(C_INFO))
                row += 1
                stdscr.addstr(row, 0, f"[{bar}] {pct*100:5.1f}%")
                row += 2

            # Schedule block — compressed for 20-min schedules (20 entries)
            stdscr.addstr(row, 0, "--- SCHEDULE ---".ljust(w, "-"), curses.A_BOLD)
            row += 1
            for start, end, channels in schedule:
                if row >= h - 6:
                    stdscr.addstr(row, 0, "  ... (truncated)", curses.A_DIM)
                    row += 1
                    break
                is_now = (start <= test_t < end)
                marker = ">>" if is_now else "  "
                ch_str = ",".join(channels) if channels else "clean"
                line = f"{marker} {fmt_time(start)}-{fmt_time(end)}   {ch_str}"
                attr = curses.A_BOLD | curses.color_pair(C_WARN) if is_now else curses.A_DIM
                stdscr.addstr(row, 0, line[:w-1], attr)
                row += 1

            row += 1
            if row >= h - 1:
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == ord("q"): aborted = True; break
                if test_t > TEST_DURATION + 12: break
                continue

            # Node A status
            stdscr.addstr(row, 0, "--- NODE A (live) ---".ljust(w, "-"), curses.A_BOLD)
            row += 1
            status = read_status_a(expected_run_id=run_id)
            if status:
                bucket   = status.get("bucket", "?")
                iface    = status.get("active_iface", "?")
                channel  = status.get("active_channel", "?")
                bl       = status.get("blacklist", [])
                iface_ch = status.get("iface_channels", {})
                tx       = status.get("tx_count", 0)
                rx       = status.get("rx_count", 0)
                p95_ns   = status.get("latency_p95_ns", 0)
                last_rx  = status.get("last_rx_ts_ns", 0)

                stdscr.addstr(row, 0, (f"  bucket={bucket}   active={iface} ch{channel}   "
                                       f"wlan0=ch{iface_ch.get('wlan0','?')}   "
                                       f"wlan1=ch{iface_ch.get('wlan1','?')}")[:w-1])
                row += 1
                stdscr.addstr(row, 0, f"  blacklist: {bl}"[:w-1])
                row += 1
                stdscr.addstr(row, 0, f"  TX: {tx}    RX (from B): {rx}    p95: {p95_ns/1e6:.2f} ms")
                row += 1

                if test_t > 0:
                    expected = test_t * PACKET_RATE_HZ
                    pdr = (rx / expected * 100) if expected > 0 else 0
                    color = C_OK if pdr > 90 else (C_WARN if pdr > 60 else C_BAD)
                    stdscr.addstr(row, 0, f"  live PDR: {pdr:6.2f}%",
                                  curses.color_pair(color) | curses.A_BOLD)
                    row += 1
                if last_rx > 0:
                    age = (time.time_ns() - last_rx) / 1e9
                    color = C_OK if age < 1.0 else (C_WARN if age < 5.0 else C_BAD)
                    stdscr.addstr(row, 0, f"  last RX from B: {age:.2f}s ago",
                                  curses.color_pair(color))
                    row += 1
            else:
                stdscr.addstr(row, 0, "  (no status yet)", curses.color_pair(C_WARN))
                row += 1

            row += 1
            if row < h - 1:
                stdscr.addstr(row, 0, "Keys: q = abort this window only",
                              curses.A_DIM)

            stdscr.refresh()

            ch = stdscr.getch()
            if ch == ord("q"):
                aborted = True
                break
            if test_t > TEST_DURATION + 12:
                break

        except curses.error:
            try: stdscr.refresh()
            except: pass

    return aborted


# ===========================================================================
# Run one window end-to-end
# ===========================================================================

def run_one_window(mode, window_idx, window_total, window_name, sim_offset,
                   static_channel, transport, level_dir):
    """Runs preflight cleanup -> launches both nodes -> dashboard ->
    cleanup -> pulls B's files -> writes config -> runs analyzer.
    Returns True on success, False on failure (so the outer loop can continue)."""
    print("\n" + "=" * 70)
    print(f" WINDOW {window_idx}/{window_total}  —  {window_name}  —  MODE={mode}")
    print("=" * 70)

    window_dir = os.path.join(level_dir, f"window_{window_name}")
    ensure_dir(window_dir)
    remote_output_dir = f"/tmp/artcho_run_{int(time.time())}"

    run_id = f"{window_name}_{time.strftime('%Y%m%d_%H%M%S')}"

    # Compute epochs
    now = time.time()
    test_epoch = (int(now / BUCKET_SECONDS) + 1) * BUCKET_SECONDS + 20.0
    bucket_epoch = test_epoch - sim_offset
    print(f"[EPOCH] test_epoch (wall start) = {test_epoch:.3f}")
    print(f"[EPOCH] bucket_epoch (sim ref)  = {bucket_epoch:.3f}  "
          f"(simulated +{sim_offset}s offset)")
    print(f"[OUT]   window_dir = {os.path.abspath(window_dir)}")

    # Write config sidecar so analyze.py can find sim_offset later
    config = {
        "mode":           mode,
        "window_name":    window_name,
        "window_idx":     window_idx,
        "window_total":   window_total,
        "sim_offset_s":   sim_offset,
        "test_epoch":     test_epoch,
        "bucket_epoch":   bucket_epoch,
        "run_id":         run_id,
        "transport":      transport,
        "static_channel": static_channel,
        "test_duration":  TEST_DURATION,
        "wall_start_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(test_epoch)),
    }
    with open(f"{window_dir}/config.json", "w") as f:
        json.dump(config, f, indent=2)

    # Cleanup before launching
    per_window_cleanup()

    # Launch B
    try:
        launch_node_b(mode, test_epoch, bucket_epoch, run_id,
                      static_channel, transport, remote_output_dir)
    except Exception as e:
        print(f"[ERROR] launch_node_b failed: {e}")
        return False

    # Launch A
    try:
        proc_a_artcho, proc_a_worker = launch_node_a(
            mode, test_epoch, bucket_epoch, run_id,
            static_channel, transport, window_dir,
        )
    except Exception as e:
        print(f"[ERROR] launch_node_a failed: {e}")
        return False

    # Live dashboard for the duration of the window
    print("\n[STARTED] Dashboard opening in 1s...\n")
    time.sleep(1)
    try:
        aborted = curses.wrapper(
            draw_dashboard, mode, run_id, test_epoch,
            window_idx, window_total, window_name,
        )
    except KeyboardInterrupt:
        aborted = True

    if aborted:
        print(f"\n[ABORTED] Window {window_name} aborted.")
    else:
        print(f"\n[COMPLETE] Window {window_name} reached test duration.")

    # Cleanup local + pull B's files
    per_window_cleanup()
    pull_b_files(mode, run_id, window_dir, remote_output_dir)

    # Run analyzer against window_dir
    if os.path.exists("analyze.py"):
        print(f"\n[ANALYZE] running analyze.py against {window_dir} ...")
        result = subprocess.run(
            ["python3", "analyze.py",
             "--mode", mode,
             "--run-id", run_id,
             "--base-dir", window_dir],
            check=False,
        )
        if result.returncode != 0:
            print(f"[ANALYZE] analyzer exited non-zero ({result.returncode})")
    else:
        print("[ANALYZE] analyze.py not found in cwd, skipping")

    # chown the whole level dir to the user
    chown_to_user(level_dir)
    return not aborted


# ===========================================================================
# Mode selection
# ===========================================================================

def select_mode():
    print("\nSelect test level:")
    for i, m in enumerate(("L1", "L2", "L3", "L4", "L5"), 1):
        print(f"  {i}) {m}: {MODE_DESCRIPTIONS[m]}")
    while True:
        choice = input("Enter 1-5: ").strip()
        if choice in ("1", "2", "3", "4", "5"):
            return ("L1", "L2", "L3", "L4", "L5")[int(choice) - 1]


# ===========================================================================
# Main
# ===========================================================================

def main():
    require_root()
    check_sshpass()

    mode = select_mode()
    static_channel = os.environ.get("STATIC_CHANNEL", "7") if mode == "L1" else None
    transport = os.environ.get("TRANSPORT", "unicast").lower()
    if transport not in ("unicast", "broadcast"):
        print(f"[FATAL] TRANSPORT must be 'unicast' or 'broadcast' (got '{transport}')")
        sys.exit(1)

    # Build folder structure: final_experiment/<MODE>/window_<NAME>/
    level_dir = os.path.join(BASE_OUTPUT_DIR, mode)
    ensure_dir(level_dir)
    chown_to_user(BASE_OUTPUT_DIR)

    print(f"\n[CONFIG] mode           : {mode}  ({MODE_DESCRIPTIONS[mode]})")
    print(f"[CONFIG] transport      : {transport}")
    if static_channel:
        print(f"[CONFIG] static channel : ch{static_channel}")
    print(f"[CONFIG] output         : {os.path.abspath(level_dir)}/")
    print(f"[CONFIG] windows        : {[w[0] for w in WINDOWS]}")
    total_minutes = len(WINDOWS) * (TEST_DURATION + INTER_WINDOW_COOLDOWN_S + 30) / 60
    print(f"[CONFIG] est duration   : ~{total_minutes:.0f} min for {len(WINDOWS)} windows")
    print(f"[CONFIG] schedule for this mode:")
    print(describe_schedule(mode))
    print()

    preflight_once()

    results = []
    for idx, (window_name, sim_offset) in enumerate(WINDOWS, 1):
        try:
            ok = run_one_window(
                mode=mode,
                window_idx=idx,
                window_total=len(WINDOWS),
                window_name=window_name,
                sim_offset=sim_offset,
                static_channel=static_channel,
                transport=transport,
                level_dir=level_dir,
            )
            results.append((window_name, ok))
        except Exception as e:
            print(f"[ERROR] window {window_name} crashed: {e}")
            results.append((window_name, False))
            # Try to clean up before next window
            try:
                per_window_cleanup()
            except Exception:
                pass

        # Cooldown before next window (skip after last)
        if idx < len(WINDOWS):
            print(f"\n[COOLDOWN] {INTER_WINDOW_COOLDOWN_S}s before next window ...")
            time.sleep(INTER_WINDOW_COOLDOWN_S)

    # Final summary
    print("\n" + "=" * 70)
    print(f" LEVEL {mode} COMPLETE — summary")
    print("=" * 70)
    for window_name, ok in results:
        print(f"  window_{window_name}  {'OK' if ok else 'FAILED'}")
    print(f"\n[OUT] {os.path.abspath(level_dir)}/")
    chown_to_user(level_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        try:
            per_window_cleanup()
        except Exception:
            pass
        sys.exit(1)
