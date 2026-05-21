# Adaptive Frequency Hopping for EW-Resilient Mesh Networking

A two-node, dual-radio mesh built from Raspberry Pi 5s and consumer
Wi-Fi adaptors that performs time-synchronised frequency hopping to
maintain a video link under active jamming. The link is held together
by a 2-second TOTP-style bucket (HMAC-SHA256 over the shared secret
and current time) that selects the next 2.4 GHz channel; the
~500 ms hardware retune is masked by running two interfaces in a
ping-pong configuration — the active interface routes the stream
while the background interface prep-tunes to the next bucket, and
the kernel route is swapped at the bucket boundary.

Two variants are included:

- **Stateless TOTP** (`src/stateless/`) — pure deterministic hopping
  with no inter-node coordination beyond the shared secret. Used in
  the L1–L5 experimental conditions.
- **Adaptive piggyback** (`src/adaptive/`) — extends the stateless
  design with a piggyback control channel that broadcasts perceived
  channel quality on each bucket. Detected jamming triggers a local
  blacklist (60 s expiry) that the bucket scheduler avoids on the
  next pass. Used in the L6 experimental condition.

## Hardware

- 2× Raspberry Pi 5
- Per node: 1× internal Wi-Fi (`wlan0`), 1× external USB Wi-Fi
  (`wlan1`, Atheros AR9271 in this build) — both must support
  `ibss` mode
- Time sync: Chrony against an upstream NTP source. Sub-bucket
  offset is required by the 2 s bucket width; achieved RMS offset
  is logged per experiment window.

## Repo layout

```
src/
  setup_01.sh, setup_02.sh   per-node IBSS init (Node A / Node B)
  stateless/                  TOTP-only variant (L1-L5)
    artcho.py                 unified node: TOTP scheduling + detection
    analyze.py                offline post-run analysis
    schedule.py               experiment scheduler
    testdash.py               curses TUI dashboard
    testworker.py             test harness driving repeated runs
  adaptive/                   adaptive piggyback variant (L6)
    artcho.py                 adds adaptive blacklist logic
    schedule.py               expanded scheduler
    testdash.py, testdash_l6.py
    analyze.py, testworker.py

data/final_experiment/
  L{1..6}/window_{HH_MM}/     30 measurement windows (6 conditions
                              × 5 times-of-day: 00:00 / 05:00 /
                              11:50 / 17:00 / 23:00)
    config.json               experiment parameters
    test_*_A_tx.csv           sender-side packet log, Node A
    test_*_A_rx.csv           receiver-side packet log, Node A
    test_*_B_tx.csv           sender-side packet log, Node B
    test_*_B_rx.csv           receiver-side packet log, Node B
    test_*_channels.png       channel-occupancy timeline
    test_*_latency.png        per-packet latency
    test_*_pdr.png            packet delivery ratio
    test_*_report.json        numerical summary
    test_*_summary.txt        human-readable summary
    artcho_{A,B}.log          per-node protocol log
    testworker_{A,B}.log      test harness log
    iw_dump_B.txt             interface state snapshot
```

## Reproducing an experiment window

```bash
# Node A and Node B both clone this repo.

# Bring up the dual-radio IBSS mesh
# Node A: IP_ADDR=192.168.200.1/24
# Node B: IP_ADDR=192.168.200.2/24
sudo ./src/setup_01.sh   # on Node A
sudo ./src/setup_02.sh   # on Node B

# Run the unified node process on each side
# Choose the variant matching the condition being reproduced:
#   - L1-L5: src/stateless/
#   - L6:    src/adaptive/
NODE_ID=A sudo -E python3 src/adaptive/artcho.py   # on Node A
NODE_ID=B sudo -E python3 src/adaptive/artcho.py   # on Node B

# Optional: live dashboard
sudo python3 src/adaptive/testdash.py
```

`setup_*.sh`, `artcho.py` and the dashboards require root for IBSS
re-join, routing-table writes, and `tc` qdisc manipulation.

## Experimental data

`data/final_experiment/` is the full 30-window archive used in the
thesis. Each window is self-contained — config, raw per-side packet
logs, pre-rendered plots, JSON/text summaries — so any single result
cited in the thesis traces back to one `window_HH_MM/` directory
without re-running the rig.

The five time-of-day buckets (00:00, 05:00, 11:50, 17:00, 23:00)
were chosen to span both quiet hours and peak-occupancy hours of
the local 2.4 GHz environment.

## License

MIT — see [LICENSE](LICENSE).

## Author

Arturs Mikelsons — Bachelor's thesis, 2026.
