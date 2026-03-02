import json
import os
import shutil
import subprocess
import time
import uuid
import requests
from flask import Flask, request

PROVISION_PATH = "/etc/hint/provision.json"
DEVICE_ID_PATH = "/etc/hint/device_id"

# Hub pairing endpoint (mDNS)
HUB_PAIR_URL = "http://hinthub.local:5000/pair/complete"

app = Flask(__name__)

HTML_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Hint Device Setup</title>
  </head>
  <body style=\"font-family: sans-serif; max-width: 520px; margin: 40px auto;\">
    <h2>Hint Device Setup</h2>

    <p>{connection_note}</p>

    <form method=\"post\" action=\"/submit\">
      {wifi_fields}

      <label>Pairing Code</label><br>
      <input name=\"pair_code\" style=\"width:100%;padding:10px\" required><br><br>

      <button type=\"submit\" style=\"padding:12px 18px\">Connect & Pair</button>
    </form>
  </body>
</html>
"""

WIFI_FIELDS_HTML = """
<label>Venue Wi-Fi SSID</label><br>
<input name=\"ssid\" style=\"width:100%;padding:10px\" required><br><br>
<label>Venue Wi-Fi Password</label><br>
<input name=\"psk\" type=\"password\" style=\"width:100%;padding:10px\" required><br><br>
"""


def render_index_html(wired: bool) -> str:
    if wired:
        note = (
            "Ethernet detected (DHCP active). You can pair this device without entering Wi-Fi. "
            "If you want the device to use Wi-Fi instead, unplug Ethernet and refresh."
        )
        wifi_fields = ""
    else:
        note = (
            "Connect your phone/laptop to Wi-Fi <b>HintDevice-SETUP</b> (password <b>hintsetup123</b>). "
            "Your phone may open the setup page automatically; if not, open your browser and visit the gateway IP "
            "(often <b>10.42.0.1</b>)."
        )
        wifi_fields = WIFI_FIELDS_HTML

    return HTML_TEMPLATE.format(connection_note=note, wifi_fields=wifi_fields)

def sh(cmd: list[str]) -> None:
    subprocess.check_call(cmd)

def enable_agent_service() -> None:
    try:
        sh(["sudo", "systemctl", "daemon-reload"])
        sh(["sudo", "systemctl", "enable", "hint-agent.service"])
        sh(["sudo", "systemctl", "start", "hint-agent.service"])
    except Exception as e:
        raise RuntimeError(f"Failed to enable agent service: {e}")

HOTSPOT_CONN_NAME = "hint-setup"
HOTSPOT_SSID = "HintDevice-SETUP"
HOTSPOT_PSK = "hintsetup123"


def nmcli(*args: str) -> str:
    # Returns stdout as text.
    out = subprocess.check_output(["nmcli", "-t", *args], text=True)
    return out.strip()


def has_ipv4(ifname: str) -> bool:
    """Return True if the interface has a non-empty IPv4 address."""
    try:
        ip4 = nmcli("-f", "IP4.ADDRESS", "device", "show", ifname)
        # Looks like: "IP4.ADDRESS[1]:192.168.1.10/24" or empty.
        if ":" in ip4 and ip4.split(":", 1)[1].strip():
            return True
    except Exception:
        pass
    return False


def ensure_networkmanager() -> None:
    if shutil.which("nmcli") is None:
        raise RuntimeError("nmcli not found. Install and enable NetworkManager.")


def ensure_hotspot_up() -> None:
    """Ensure the provisioning hotspot is active via NetworkManager."""
    ensure_networkmanager()

    # Create the hotspot connection if it doesn't exist.
    # Using `nmcli dev wifi hotspot` keeps the setup simple and works on Pi OS.
    existing = nmcli("-f", "NAME", "connection", "show")
    names = set([line for line in existing.splitlines() if line])
    if HOTSPOT_CONN_NAME not in names:
        sh([
            "sudo",
            "nmcli",
            "dev",
            "wifi",
            "hotspot",
            "ifname",
            "wlan0",
            "con-name",
            HOTSPOT_CONN_NAME,
            "ssid",
            HOTSPOT_SSID,
            "password",
            HOTSPOT_PSK,
        ])
    else:
        # Bring it up (idempotent)
        sh(["sudo", "nmcli", "con", "up", HOTSPOT_CONN_NAME])

    # Give NM a moment to finish assigning the hotspot IP
    time.sleep(2)


# --- Helper functions for explicit Wi-Fi profile connection ---
def safe_conn_name(ssid: str) -> str:
    # Create a deterministic, NM-safe connection name from SSID
    base = "venue-" + "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in ssid)
    return base[:28]


def connect_with_profile(ssid: str, psk: str) -> None:
    """Create/replace a Wi-Fi profile with explicit key-mgmt/psk and bring it up."""
    conn = safe_conn_name(ssid)

    # Remove any old profile with this name (ignore errors)
    try:
        sh(["sudo", "nmcli", "con", "delete", conn])
    except Exception:
        pass

    # Add a Wi-Fi connection profile
    sh(["sudo", "nmcli", "con", "add", "type", "wifi", "ifname", "wlan0", "con-name", conn, "ssid", ssid])

    # Explicit security settings (prevents 'key-mgmt missing' errors)
    sh(["sudo", "nmcli", "con", "modify", conn, "wifi-sec.key-mgmt", "wpa-psk"])
    sh(["sudo", "nmcli", "con", "modify", conn, "wifi-sec.psk", psk])

    # Bring the connection up
    sh(["sudo", "nmcli", "con", "up", conn])


def join_venue_wifi(ssid: str, psk: str) -> None:
    """Switch from hotspot to client Wi-Fi and wait for an IP address."""
    ensure_networkmanager()

    # Stop hotspot if it's active
    try:
        sh(["sudo", "nmcli", "con", "down", HOTSPOT_CONN_NAME])
    except Exception:
        pass

    # After AP mode, force a rescan so NM sees nearby networks.
    try:
        sh(["sudo", "nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"])
    except Exception:
        pass

    # Give scan results time to populate and confirm the SSID is visible.
    visible = False
    deadline_scan = time.time() + 10
    while time.time() < deadline_scan:
        try:
            # `-t` output is colon-separated; SSID is the first field with `-f SSID`.
            ssids = nmcli("-f", "SSID", "dev", "wifi", "list", "ifname", "wlan0")
            if any(line.strip() == ssid for line in ssids.splitlines()):
                visible = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not visible:
        # Bring hotspot back so the user can reconnect and try again.
        try:
            sh(["sudo", "nmcli", "con", "up", HOTSPOT_CONN_NAME])
        except Exception:
            pass
        raise RuntimeError(f"SSID '{ssid}' not visible. If this is a hidden network, this UI does not support it yet.")

    # Connect using an explicit profile so key-mgmt/psk are always set.
    last_err: Exception | None = None
    for _ in range(2):
        try:
            connect_with_profile(ssid, psk)
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(2)

    if last_err is not None:
        # Bring hotspot back so the user can reconnect and try again.
        try:
            sh(["sudo", "nmcli", "con", "up", HOTSPOT_CONN_NAME])
        except Exception:
            pass
        raise RuntimeError(f"nmcli connect failed: {last_err}")

    # Wait for an IPv4 address and an activated state.
    deadline = time.time() + 30
    last_state = ""
    while time.time() < deadline:
        try:
            state = nmcli("-f", "GENERAL.STATE", "device", "show", "wlan0")
            ip4 = nmcli("-f", "IP4.ADDRESS", "device", "show", "wlan0")
            last_state = f"{state} {ip4}".strip()
            # GENERAL.STATE will look like: GENERAL.STATE:100 (connected)
            if "100" in state and "IP4.ADDRESS" in ip4 and ":" in ip4 and ip4.split(":", 1)[1].strip():
                return
        except Exception:
            pass
        time.sleep(1)

    raise RuntimeError(f"Timed out waiting for Wi-Fi connection/IP. Last: {last_state}")


def read_device_id() -> str:
    """Return a stable per-device ID.

    If the image was cloned and `/etc/hint/device_id` doesn't exist yet, generate
    a new UUID and persist it so this device stays unique across reboots.
    """
    # If already present, just read it.
    try:
        with open(DEVICE_ID_PATH, "r") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass

    # Ensure directory exists.
    os.makedirs(os.path.dirname(DEVICE_ID_PATH), exist_ok=True)

    # Generate a new ID and write it atomically.
    new_id = uuid.uuid4().hex  # 32-char lowercase hex
    tmp = "/tmp/device_id"
    with open(tmp, "w") as f:
        f.write(new_id + "\n")

    # Write to /etc/hint/device_id with root permissions (setup typically runs as root).
    # Use the same sudo+cp pattern as provisioning to avoid permission surprises.
    sh(["sudo", "cp", tmp, DEVICE_ID_PATH])
    sh(["sudo", "chmod", "600", DEVICE_ID_PATH])

    return new_id


# --- Hostname helpers ---
def desired_hostname(device_id: str) -> str:
    """Generate a stable, unique hostname for the device.

    Keep it short for mDNS friendliness and to fit hostname limits.
    """
    short = (device_id or "").strip().lower()[:8]
    # Ensure only safe chars (uuid hex is already safe)
    if not short:
        short = "unknown"
    return f"hintdev-{short}"


def set_hostname(hostname: str) -> None:
    """Set system hostname and update /etc/hosts mapping."""
    hostname = hostname.strip().lower()
    if not hostname:
        raise RuntimeError("hostname is empty")

    # Read current hostname; if already correct, do nothing.
    try:
        current = subprocess.check_output(["hostname"], text=True).strip().lower()
        if current == hostname:
            return
    except Exception:
        pass

    # Set hostname (persists /etc/hostname under systemd)
    sh(["sudo", "hostnamectl", "set-hostname", hostname])

    # Ensure 127.0.1.1 maps to the hostname for local resolution.
    # Replace an existing 127.0.1.1 line if present; otherwise append one.
    tmp_hosts = "/tmp/hosts"
    try:
        with open("/etc/hosts", "r") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []

    out_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith("127.0.1.1"):
            out_lines.append(f"127.0.1.1\t{hostname}")
            replaced = True
        else:
            out_lines.append(line)

    if not replaced:
        out_lines.append(f"127.0.1.1\t{hostname}")

    with open(tmp_hosts, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    sh(["sudo", "cp", tmp_hosts, "/etc/hosts"])

@app.get("/")
def index():
    wired = has_ipv4("eth0")
    return render_index_html(wired)

@app.post("/submit")
def submit():
    ssid = request.form.get("ssid", "").strip()
    psk = request.form.get("psk", "").strip()
    pair_code = request.form.get("pair_code", "").strip()

    wired = has_ipv4("eth0")

    if not pair_code:
        return "Missing pairing code", 400

    if not wired and (not ssid or not psk):
        return "Missing Wi-Fi fields", 400

    device_id = read_device_id()

    # Ensure a unique hostname so multiple devices on the same LAN are easy to manage.
    try:
        set_hostname(desired_hostname(device_id))
    except Exception as e:
        return f"Failed to set hostname: {e}", 500

    # Only switch Wi-Fi if we're not already on Ethernet.
    if not wired:
        try:
            join_venue_wifi(ssid, psk)
        except Exception as e:
            return f"Failed to join venue Wi-Fi: {e}", 500

    # Pair with hub
    try:
        r = requests.post(HUB_PAIR_URL, json={"pair_code": pair_code, "device_id": device_id}, timeout=10)
        if r.status_code != 200:
            return f"Pairing failed ({r.status_code}): {r.text}", 500
        data = r.json()
        if not data.get("ok"):
            return f"Pairing failed: {data}", 500
    except Exception as e:
        return f"Could not reach hub for pairing: {e}", 500

    # Save provisioning (atomic)
    tmp = "/tmp/provision.json"
    with open(tmp, "w") as f:
        json.dump(data, f)
    sh(["sudo", "cp", tmp, PROVISION_PATH])
    sh(["sudo", "chmod", "600", PROVISION_PATH])

    # Enable and start agent service
    enable_agent_service()

    # Reboot into agent mode
    sh(["sudo", "reboot"])
    return "Paired. Rebooting..."

def main():
    # If already provisioned, don't enter setup mode again.
    if os.path.exists(PROVISION_PATH):
        enable_agent_service()
        sh(["sudo", "reboot"])
        return

    wired = has_ipv4("eth0")

    # If Ethernet is up, do NOT start the provisioning hotspot.
    if not wired:
        ensure_hotspot_up()
    else:
        # Ensure hotspot isn't active (best effort).
        try:
            sh(["sudo", "nmcli", "con", "down", HOTSPOT_CONN_NAME])
        except Exception:
            pass

    # Run Flask (this must NOT exit)
    app.run(host="0.0.0.0", port=80)

# Run unconditionally so it can't “exit cleanly” due to __name__ oddities
main()