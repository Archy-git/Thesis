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


def schedule_for_mode(mode):
    """L1/L2/L3 use the jamming schedule; L4/L5 are clean."""
    if mode in ("L4", "L5"):
        return SCHEDULE_CLEAN
    return SCHEDULE_JAMMED


def get_jammed_channels(test_t, mode="L3"):
    """Return list of jammed channel strings at test-relative time `test_t`."""
    for start, end, channels in schedule_for_mode(mode):
        if start <= test_t < end:
            return list(channels)
    return []


def describe_schedule(mode="L3"):
    """Pretty-print the schedule used for `mode`, for the dashboard header."""
    out = []
    for start, end, channels in schedule_for_mode(mode):
        mm_s = f"{start//60:>2d}:{start%60:02d}"
        mm_e = f"{end//60:>2d}:{end%60:02d}"
        chs = ",".join(channels) if channels else "clean"
        out.append(f"  {mm_s}-{mm_e}  {chs}")
    return "\n".join(out)
