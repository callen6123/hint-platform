from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request, send_file


AUDIO_DIR = Path("/opt/hint/audio_cache")
DEVICES_DB = Path("/var/lib/hint/devices.json")


def _sanitize_id(s: str) -> str:
    out = []
    for ch in (s or ""):
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
    return "".join(out)


def _load_devices() -> dict[str, Any]:
    if not DEVICES_DB.exists():
        return {}
    try:
        return json.loads(DEVICES_DB.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _device_token(device_id: str) -> str | None:
    d = _load_devices().get(device_id)
    if isinstance(d, dict):
        tok = d.get("ota_token")
        if tok:
            return str(tok)
    return None


def _auth_device_or_403(device_id: str, token: str) -> None:
    expected = _device_token(device_id)
    if not expected:
        abort(403)
    if token != expected:
        abort(403)


@dataclass(frozen=True)
class StoredAudio:
    audio_id: str
    path: Path
    sha256: str
    size: int
    ext: str
    content_type: str


class AudioStore:
    """Filesystem-backed audio store."""

    def __init__(self, base_dir: Path = AUDIO_DIR):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _content_type_for_ext(self, ext: str) -> str:
        ext = ext.lower().lstrip(".")
        if ext == "wav":
            return "audio/wav"
        if ext == "mp3":
            return "audio/mpeg"
        return "application/octet-stream"

    def put(self, audio: bytes, ext: str, *, prefix: str = "tts") -> StoredAudio:
        ext = ext.lower().lstrip(".")
        if ext not in ("wav", "mp3"):
            raise ValueError("ext must be wav or mp3")

        h = hashlib.sha256(audio).hexdigest()
        audio_id = f"{prefix}-{int(time.time())}-{h[:10]}"
        audio_id = _sanitize_id(audio_id)
        p = self.base_dir / f"{audio_id}.{ext}"
        p.write_bytes(audio)

        return StoredAudio(
            audio_id=audio_id,
            path=p,
            sha256=h,
            size=len(audio),
            ext=ext,
            content_type=self._content_type_for_ext(ext),
        )

    def get_path(self, audio_id: str) -> Path | None:
        audio_id = _sanitize_id(audio_id)
        if not audio_id:
            return None
        # Prefer wav, then mp3.
        p1 = self.base_dir / f"{audio_id}.wav"
        if p1.exists():
            return p1
        p2 = self.base_dir / f"{audio_id}.mp3"
        if p2.exists():
            return p2
        return None


bp = Blueprint("audio", __name__)
_store = AudioStore()


@bp.get("/audio/health")
def audio_health():
    return jsonify({"ok": True, "audio_dir": str(AUDIO_DIR)})


@bp.get("/audio/file/<audio_id>")
def audio_file(audio_id: str):
    """Serve an audio file to a device.

    Query params:
      - device_id
      - token (must match devices.json ota_token)

    Response:
      - 200 + audio bytes
      - 403 on auth failure
      - 404 if not found
    """
    device_id = request.args.get("device_id", "")
    token = request.args.get("token", "")
    device_id = _sanitize_id(device_id)

    if not device_id or not token:
        abort(403)

    _auth_device_or_403(device_id, token)

    p = _store.get_path(audio_id)
    if not p:
        abort(404)

    # send_file handles range requests and sets a decent Content-Type
    return send_file(p, as_attachment=False, conditional=True)


def build_audio_url(base_url: str, *, audio_id: str, device_id: str, token: str) -> str:
    """Build a fully-qualified URL for a device to fetch audio."""
    base = base_url.rstrip("/")
    audio_id = _sanitize_id(audio_id)
    device_id = _sanitize_id(device_id)
    return f"{base}/audio/file/{audio_id}?device_id={device_id}&token={token}"


def store_audio(audio: bytes, ext: str, *, prefix: str = "tts") -> StoredAudio:
    """Store audio bytes and return StoredAudio metadata."""
    return _store.put(audio, ext, prefix=prefix)


def ensure_audio_dir():
    """Ensure the audio cache directory exists."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
