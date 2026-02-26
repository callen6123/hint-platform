#!/bin/bash
set -euo pipefail

echo "[RESET] Stopping hub services..."
systemctl stop hint-hub-pair 2>/dev/null || true
systemctl stop hint-device-watch 2>/dev/null || true
systemctl stop hint-hub-setup 2>/dev/null || true

echo "[RESET] Removing hub provisioning..."
rm -f /etc/hint/hub_provision.json

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

echo "[RESET] Re-enabling hub setup service..."
systemctl enable hint-hub-setup >/dev/null 2>&1 || true
systemctl start hint-hub-setup >/dev/null 2>&1 || true

echo "[RESET] Rebooting..."
reboot