import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

REPO = Path("/home/hub/hint-platform")
DEVICE_DIR = REPO / "device"
OUT_DIR = Path("/opt/hint/ota")

def sh(cmd: list[str]) -> str:
    res = subprocess.run(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
    return res.stdout.strip()

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Pull latest code
    sh(["git", "pull"])

    ver = sh(["git", "rev-parse", "--short", "HEAD"])
    bundle = OUT_DIR / f"hint-device-{ver}.tar.gz"

    # Create tarball containing the device/ tree only
    if bundle.exists():
        bundle.unlink()

    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(DEVICE_DIR), arcname="device")

    digest = sha256_file(bundle)

    latest = {
        "version": ver,
        "filename": bundle.name,
        "sha256": digest,
    }
    (OUT_DIR / "latest.json").write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")

    print(f"Built {bundle} sha256={digest}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())