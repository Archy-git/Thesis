#!/usr/bin/env python3
"""
ARTCHO TEST DASHBOARD — L6 (DESYNCHRONISED JAMMING).

L6 is the resilience test for blacklist desynchronisation. Each node
applies a DIFFERENT jamming schedule (SCHEDULE_L6_A on Node A,
SCHEDULE_L6_B on Node B), so:

  - One node may detect ch6 jammed and blacklist it; the other may
    never detect ch6 and not blacklist it.
  - Skip-pattern TOTP then sends them to different "next channels"
    until the gossip layer propagates the BL.
  - The test measures how quickly the protocol reconverges, what
    PDR cost the desync imposes, and how often BL_A != BL_B.

Behaviorally L6 = L3 (BL + detect + dual radio); the only difference
is that testworker.py applies a per-node jamming schedule instead of
the shared one. All 5 simulated time windows are still used.

This script is a thin wrapper around testdash.py — it doesn't
duplicate any code. It just forces the mode to L6 and registers L6
in MODE_DESCRIPTIONS.

Usage:
    sudo python3 testdash_l6.py

Same env knobs apply (TRANSPORT, etc.) as testdash.py.
"""

import os
import sys

# Reuse the full machinery from testdash.py
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import testdash  # noqa: E402
from schedule import describe_l6_schedules  # noqa: E402


# Register L6 description before anything inspects the dict
testdash.MODE_DESCRIPTIONS["L6"] = (
    "Dual radio + BL + detect, PER-NODE jamming "
    "(tests blacklist desync resilience)"
)


# Replace testdash.select_mode so testdash.main() goes straight into L6.
# Python looks up `select_mode` in the module's globals each call, so
# overwriting the attribute on the module redirects the call inside
# testdash.main() to this function.
def _force_l6():
    print("\n" + "=" * 70)
    print(" L6 — DESYNCHRONISED JAMMING RESILIENCE TEST")
    print("=" * 70)
    print(" Each node runs a different jamming schedule.")
    print(" Per-node schedules:")
    print()
    print(describe_l6_schedules())
    print()
    print(" Same 5 simulated time windows as the other levels.")
    print(" Total estimated duration: ~1h 45m.")
    print("=" * 70)
    return "L6"


testdash.select_mode = _force_l6


if __name__ == "__main__":
    try:
        testdash.main()
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        try:
            testdash.per_window_cleanup()
        except Exception:
            pass
        sys.exit(1)
