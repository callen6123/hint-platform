#!/bin/bash
set -euo pipefail

echo "[RESET] Stopping services..."
systemctl stop hint-agent 2>/dev/null || true
systemctl stop hint-setup 2>/dev/null || true

echo "[RESET] Removing provisioning..."
rm -f /etc/hint/provision.json

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