#!/usr/bin/env python3
"""
ARTCHO TEST ANALYZER

Reads test_{mode}_{run_id}_{A|B}_{tx|rx}.csv from <base_dir>, computes all
the metrics we care about for the thesis, and produces:

  - <base_dir>/test_{mode}_{run_id}_report.json   (numeric report)
  - <base_dir>/test_{mode}_{run_id}_pdr.png       (PDR over time, both ways)
  - <base_dir>/test_{mode}_{run_id}_latency.png   (latency CDF)
  - <base_dir>/test_{mode}_{run_id}_channels.png  (channel usage timeline)
  - <base_dir>/test_{mode}_{run_id}_summary.txt   (human-readable summary)

The analyzer reads <base_dir>/config.json (written by testdash) to find the
sim_offset for the run — this lets it correctly translate bucket numbers
(which are offset by sim_offset/2 from test-start) back into 0..1200s on
the time axis, and find the right buckets for jam-onset convergence math.

Usage:  python3 analyze.py --mode L3 --run-id 00_00_20260513_181500 \\
                           --base-dir final_experiment/L3/window_00_00
"""

import csv
import os
import sys
import argparse
import json
import statistics
import re
from collections import defaultdict, Counter

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

from schedule import schedule_for_mode, TEST_DURATION, get_jammed_channels

BUCKET_SECONDS = 2.0
PACKET_RATE_HZ = 20


# ===========================================================================
# CSV loading
# ===========================================================================

def load_csv(path):
    if not os.path.exists(path):
        print(f"[WARN] missing: {path}")
        return []
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def coerce(rows):
    """Cast numeric fields to int/float."""
    out = []
    for r in rows:
        try:
            rr = dict(r)
            rr["wall_ts_ns"] = int(r["wall_ts_ns"])
            rr["test_t_sec"] = float(r["test_t_sec"])
            rr["bucket"]     = int(r["bucket"])
            rr["seq"]        = int(r["seq"])
            if "send_ts_ns" in r and r["send_ts_ns"]:
                rr["send_ts_ns"] = int(r["send_ts_ns"])
            if "latency_ns" in r and r["latency_ns"]:
                rr["latency_ns"] = int(r["latency_ns"])
            out.append(rr)
        except (ValueError, KeyError):
            continue
    return out


# ===========================================================================
# Metric computations
# ===========================================================================

def pdr_overall(tx_rows, rx_rows, peer):
    tx_seqs = {r["seq"] for r in tx_rows}
    rx_seqs = {r["seq"] for r in rx_rows if r.get("src") == peer}
    received = tx_seqs & rx_seqs
    if not tx_seqs:
        return 0.0, 0, 0
    return len(received) / len(tx_seqs) * 100, len(received), len(tx_seqs)


def pdr_per_bucket(tx_rows, rx_rows, peer):
    """Returns list of (bucket, pdr_pct, tx_count, rx_count)."""
    tx_by_b = defaultdict(set)
    for r in tx_rows:
        tx_by_b[r["bucket"]].add(r["seq"])
    rx_by_b = defaultdict(set)
    for r in rx_rows:
        if r.get("src") == peer:
            rx_by_b[r["bucket"]].add(r["seq"])
    all_buckets = sorted(set(tx_by_b.keys()) | set(rx_by_b.keys()))
    out = []
    for b in all_buckets:
        tx_set = tx_by_b.get(b, set())
        rx_set = rx_by_b.get(b, set())
        received = tx_set & rx_set
        if tx_set:
            pdr = len(received) / len(tx_set) * 100
        else:
            pdr = 0.0
        out.append((b, pdr, len(tx_set), len(received)))
    return out


def pdr_per_channel(tx_rows, rx_rows, peer):
    """Group TX'd packets by the channel they were SENT on; compute PDR for each."""
    tx_by_ch = defaultdict(set)
    for r in tx_rows:
        ch = r.get("channel", "?")
        tx_by_ch[ch].add(r["seq"])

    rx_seqs_from_peer = {r["seq"] for r in rx_rows if r.get("src") == peer}

    out = {}
    for ch, tx_set in tx_by_ch.items():
        received = tx_set & rx_seqs_from_peer
        out[ch] = {
            "tx_count": len(tx_set),
            "rx_count": len(received),
            "pdr_pct":  len(received) / len(tx_set) * 100 if tx_set else 0.0,
        }
    return out


def latency_stats(rx_rows, peer):
    lats = [r["latency_ns"] for r in rx_rows
            if r.get("src") == peer and "latency_ns" in r]
    if not lats:
        return None
    lats_sorted = sorted(lats)
    n = len(lats_sorted)
    return {
        "count":     n,
        "mean_ms":   statistics.mean(lats_sorted)   / 1e6,
        "median_ms": statistics.median(lats_sorted) / 1e6,
        "p95_ms":    lats_sorted[min(n-1, int(n*0.95))] / 1e6,
        "p99_ms":    lats_sorted[min(n-1, int(n*0.99))] / 1e6,
        "min_ms":    lats_sorted[0]  / 1e6,
        "max_ms":    lats_sorted[-1] / 1e6,
        "stdev_ms":  (statistics.stdev(lats_sorted) / 1e6) if n > 1 else 0.0,
    }


def jitter(rx_rows, peer):
    """Std-dev of inter-arrival time of consecutive received seqs."""
    by_seq = sorted([(r["seq"], r["wall_ts_ns"]) for r in rx_rows
                     if r.get("src") == peer])
    if len(by_seq) < 2:
        return None
    inter_arr = []
    for i in range(1, len(by_seq)):
        delta = by_seq[i][1] - by_seq[i-1][1]
        inter_arr.append(delta)
    if not inter_arr:
        return None
    return {
        "mean_inter_arrival_ms": statistics.mean(inter_arr) / 1e6,
        "stdev_inter_arrival_ms": (statistics.stdev(inter_arr) / 1e6) if len(inter_arr) > 1 else 0.0,
        "samples": len(inter_arr),
    }


def burst_loss(tx_rows, rx_rows, peer):
    """Distribution of consecutive-lost-packet run lengths."""
    tx_seqs = sorted({r["seq"] for r in tx_rows})
    rx_seqs = {r["seq"] for r in rx_rows if r.get("src") == peer}
    if not tx_seqs:
        return None
    runs = []
    current = 0
    for s in tx_seqs:
        if s not in rx_seqs:
            current += 1
        else:
            if current > 0:
                runs.append(current)
                current = 0
    if current > 0:
        runs.append(current)
    if not runs:
        return {"max_burst": 0, "total_bursts": 0, "histogram": {}}
    hist = Counter(runs)
    return {
        "max_burst":     max(runs),
        "total_bursts":  len(runs),
        "mean_burst":    statistics.mean(runs),
        "median_burst":  statistics.median(runs),
        "histogram":     dict(hist),
    }


def out_of_order(rx_rows, peer):
    by_arrival = [r for r in rx_rows if r.get("src") == peer]
    by_arrival.sort(key=lambda r: r["wall_ts_ns"])
    seqs = [r["seq"] for r in by_arrival]
    ooo = 0
    max_seen = -1
    for s in seqs:
        if s < max_seen:
            ooo += 1
        else:
            max_seen = s
    return ooo


# ---------------------------------------------------------------------------
# Blacklist convergence — parse from artcho log if available
# ---------------------------------------------------------------------------

BL_LINE_RE = re.compile(r"BL=\[([^\]]*)\]")
HOP_LINE_RE = re.compile(r"\[(\d\d:\d\d:\d\d)\]\s+HOP bucket=(\d+).*BL=\[([^\]]*)\]")
JAM_FIRED_RE = re.compile(r"\[\!\! JAMMED\s+\] bucket=(\d+).*?ch(\d+)")
MERGE_RE     = re.compile(r"\[<= MERGED\s+\] ch(\d+)")


def parse_artcho_log(base_dir, node_id):
    """Parse {base_dir}/artcho_{node_id}.log for BL state per bucket."""
    log_path = f"{base_dir}/artcho_{node_id}.log"
    if not os.path.exists(log_path):
        return None

    bl_by_bucket = {}
    jam_fires    = []
    merges       = []

    with open(log_path) as f:
        for line in f:
            m = HOP_LINE_RE.search(line)
            if m:
                bucket = int(m.group(2))
                bl_str = m.group(3).strip()
                bl = set()
                if bl_str:
                    for tok in bl_str.split(","):
                        tok = tok.strip().strip("'").strip('"')
                        if tok:
                            bl.add(tok)
                bl_by_bucket[bucket] = bl
                continue

            m = JAM_FIRED_RE.search(line)
            if m:
                jam_fires.append((int(m.group(1)), m.group(2)))
                continue

            m = MERGE_RE.search(line)
            if m:
                merges.append(m.group(1))

    return {
        "bl_by_bucket": {b: sorted(s, key=lambda x: int(x))
                         for b, s in bl_by_bucket.items()},
        "jam_fires":    jam_fires,
        "merges":       merges,
    }


def convergence_per_event(base_dir, mode, sim_offset):
    """For each schedule transition that ADDS a jammed channel, measure
    bucket-delay until BOTH nodes have that channel in their BL.

    sim_offset (in seconds) shifts the bucket counter; artcho writes
    buckets relative to BUCKET_EPOCH, which is sim_offset seconds before
    TEST_EPOCH. So a schedule event at test_t=60 corresponds to a bucket
    that's `sim_offset/2` higher than the naive (t_sec / BUCKET_SECONDS).
    """
    a_log = parse_artcho_log(base_dir, "A")
    b_log = parse_artcho_log(base_dir, "B")
    if a_log is None:
        return None

    bucket_offset = int(sim_offset / BUCKET_SECONDS)

    transitions = []
    prev = set()
    for start, end, channels in schedule_for_mode(mode):
        cur = set(channels)
        added = cur - prev
        for ch in added:
            transitions.append((start, ch))
        prev = cur

    results = []
    a_bl = a_log["bl_by_bucket"]
    b_bl = b_log["bl_by_bucket"] if b_log else {}

    for t_sec, ch in transitions:
        first_bucket = int(t_sec / BUCKET_SECONDS) + bucket_offset
        a_bucket = None
        for b in sorted(a_bl.keys()):
            if b >= first_bucket and ch in a_bl[b]:
                a_bucket = b
                break
        b_bucket = None
        if b_bl:
            for b in sorted(b_bl.keys()):
                if b >= first_bucket and ch in b_bl[b]:
                    b_bucket = b
                    break

        results.append({
            "event_time_s":         t_sec,
            "channel":              ch,
            "first_jammed_bucket":  first_bucket,
            "a_detected_bucket":    a_bucket,
            "b_detected_bucket":    b_bucket,
            "a_convergence_buckets": (a_bucket - first_bucket) if a_bucket else None,
            "b_convergence_buckets": (b_bucket - first_bucket) if b_bucket else None,
        })
    return results


def bl_agreement(base_dir):
    """Per-bucket: do A and B agree on the BL? Returns (bucket, agree?) list
    and the fraction of buckets in agreement."""
    a_log = parse_artcho_log(base_dir, "A")
    b_log = parse_artcho_log(base_dir, "B")
    if not a_log or not b_log:
        return None
    a_bl = a_log["bl_by_bucket"]
    b_bl = b_log["bl_by_bucket"]
    common = sorted(set(a_bl.keys()) & set(b_bl.keys()))
    if not common:
        return None
    agree_count = 0
    rows = []
    for b in common:
        agree = (set(a_bl[b]) == set(b_bl[b]))
        rows.append((b, agree, a_bl[b], b_bl[b]))
        if agree:
            agree_count += 1
    return {
        "fraction_agree": agree_count / len(common) * 100,
        "samples":        len(common),
        "rows":           rows,
    }


# ===========================================================================
# Plots
# ===========================================================================

def plot_pdr_over_time(a_tx, a_rx, b_tx, b_rx, mode, run_id, sim_offset, out_path):
    if not HAVE_MPL:
        return
    pdr_ab = pdr_per_bucket(a_tx, b_rx, "A") if a_tx and b_rx else []
    pdr_ba = pdr_per_bucket(b_tx, a_rx, "B") if b_tx and a_rx else []

    fig, ax = plt.subplots(figsize=(14, 5))
    if pdr_ab:
        xs = [b * BUCKET_SECONDS - sim_offset for b, _, _, _ in pdr_ab]
        ys = [p for _, p, _, _ in pdr_ab]
        ax.plot(xs, ys, label="A -> B", linewidth=1.2, color="tab:blue")
    if pdr_ba:
        xs = [b * BUCKET_SECONDS - sim_offset for b, _, _, _ in pdr_ba]
        ys = [p for _, p, _, _ in pdr_ba]
        ax.plot(xs, ys, label="B -> A", linewidth=1.2, color="tab:orange")

    # Shade jamming regimes
    for start, end, channels in schedule_for_mode(mode):
        if channels:
            alpha = min(0.45, 0.15 + 0.10 * len(channels))
            ax.axvspan(start, end, alpha=alpha, color="red",
                       label=f"jam ch{','.join(channels)}" if start < 600 else None)

    ax.set_xlabel("Test time (s)")
    ax.set_ylabel("PDR (%) per 2s bucket")
    ax.set_title(f"PDR over time — {mode} (run {run_id})")
    ax.set_ylim(-2, 102)
    ax.set_xlim(0, TEST_DURATION)
    ax.grid(True, alpha=0.3)

    # Deduplicate legend
    handles, labels = ax.get_legend_handles_labels()
    seen = set()
    dedup_h, dedup_l = [], []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l)
            dedup_h.append(h)
            dedup_l.append(l)
    ax.legend(dedup_h, dedup_l, loc="lower left", fontsize=9)

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()


def plot_latency_cdf(a_rx, b_rx, mode, run_id, out_path):
    if not HAVE_MPL:
        return
    fig, ax = plt.subplots(figsize=(9, 5))

    for rx_rows, peer, color, label in (
        (a_rx, "B", "tab:blue",   "B -> A"),
        (b_rx, "A", "tab:orange", "A -> B"),
    ):
        lats = sorted([r["latency_ns"] for r in rx_rows
                       if r.get("src") == peer and "latency_ns" in r])
        if not lats:
            continue
        xs = [l / 1e6 for l in lats]
        ys = [i / len(lats) for i in range(1, len(lats) + 1)]
        ax.plot(xs, ys, label=f"{label} (n={len(lats)})", color=color, linewidth=1.2)

    ax.set_xscale("log")
    ax.set_xlabel("One-way latency (ms, log scale)")
    ax.set_ylabel("CDF")
    ax.set_title(f"Latency CDF — {mode} (run {run_id})")
    ax.grid(True, alpha=0.3, which="both")
    ax.legend(loc="lower right", fontsize=9)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()


def plot_channel_usage(a_tx, mode, run_id, out_path):
    if not HAVE_MPL or not a_tx:
        return
    # Each TX point: x=test_t, y=channel
    fig, ax = plt.subplots(figsize=(14, 4))

    # Shade jamming regimes by channel
    for start, end, channels in schedule_for_mode(mode):
        for ch in channels:
            try:
                y = int(ch)
                ax.axvspan(start, end, ymin=(y-2.5)/10, ymax=(y-1.5)/10,
                           alpha=0.35, color="red")
            except ValueError:
                pass

    xs = [r["test_t_sec"] for r in a_tx]
    ys = []
    for r in a_tx:
        try:
            ys.append(int(r.get("channel", "0")))
        except ValueError:
            ys.append(0)
    ax.scatter(xs, ys, s=2, color="tab:blue", alpha=0.5, label="A active channel")

    ax.set_xlabel("Test time (s)")
    ax.set_ylabel("Channel")
    ax.set_yticks(list(range(3, 12)))
    ax.set_ylim(2.5, 11.5)
    ax.set_xlim(0, TEST_DURATION)
    ax.set_title(f"Channel usage timeline (Node A) — {mode} (run {run_id})")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()


# ===========================================================================
# Report assembly
# ===========================================================================

def fmt_latency(lat):
    if not lat:
        return "(no data)"
    return (f"mean={lat['mean_ms']:.2f}ms  median={lat['median_ms']:.2f}ms  "
            f"p95={lat['p95_ms']:.2f}ms  p99={lat['p99_ms']:.2f}ms  "
            f"max={lat['max_ms']:.2f}ms  stdev={lat['stdev_ms']:.2f}ms  n={lat['count']}")


def build_report(mode, run_id, base_dir):
    # Read config sidecar (written by testdash per window) for sim_offset
    sim_offset = 0
    config_path = os.path.join(base_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            sim_offset = float(cfg.get("sim_offset_s", 0))
        except Exception as e:
            print(f"[WARN] couldn't read {config_path}: {e}")

    a_tx = coerce(load_csv(f"{base_dir}/test_{mode}_{run_id}_A_tx.csv"))
    a_rx = coerce(load_csv(f"{base_dir}/test_{mode}_{run_id}_A_rx.csv"))
    b_tx = coerce(load_csv(f"{base_dir}/test_{mode}_{run_id}_B_tx.csv"))
    b_rx = coerce(load_csv(f"{base_dir}/test_{mode}_{run_id}_B_rx.csv"))

    print(f"\n=== ANALYZE: {mode} run {run_id} ===")
    print(f"  base_dir   : {base_dir}")
    print(f"  sim_offset : {sim_offset:.0f}s")
    print(f"  A_tx rows  : {len(a_tx)}")
    print(f"  A_rx rows  : {len(a_rx)}")
    print(f"  B_tx rows  : {len(b_tx)}")
    print(f"  B_rx rows  : {len(b_rx)}\n")

    report = {"mode": mode, "run_id": run_id, "base_dir": base_dir,
              "sim_offset_s": sim_offset}

    # PDR overall
    if a_tx and b_rx:
        pdr, recv, sent = pdr_overall(a_tx, b_rx, "A")
        report["pdr_ab_overall"] = pdr
        report["pdr_ab_recv"] = recv
        report["pdr_ab_sent"] = sent
        print(f"PDR (A -> B): {pdr:6.2f}%  ({recv}/{sent})")
    if b_tx and a_rx:
        pdr, recv, sent = pdr_overall(b_tx, a_rx, "B")
        report["pdr_ba_overall"] = pdr
        report["pdr_ba_recv"] = recv
        report["pdr_ba_sent"] = sent
        print(f"PDR (B -> A): {pdr:6.2f}%  ({recv}/{sent})")

    # Latency
    lat_ba = latency_stats(a_rx, "B") if a_rx else None
    lat_ab = latency_stats(b_rx, "A") if b_rx else None
    report["latency_ba"] = lat_ba
    report["latency_ab"] = lat_ab
    print(f"\nLatency (B -> A): {fmt_latency(lat_ba)}")
    print(f"Latency (A -> B): {fmt_latency(lat_ab)}")

    # Jitter
    jit_ba = jitter(a_rx, "B") if a_rx else None
    jit_ab = jitter(b_rx, "A") if b_rx else None
    report["jitter_ba"] = jit_ba
    report["jitter_ab"] = jit_ab
    if jit_ba:
        print(f"\nJitter (B -> A): inter_arr mean={jit_ba['mean_inter_arrival_ms']:.2f}ms  "
              f"stdev={jit_ba['stdev_inter_arrival_ms']:.2f}ms")
    if jit_ab:
        print(f"Jitter (A -> B): inter_arr mean={jit_ab['mean_inter_arrival_ms']:.2f}ms  "
              f"stdev={jit_ab['stdev_inter_arrival_ms']:.2f}ms")

    # Burst loss
    burst_ab = burst_loss(a_tx, b_rx, "A") if a_tx and b_rx else None
    burst_ba = burst_loss(b_tx, a_rx, "B") if b_tx and a_rx else None
    report["burst_ab"] = burst_ab
    report["burst_ba"] = burst_ba
    if burst_ab:
        print(f"\nBurst loss (A -> B): max={burst_ab['max_burst']} packets  "
              f"({burst_ab['max_burst'] * 50:.0f}ms)  "
              f"total_bursts={burst_ab['total_bursts']}")
    if burst_ba:
        print(f"Burst loss (B -> A): max={burst_ba['max_burst']} packets  "
              f"({burst_ba['max_burst'] * 50:.0f}ms)  "
              f"total_bursts={burst_ba['total_bursts']}")

    # Out-of-order
    if a_rx:
        ooo = out_of_order(a_rx, "B")
        report["ooo_ba"] = ooo
        print(f"\nOut-of-order (B -> A): {ooo}")
    if b_rx:
        ooo = out_of_order(b_rx, "A")
        report["ooo_ab"] = ooo
        print(f"Out-of-order (A -> B): {ooo}")

    # Per-channel PDR
    if a_tx and b_rx:
        per_ch = pdr_per_channel(a_tx, b_rx, "A")
        report["pdr_per_channel_ab"] = per_ch
        print(f"\nPer-channel PDR (A -> B):")
        for ch in sorted(per_ch.keys(), key=lambda x: (len(x), x)):
            d = per_ch[ch]
            print(f"  ch{ch:>2}: {d['pdr_pct']:6.2f}%  ({d['rx_count']:>5}/{d['tx_count']:>5})")

    # Per-bucket PDR — store but only print summary
    if a_tx and b_rx:
        per_b = pdr_per_bucket(a_tx, b_rx, "A")
        report["pdr_per_bucket_ab"] = [
            {"bucket": b, "pdr_pct": p, "tx": t, "rx": r}
            for b, p, t, r in per_b
        ]
        # Print regime averages
        print(f"\nPer-regime PDR averages (A -> B):")
        for start, end, channels in schedule_for_mode(mode):
            # b * BUCKET_SECONDS is in BUCKET_EPOCH frame; subtract sim_offset
            # to get back to test-relative seconds for the schedule comparison.
            bucks = [(b, p) for b, p, _, _ in per_b
                     if start <= b * BUCKET_SECONDS - sim_offset < end]
            if bucks:
                avg = sum(p for _, p in bucks) / len(bucks)
                tag = f"jam {channels}" if channels else "clean"
                print(f"  [{start:>4}s-{end:>4}s] {tag:>20s}: avg={avg:6.2f}%  ({len(bucks)} buckets)")

    # Convergence
    conv = convergence_per_event(base_dir, mode, sim_offset)
    if conv:
        report["convergence_events"] = conv
        print(f"\nConvergence (per jam-onset event):")
        for ev in conv:
            a_str = (f"+{ev['a_convergence_buckets']} buckets" if ev['a_convergence_buckets'] is not None
                     else "never")
            b_str = (f"+{ev['b_convergence_buckets']} buckets" if ev['b_convergence_buckets'] is not None
                     else "never (log unavailable)")
            print(f"  t={ev['event_time_s']}s ch{ev['channel']}: "
                  f"A={a_str}, B={b_str}")

    # BL agreement
    bl_agree = bl_agreement(base_dir)
    if bl_agree:
        report["bl_agreement"] = {
            "fraction_agree": bl_agree["fraction_agree"],
            "samples":        bl_agree["samples"],
        }
        print(f"\nBL agreement (A vs B): {bl_agree['fraction_agree']:.2f}% over {bl_agree['samples']} common buckets")

    # ----- Plots ----------------------------------------------------------
    if HAVE_MPL:
        pdr_path = f"{base_dir}/test_{mode}_{run_id}_pdr.png"
        plot_pdr_over_time(a_tx, a_rx, b_tx, b_rx, mode, run_id, sim_offset, pdr_path)
        print(f"\n[PLOT] {pdr_path}")

        lat_path = f"{base_dir}/test_{mode}_{run_id}_latency.png"
        plot_latency_cdf(a_rx, b_rx, mode, run_id, lat_path)
        print(f"[PLOT] {lat_path}")

        ch_path = f"{base_dir}/test_{mode}_{run_id}_channels.png"
        plot_channel_usage(a_tx, mode, run_id, ch_path)
        print(f"[PLOT] {ch_path}")
    else:
        print("\n[PLOT] matplotlib not installed — skipping plots. (pip install matplotlib)")

    # ----- Write report ---------------------------------------------------
    report_path = f"{base_dir}/test_{mode}_{run_id}_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n[REPORT] {report_path}")

    # ----- Human summary --------------------------------------------------
    summary_path = f"{base_dir}/test_{mode}_{run_id}_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"ARTCHO TEST SUMMARY — {mode} (run {run_id})\n")
        f.write("=" * 60 + "\n\n")
        if "pdr_ab_overall" in report:
            f.write(f"PDR (A -> B): {report['pdr_ab_overall']:.2f}%  "
                    f"({report['pdr_ab_recv']}/{report['pdr_ab_sent']})\n")
        if "pdr_ba_overall" in report:
            f.write(f"PDR (B -> A): {report['pdr_ba_overall']:.2f}%  "
                    f"({report['pdr_ba_recv']}/{report['pdr_ba_sent']})\n")
        if lat_ba:
            f.write(f"\nLatency (B -> A): {fmt_latency(lat_ba)}\n")
        if lat_ab:
            f.write(f"Latency (A -> B): {fmt_latency(lat_ab)}\n")
        if conv:
            f.write("\nConvergence (buckets after jam onset):\n")
            for ev in conv:
                a_str = str(ev['a_convergence_buckets']) if ev['a_convergence_buckets'] is not None else "?"
                b_str = str(ev['b_convergence_buckets']) if ev['b_convergence_buckets'] is not None else "?"
                f.write(f"  ch{ev['channel']} (t={ev['event_time_s']}s): A=+{a_str}, B=+{b_str}\n")
        if bl_agree:
            f.write(f"\nBL agreement: {bl_agree['fraction_agree']:.2f}% over {bl_agree['samples']} buckets\n")
    print(f"[SUMMARY] {summary_path}")


# ===========================================================================
# Main
# ===========================================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True, choices=["L1", "L2", "L3", "L4", "L5"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--base-dir", default="/tmp",
                   help="Directory holding the CSVs/logs and where outputs go (default /tmp)")
    args = p.parse_args()
    build_report(args.mode, args.run_id, args.base_dir)


if __name__ == "__main__":
    main()
