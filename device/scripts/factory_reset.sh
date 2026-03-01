#!/bin/bash
set -euo pipefail

echo "[RESET] Stopping services..."
systemctl stop hint-agent 2>/dev/null || true
systemctl stop hint-setup 2>/dev/null || true

echo "[RESET] Removing provisioning..."
rm -f /etc/hint/provision.json
echo "[RESET] Removing device ID..."
rm -f /etc/hint/device_id

echo "[RESET] Resetting Ethernet to DHCP (NetworkManager)..."
if command -v nmcli >/dev/null 2>&1; then
  nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="ethernet"{print $1}' | while read -r name; do
    [ -n "$name" ] || continue
    nmcli connection modify "$name" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" ipv4.ignore-auto-dns no ipv6.method auto || true
    nmcli connection down "$name" >/dev/null 2>&1 || true
    nmcli connection up "$name" >/dev/null 2>&1 || true
  done
fi

echo "[RESET] Removing any dhcpcd static eth0 config (if present)..."
if [ -f /etc/dhcpcd.conf ]; then
  if grep -qE '^\s*interface\s+eth0\b' /etc/dhcpcd.conf; then
    cp /etc/dhcpcd.conf "/etc/dhcpcd.conf.bak.$(date +%s)" 2>/dev/null || true
    awk '
      BEGIN{skip=0}
      /^\s*interface\s+eth0\b/{skip=1; print "# [RESET] removed eth0 config"; next}
      skip==1 && /^\s*interface\s+/{skip=0}
      skip==0{print}
    ' /etc/dhcpcd.conf > /tmp/dhcpcd.conf && mv /tmp/dhcpcd.conf /etc/dhcpcd.conf
  fi
fi

echo "[RESET] Clearing NetworkManager Wi-Fi connections (UUID-based)..."
nmcli -t -f UUID,TYPE connection show | awk -F: '$2=="wifi"{print $1}' | while read -r uuid; do
  [ -n "$uuid" ] || continue
  nmcli connection delete uuid "$uuid" || true
done

echo "[RESET] Removing any persisted Wi-Fi profiles from disk..."
for f in /etc/NetworkManager/system-connections/*.nmconnection; do
  [ -e "$f" ] || continue
  if grep -q '^type=wifi' "$f"; then
    rm -f "$f"
  fi
done

echo "[RESET] Restarting NetworkManager..."
systemctl restart NetworkManager

echo "[RESET] Re-enabling setup service..."
systemctl enable hint-setup >/dev/null 2>&1 || true
systemctl start hint-setup >/dev/null 2>&1 || true

echo "[RESET] Rebooting..."
reboot