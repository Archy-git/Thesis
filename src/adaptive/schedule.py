# ===========================================================================
# ARTCHO TEST SCHEDULE
# ===========================================================================
# Shared between testdash (Node A) and testworker (both nodes). Both read
# this file. SCHEDULE_JAMMED is used by L1/L2/L3; SCHEDULE_CLEAN by L4/L5.
# ===========================================================================

# 20-minute jamming pattern for L1, L2, L3.
# (start_seconds, end_seconds, [jammed_channel_strings])
SCHEDULE_JAMMED = [
    (   0,   60, []),
    (  60,  120, ["6"]),
    ( 120,  180, ["6"]),
    ( 180,  240, ["6", "7"]),
    ( 240,  300, ["6", "7"]),
    ( 300,  360, ["7"]),
    ( 360,  420, ["7"]),
    ( 420,  480, ["5", "7", "8"]),
    ( 480,  540, ["5", "7", "8"]),
    ( 540,  600, []),
    ( 600,  660, ["3"]),
    ( 660,  720, ["3", "4"]),
    ( 720,  780, ["4", "5", "6"]),
    ( 780,  840, ["4", "5", "6", "7"]),
    ( 840,  900, ["7", "8", "9"]),
    ( 900,  960, ["7", "8", "9", "10"]),
    ( 960, 1020, ["10", "11"]),
    (1020, 1080, ["3", "6", "9", "11"]),
    (1080, 1140, ["3", "6", "9", "11"]),
    (1140, 1200, []),
]

# 20-minute clean schedule for L4/L5 — no jamming, just duration.
SCHEDULE_CLEAN = [
    (0, 1200, []),
]

# 20-minute schedules for L6 (desynchronised jamming).
# Each node sees a different jamming pattern. Mix of:
#  - sync intervals (both same — baseline behavior)
#  - 1-channel shifts (A: ch6 ; B: ch7 — moderate asymmetry)
#  - partial overlap (A: ch6,7 ; B: ch7,8 — one shared, one not)
#  - one-sided (only one node detects jamming — worst case for BL desync)
#  - clean intervals (both unjammed — recovery windows)
# Designed so neither node has a clearly easier or harder schedule overall.
SCHEDULE_L6_A = [
    (   0,   60, []),                       # clean
    (  60,  120, ["6"]),                    # sync
    ( 120,  180, ["6"]),                    # 1-ch shift (B: 7)
    ( 180,  240, ["6", "7"]),               # partial overlap (B: 7,8)
    ( 240,  300, ["6"]),                    # A-only (B: clean)
    ( 300,  360, []),                       # B-only (A: clean)
    ( 360,  420, ["7"]),                    # sync
    ( 420,  480, ["5", "7", "8"]),          # sync
    ( 480,  540, ["5", "7", "8"]),          # partial overlap (B: 6,7,9)
    ( 540,  600, []),                       # clean
    ( 600,  660, ["3"]),                    # sync
    ( 660,  720, ["3", "4"]),               # 1-ch shift (B: 3,5)
    ( 720,  780, ["4", "5", "6"]),          # sync
    ( 780,  840, ["4", "5", "6", "7"]),     # 1-ch shift (B: 5,6,7,8)
    ( 840,  900, ["7", "8", "9"]),          # A sees +1 (B: 7,8)
    ( 900,  960, ["7", "8", "9", "10"]),    # shift (B: 8,9,10,11)
    ( 960, 1020, ["10", "11"]),             # sync
    (1020, 1080, ["3", "6", "9", "11"]),    # sync
    (1080, 1140, ["3", "6", "9", "11"]),    # very different (B: 4,7,10,11)
    (1140, 1200, []),                       # clean
]

SCHEDULE_L6_B = [
    (   0,   60, []),                       # clean
    (  60,  120, ["6"]),                    # sync
    ( 120,  180, ["7"]),                    # 1-ch shift (A: 6)
    ( 180,  240, ["7", "8"]),               # partial overlap (A: 6,7)
    ( 240,  300, []),                       # A-only
    ( 300,  360, ["7"]),                    # B-only (A: clean)
    ( 360,  420, ["7"]),                    # sync
    ( 420,  480, ["5", "7", "8"]),          # sync
    ( 480,  540, ["6", "7", "9"]),          # partial overlap (A: 5,7,8)
    ( 540,  600, []),                       # clean
    ( 600,  660, ["3"]),                    # sync
    ( 660,  720, ["3", "5"]),               # 1-ch shift (A: 3,4)
    ( 720,  780, ["4", "5", "6"]),          # sync
    ( 780,  840, ["5", "6", "7", "8"]),     # 1-ch shift (A: 4,5,6,7)
    ( 840,  900, ["7", "8"]),               # A sees +1
    ( 900,  960, ["8", "9", "10", "11"]),   # shift (A: 7,8,9,10)
    ( 960, 1020, ["10", "11"]),             # sync
    (1020, 1080, ["3", "6", "9", "11"]),    # sync
    (1080, 1140, ["4", "7", "10", "11"]),   # very different (A: 3,6,9,11)
    (1140, 1200, []),                       # clean
]

TEST_DURATION = 1200  # 20 minutes


# Five simulated time windows. Each = (folder_name, seconds_since_midnight).
# Used to shift the TOTP bucket counter so each window produces an
# independent channel-hopping sequence. Same window across L1..L5 gives
# byte-identical hop sequences -> fair comparison. Different windows ->
# 5 independent samples for variance.
WINDOWS = [
    ("00_00",  0),
    ("05_00",  5 * 3600),
    ("11_50",  11 * 3600 + 50 * 60),
    ("17_00",  17 * 3600),
    ("23_00",  23 * 3600),
]


def schedule_for_mode(mode, node_id=None):
    """L1/L2/L3 -> jammed; L4/L5 -> clean; L6 -> per-node (A vs B)."""
    if mode == "L6":
        return SCHEDULE_L6_B if node_id == "B" else SCHEDULE_L6_A
    if mode in ("L4", "L5"):
        return SCHEDULE_CLEAN
    return SCHEDULE_JAMMED


def get_jammed_channels(test_t, mode="L3", node_id=None):
    """Return list of jammed channel strings at test-relative time `test_t`."""
    for start, end, channels in schedule_for_mode(mode, node_id):
        if start <= test_t < end:
            return list(channels)
    return []


def describe_schedule(mode="L3", node_id=None):
    """Pretty-print the schedule used for `mode`, for the dashboard header."""
    out = []
    for start, end, channels in schedule_for_mode(mode, node_id):
        mm_s = f"{start//60:>2d}:{start%60:02d}"
        mm_e = f"{end//60:>2d}:{end%60:02d}"
        chs = ",".join(channels) if channels else "clean"
        out.append(f"  {mm_s}-{mm_e}  {chs}")
    return "\n".join(out)


def describe_l6_schedules():
    """Print L6's per-node schedules side-by-side, marking diffs."""
    out = [f"  {'time':>11}   {'A jams':<22}  {'B jams':<22}  diff"]
    out.append(f"  {'-'*11}   {'-'*22}  {'-'*22}  ----")
    for (sa, ea, ca), (_, _, cb) in zip(SCHEDULE_L6_A, SCHEDULE_L6_B):
        mm_s = f"{sa//60:>2d}:{sa%60:02d}"
        mm_e = f"{ea//60:>2d}:{ea%60:02d}"
        a_str = ",".join(ca) if ca else "clean"
        b_str = ",".join(cb) if cb else "clean"
        diff = "DIFF" if set(ca) != set(cb) else "same"
        out.append(f"  {mm_s}-{mm_e}   {a_str:<22}  {b_str:<22}  {diff}")
    return "\n".join(out)
