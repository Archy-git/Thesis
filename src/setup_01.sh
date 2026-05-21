#!/bin/bash
# ARTCHO Mesh — Dual-Radio IBSS Initialization
# NOTE: Change IP_ADDR to .2 on Node B before running.

NETWORK="TacticalMesh"
BSSID="02:11:22:33:44:55"
IP_ADDR="192.168.200.1/24"   # Node A: .1   |   Node B: .2

echo "[*] Clearing stale piggyback blacklist state..."
sudo rm -f /tmp/blacklist.json

echo "[*] Disabling Reverse Path Filtering for dual-homed IPs..."
sudo sysctl -w net.ipv4.conf.all.rp_filter=0
sudo sysctl -w net.ipv4.conf.wlan0.rp_filter=0
sudo sysctl -w net.ipv4.conf.wlan1.rp_filter=0

echo "[*] Initializing wlan0 (Internal)..."
sudo ip addr flush dev wlan0
sudo ip link set wlan0 down
sudo iw dev wlan0 set type ibss
sudo ip link set wlan0 up
sudo iw dev wlan0 ibss join $NETWORK 2412 fixed-freq $BSSID
sudo ip addr add $IP_ADDR dev wlan0
sudo ip link set wlan0 txqueuelen 5

echo "[*] Initializing wlan1 (External AR9271)..."
sudo ip addr flush dev wlan1
sudo ip link set wlan1 down
sudo iw dev wlan1 set type ibss
sudo ip link set wlan1 up
sudo iw dev wlan1 ibss join $NETWORK 2412 fixed-freq $BSSID
sudo ip addr add $IP_ADDR dev wlan1
sudo ip link set wlan1 txqueuelen 5

echo "[*] Setting initial route through wlan0..."
sudo ip route replace 192.168.200.0/24 dev wlan0

echo "[*] Restarting Chrony..."
sudo systemctl restart chrony

echo "[*] Mesh initialized."
echo "    1. Start TOTPpingpong.py  (performs cold-start tune)"
echo "    2. Start piggyback.py     (gossip + jam-detection agent)"
echo "    3. Start jammerv5.py      (EW dashboard + blacklist intelligence)"
echo "    4. Start GStreamer pipeline."