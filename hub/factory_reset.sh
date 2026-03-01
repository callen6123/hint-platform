#!/bin/bash
set -euo pipefail

echo "[RESET] Stopping hub services..."
systemctl stop hint-hub-pair 2>/dev/null || true
systemctl stop hint-device-watch 2>/dev/null || true
systemctl stop hint-hub-setup 2>/dev/null || true

echo "[RESET] Removing hub provisioning..."
rm -f /etc/hint/hub_provision.json

echo "[RESET] Removing hub runtime state..."
rm -f /var/lib/hint/devices.json
rm -f /var/lib/hint/ui_config.json
rm -f /var/lib/hint/ui_users.json
rm -f /var/lib/hint/logs/hub_cmd.log
rm -rf /var/lib/hint/audio_cache/*
rm -rf /var/lib/hint/audio_pinned/*

echo "[RESET] Soft-resetting hub secrets (/etc/hint/hub.env)..."
if [ -f /etc/hint/hub.env ]; then
  cp /etc/hint/hub.env "/etc/hint/hub.env.bak.$(date +%s)" 2>/dev/null || true
fi
cat > /etc/hint/hub.env <<'EOF'
# Hint Hub environment (factory reset)
# Fill these during provisioning before demo/shipping.
ELEVENLABS_API_KEY=
HINT_UI_USER=
HINT_UI_PASS=
HINT_UI_SECRET=
HINT_DASH_API_KEY=
EOF
chown root:root /etc/hint/hub.env
chmod 600 /etc/hint/hub.env

echo "[RESET] Removing systemd override credentials (if present)..."
if [ -f /etc/systemd/system/hint-hub-pair.service.d/override.conf ]; then
  rm -f /etc/systemd/system/hint-hub-pair.service.d/override.conf
fi
# If the drop-in dir is empty after removing the file, remove it too.
if [ -d /etc/systemd/system/hint-hub-pair.service.d ]; then
  rmdir /etc/systemd/system/hint-hub-pair.service.d 2>/dev/null || true
fi
systemctl daemon-reload || true

echo "[RESET] Resetting Ethernet to DHCP (NetworkManager)..."
if command -v nmcli >/dev/null 2>&1; then
  nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="ethernet"{print $1}' | while read -r name; do
    [ -n "$name" ] || continue
    nmcli connection modify "$name" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" ipv4.ignore-auto-dns no ipv6.method auto || true
    nmcli connection down "$name" >/dev/null 2>&1 || true
    nmcli connection up "$name" >/dev/null 2>&1 || true
  done
fi

echo "[RESET] Removing any dhcpcd static eth0/wlan0 config (if present)..."
if [ -f /etc/dhcpcd.conf ]; then
  if grep -qE '^\s*interface\s+(eth0|wlan0)\b' /etc/dhcpcd.conf; then
    cp /etc/dhcpcd.conf "/etc/dhcpcd.conf.bak.$(date +%s)" 2>/dev/null || true
    awk '
      BEGIN{skip=0}
      /^\s*interface\s+(eth0|wlan0)\b/{skip=1; print "# [RESET] removed interface config"; next}
      skip==1 && /^\s*interface\s+/{skip=0}
      skip==0{print}
    ' /etc/dhcpcd.conf > /tmp/dhcpcd.conf && mv /tmp/dhcpcd.conf /etc/dhcpcd.conf
  fi
fi

echo "[RESET] Clearing wpa_supplicant Wi-Fi networks (if present)..."
if [ -f /etc/wpa_supplicant/wpa_supplicant.conf ]; then
  cp /etc/wpa_supplicant/wpa_supplicant.conf "/etc/wpa_supplicant/wpa_supplicant.conf.bak.$(date +%s)" 2>/dev/null || true
  # Remove all network blocks, keep global directives.
  awk '
    BEGIN{inblk=0}
    /^\s*network=\s*\{/{inblk=1; next}
    inblk==1 && /^\s*\}/{inblk=0; next}
    inblk==0{print}
  ' /etc/wpa_supplicant/wpa_supplicant.conf > /tmp/wpa_supplicant.conf && mv /tmp/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf
fi
chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null || true
chown root:root /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null || true

echo "[RESET] Clearing NetworkManager Wi-Fi connections (UUID-based)..."
if command -v nmcli >/dev/null 2>&1; then
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
else
  echo "[RESET] nmcli not found; skipping NetworkManager Wi-Fi clearing."
fi

echo "[RESET] Re-enabling hub setup service..."
systemctl enable hint-hub-setup >/dev/null 2>&1 || true
systemctl start hint-hub-setup >/dev/null 2>&1 || true

echo "[RESET] Rebooting..."
reboot