import json
import os
import shutil
import subprocess
import time
import secrets

from flask import Flask, request

# Hub provisioning marker/config
PROVISION_PATH = "/etc/hint/hub_provision.json"
HUB_ENV_PATH = "/etc/hint/hub.env"

# If your hub service has a different name, change it here.
HUB_SERVICE_NAMES = [
    "hint-hub-pair.service",
    "hint-device-watch.service",
]

# Hotspot settings used only when Ethernet is not up
HOTSPOT_CONN_NAME = "hinthub-setup"
HOTSPOT_SSID = "HintHub-SETUP"
HOTSPOT_PSK = "hintsetup123"

# Hostname you want for mDNS (hinthub.local). Change if needed.
DESIRED_HOSTNAME = "hinthub"

app = Flask(__name__)

HTML_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Hint Hub Setup</title>
  </head>
  <body style=\"font-family: sans-serif; max-width: 520px; margin: 40px auto;\">
    <h2>Hint Hub Setup</h2>

    <p>{connection_note}</p>

    <form method=\"post\" action=\"/submit\">
      {wifi_fields}
      {admin_fields}

      <button type=\"submit\" style=\"padding:12px 18px\">Connect & Provision Hub</button>
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

ADMIN_FIELDS_HTML = """
<h3>Hub Admin</h3>
<label>Admin Username</label><br>
<input name=\"admin_user\" style=\"width:100%;padding:10px\" value=\"admin\" required><br><br>
<label>Admin Password</label><br>
<input name=\"admin_pass\" type=\"password\" style=\"width:100%;padding:10px\" required><br><br>
"""

SITE_FIELDS_HTML = """
<h3>Site</h3>
<label>Site Name</label><br>
<input name=\"site_name\" style=\"width:100%;padding:10px\" value=\"demo\" required><br><br>
"""

def sh(cmd: list[str]) -> None:
    subprocess.check_call(cmd)


def read_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Preserves unknown keys.

    Lines starting with # or blank lines are ignored.
    Values are taken verbatim after the first '='.
    """
    env: dict[str, str] = {}
    try:
        with open(path, "r") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def write_env_file(path: str, env: dict[str, str]) -> None:
    """Write env vars to a root-owned 600 file via sudo cp."""
    # Stable ordering for readability.
    preferred = [
        "ELEVENLABS_API_KEY",
        "HINT_UI_USER",
        "HINT_UI_PASS",
        "HINT_UI_SECRET",
        "HINT_DASH_API_KEY",
    ]

    lines: list[str] = [
        "# Hint Hub environment",
        "# ELEVENLABS_API_KEY is provisioned manually before shipping.",
    ]

    # Write preferred keys first (even if blank).
    for k in preferred:
        lines.append(f"{k}={env.get(k, '')}")

    # Then write any other keys that already exist.
    for k in sorted(env.keys()):
        if k in preferred:
            continue
        lines.append(f"{k}={env[k]}")

    tmp = "/tmp/hub.env"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")

    sh(["sudo", "cp", tmp, path])
    sh(["sudo", "chown", "root:root", path])
    sh(["sudo", "chmod", "600", path])


def normalize_site_id(site_name: str) -> str:
    """Convert a human-entered site name into a safe site ID.

    Examples:
      "My Escape Room" -> "my-escape-room"
      "St. Louis #2"   -> "st-louis-2"
    """
    raw = (site_name or "").strip().lower()
    out_chars: list[str] = []
    prev_dash = False

    for ch in raw:
        if ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out_chars.append("-")
                prev_dash = True

    site_id = "".join(out_chars).strip("-")
    return site_id or "demo"


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

    existing = nmcli("-f", "NAME", "connection", "show")
    names = set([line for line in existing.splitlines() if line])

    if HOTSPOT_CONN_NAME not in names:
        sh(
            [
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
            ]
        )
    else:
        sh(["sudo", "nmcli", "con", "up", HOTSPOT_CONN_NAME])

    # Give NM a moment to finish assigning the hotspot IP
    time.sleep(2)


def safe_conn_name(ssid: str) -> str:
    base = "venue-" + "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in ssid)
    return base[:28]


def connect_with_profile(ssid: str, psk: str) -> None:
    """Create/replace a Wi-Fi profile with explicit key-mgmt/psk and bring it up."""
    conn = safe_conn_name(ssid)

    try:
        sh(["sudo", "nmcli", "con", "delete", conn])
    except Exception:
        pass

    sh(["sudo", "nmcli", "con", "add", "type", "wifi", "ifname", "wlan0", "con-name", conn, "ssid", ssid])
    sh(["sudo", "nmcli", "con", "modify", conn, "wifi-sec.key-mgmt", "wpa-psk"])
    sh(["sudo", "nmcli", "con", "modify", conn, "wifi-sec.psk", psk])
    sh(["sudo", "nmcli", "con", "up", conn])


def join_venue_wifi(ssid: str, psk: str) -> None:
    """Switch from hotspot to client Wi-Fi and wait for an IP address."""
    ensure_networkmanager()

    # Stop hotspot if it's active
    try:
        sh(["sudo", "nmcli", "con", "down", HOTSPOT_CONN_NAME])
    except Exception:
        pass

    # Rescan after AP mode
    try:
        sh(["sudo", "nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"])
    except Exception:
        pass

    visible = False
    deadline_scan = time.time() + 10
    while time.time() < deadline_scan:
        try:
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
        raise RuntimeError(
            f"SSID '{ssid}' not visible. If this is a hidden network, this UI does not support it yet."
        )

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
            if "100" in state and ":" in ip4 and ip4.split(":", 1)[1].strip():
                return
        except Exception:
            pass
        time.sleep(1)

    raise RuntimeError(f"Timed out waiting for Wi-Fi connection/IP. Last: {last_state}")


def set_hostname_if_needed() -> None:
    """Best-effort: ensure hostname is set so hinthub.local works."""
    try:
        current = subprocess.check_output(["hostname"], text=True).strip()
        if current != DESIRED_HOSTNAME:
            sh(["sudo", "hostnamectl", "set-hostname", DESIRED_HOSTNAME])
    except Exception:
        # Non-fatal
        pass


def enable_hub_service() -> None:
    ok = False
    errs: list[str] = []

    sh(["sudo", "systemctl", "daemon-reload"])

    for svc in HUB_SERVICE_NAMES:
        try:
            sh(["sudo", "systemctl", "enable", svc])
            sh(["sudo", "systemctl", "start", svc])
            ok = True
        except Exception as e:
            errs.append(f"{svc}: {e}")

    if not ok:
        raise RuntimeError("Failed to enable/start any hub services: " + "; ".join(errs))


def render_index_html(wired: bool) -> str:
    if wired:
        note = (
            "Ethernet detected (DHCP active). You can provision the hub without entering Wi-Fi. "
            "If you want the hub to use Wi-Fi instead, unplug Ethernet and refresh."
        )
        wifi_fields = SITE_FIELDS_HTML
    else:
        note = (
            "Connect your phone/laptop to Wi-Fi <b>HintHub-SETUP</b> (password <b>hintsetup123</b>). "
            "Your phone may open the setup page automatically; if not, open your browser and visit the gateway IP "
            "(often <b>10.42.0.1</b>)."
        )
        wifi_fields = SITE_FIELDS_HTML + WIFI_FIELDS_HTML

    return HTML_TEMPLATE.format(connection_note=note, wifi_fields=wifi_fields, admin_fields=ADMIN_FIELDS_HTML)


@app.get("/")
def index():
    wired = has_ipv4("eth0")
    return render_index_html(wired)


@app.post("/submit")
def submit():
    site_name = request.form.get("site_name", "").strip()
    ssid = request.form.get("ssid", "").strip()
    psk = request.form.get("psk", "").strip()
    admin_user = request.form.get("admin_user", "").strip()
    admin_pass = request.form.get("admin_pass", "").strip()

    wired = has_ipv4("eth0")

    if not site_name:
        return "Missing site name", 400

    if not admin_user or not admin_pass:
        return "Missing admin credentials", 400

    if not wired and (not ssid or not psk):
        return "Missing Wi-Fi fields", 400

    # If we're not already on Ethernet, join venue Wi-Fi.
    if not wired:
        try:
            join_venue_wifi(ssid, psk)
        except Exception as e:
            return f"Failed to join venue Wi-Fi: {e}", 500

    # Provision hub.env (UI creds + secrets) WITHOUT asking for ElevenLabs.
    # Preserve any existing ELEVENLABS_API_KEY you manually install before shipping.
    env = read_env_file(HUB_ENV_PATH)
    if "ELEVENLABS_API_KEY" not in env:
        env["ELEVENLABS_API_KEY"] = ""

    site_id = normalize_site_id(site_name)

    env["HUB_SITE_ID"] = site_id
    env["HINT_UI_USER"] = admin_user
    env["HINT_UI_PASS"] = admin_pass

    if not env.get("HINT_UI_SECRET", "").strip():
        env["HINT_UI_SECRET"] = secrets.token_urlsafe(32)

    # Optional: seed a dashboard/API key if used.
    if not env.get("HINT_DASH_API_KEY", "").strip():
        env["HINT_DASH_API_KEY"] = secrets.token_urlsafe(32)

    try:
        write_env_file(HUB_ENV_PATH, env)
    except Exception as e:
        return f"Failed to write hub.env: {e}", 500

    # Mark provisioned so we don't enter setup mode again.
    provision = {
        "ok": True,
        "provisioned": True,
        "site": {
            "name": site_name,
            "id": site_id,
        },
        "network": {"mode": "wired" if wired else "wifi", "ssid": ssid if not wired else None},
        "hostname": DESIRED_HOSTNAME,
        "ts": int(time.time()),
    }

    tmp = "/tmp/hub_provision.json"
    with open(tmp, "w") as f:
        json.dump(provision, f)

    sh(["sudo", "cp", tmp, PROVISION_PATH])
    sh(["sudo", "chmod", "600", PROVISION_PATH])

    # Ensure hostname is correct for mDNS (best-effort).
    set_hostname_if_needed()

    # Enable and start hub service.
    try:
        enable_hub_service()
    except Exception as e:
        return str(e), 500

    # Reboot into hub mode.
    sh(["sudo", "reboot"])
    return "Provisioned. Rebooting..."


def main():
    # If already provisioned, don't enter setup mode again.
    if os.path.exists(PROVISION_PATH):
        try:
            set_hostname_if_needed()
            enable_hub_service()
        except Exception:
            # Even if service start fails, proceed to reboot; systemd will retry on boot.
            pass
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

    # Run Flask
    app.run(host="0.0.0.0", port=80)


# Run unconditionally so it can't “exit cleanly” due to __name__ oddities
main()