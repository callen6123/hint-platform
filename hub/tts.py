from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error


# Manufacturing fallback. Preferred source is ELEVENLABS_API_KEY from hub.env.
SECRET_KEY_PATH = Path("/etc/hint/secrets/elevenlabs_api_key")


@dataclass(frozen=True)
class TTSResult:
    audio: bytes
    content_type: str
    ext: str
    model_id: str | None = None
    voice_id: str | None = None


class ElevenLabsError(RuntimeError):
    pass


def _read_api_key() -> str:
    """Read the ElevenLabs API key.

    Preferred: ELEVENLABS_API_KEY environment variable (e.g. from /etc/hint/hub.env)
    Fallback: manufacturing secret file at SECRET_KEY_PATH.
    """
    env_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if env_key:
        # Basic sanity check: ElevenLabs keys are non-trivial length.
        if len(env_key) < 20:
            raise ElevenLabsError("ELEVENLABS_API_KEY looks too short; check provisioning.")
        return env_key

    try:
        key = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise ElevenLabsError(
            f"ElevenLabs API key not found. Set ELEVENLABS_API_KEY or provision {SECRET_KEY_PATH}."
        ) from e

    if not key:
        raise ElevenLabsError(f"ElevenLabs API key file at {SECRET_KEY_PATH} is empty.")

    if len(key) < 20:
        raise ElevenLabsError("ElevenLabs API key looks too short; check provisioning.")

    return key


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout_sec: float = 30.0,
) -> tuple[int, dict[str, Any] | None, bytes]:
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                try:
                    return resp.status, json.loads(raw.decode("utf-8")), raw
                except Exception:
                    return resp.status, None, raw
            return resp.status, None, raw
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        msg = raw.decode("utf-8", errors="replace")[:500]
        raise ElevenLabsError(f"HTTP {e.code} calling {url}: {msg}") from e
    except urllib.error.URLError as e:
        raise ElevenLabsError(f"Network error calling {url}: {e}") from e


def synthesize(
    text: str,
    *,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
    output_format: str = "pcm_16000",
    stability: float | None = None,
    similarity_boost: float | None = None,
    style: float | None = None,
    speaker_boost: bool | None = None,
    timeout_sec: float = 60.0,
) -> TTSResult:
    """Synthesize speech via ElevenLabs.

    Args:
        text: The text to speak.
        voice_id: The ElevenLabs voice ID.
        model_id: The ElevenLabs model ID.
        output_format: ElevenLabs output_format (use a pcm_* value to get a WAV file, e.g. 'pcm_16000').
        stability/similarity_boost/style/speaker_boost: Optional voice settings.
        timeout_sec: HTTP timeout.

    Returns:
        TTSResult with audio bytes and metadata.

    Raises:
        ElevenLabsError on failure.
    """

    t = (text or "").strip()
    if not t:
        raise ElevenLabsError("synthesize(): text is empty")

    # ElevenLabs supports multiple output formats; for "just give me a WAV" we use pcm_*.
    if not output_format:
        raise ElevenLabsError("output_format is empty")

    api_key = _read_api_key()

    # ElevenLabs TTS endpoint.
    # Request WAV by using a pcm_* output_format (ElevenLabs returns a WAV container).
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={output_format}"

    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/wav",
    }

    voice_settings: dict[str, Any] = {}
    if stability is not None:
        voice_settings["stability"] = float(stability)
    if similarity_boost is not None:
        voice_settings["similarity_boost"] = float(similarity_boost)
    if style is not None:
        voice_settings["style"] = float(style)
    if speaker_boost is not None:
        voice_settings["use_speaker_boost"] = bool(speaker_boost)

    payload: dict[str, Any] = {
        "text": t,
        "model_id": model_id,
    }
    if voice_settings:
        payload["voice_settings"] = voice_settings

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            audio = resp.read()
            ct = resp.headers.get("Content-Type", "")

            if not audio:
                raise ElevenLabsError("Empty audio response from ElevenLabs")

            # We expect a WAV container when using pcm_* formats.
            # WAV files start with RIFF....WAVE.
            if output_format.startswith("pcm_"):
                if not (audio.startswith(b"RIFF") and b"WAVE" in audio[:32]):
                    head = audio[:32]
                    raise ElevenLabsError(
                        f"Expected WAV (RIFF/WAVE) from ElevenLabs but got different bytes: {head!r}"
                    )

            content_type = "audio/wav"
            ext = "wav"

            # Prefer server-provided content-type if it is audio/*
            if ct.startswith("audio/"):
                content_type = ct.split(";")[0].strip()

            return TTSResult(
                audio=audio,
                content_type=content_type,
                ext=ext,
                model_id=model_id,
                voice_id=voice_id,
            )

    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        msg = raw.decode("utf-8", errors="replace")[:800]
        elapsed = time.time() - started
        raise ElevenLabsError(
            f"ElevenLabs TTS failed HTTP {e.code} after {elapsed:.2f}s: {msg}"
        ) from e
    except urllib.error.URLError as e:
        elapsed = time.time() - started
        raise ElevenLabsError(
            f"ElevenLabs TTS network error after {elapsed:.2f}s: {e}"
        ) from e


def list_voices(timeout_sec: float = 20.0) -> list[dict[str, Any]]:
    """Return available voices (debug/admin use).

    Not intended for end users.
    """
    api_key = _read_api_key()
    status, js, _ = _http_json(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key},
        timeout_sec=timeout_sec,
    )
    if status != 200 or not isinstance(js, dict):
        raise ElevenLabsError(f"Unexpected response listing voices: {status}")
    voices = js.get("voices")
    if not isinstance(voices, list):
        return []
    # Keep only a subset of fields.
    out: list[dict[str, Any]] = []
    for v in voices:
        if isinstance(v, dict):
            out.append(
                {
                    "voice_id": v.get("voice_id"),
                    "name": v.get("name"),
                    "category": v.get("category"),
                }
            )
    return out


def api_key_present() -> bool:
    """Return True if the manufacturing secret exists and is non-empty."""
    try:
        return bool(SECRET_KEY_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return False


if __name__ == "__main__":
    # Minimal smoke test (requires the secret file on the hub)
    vid = os.environ.get("ELEVENLABS_VOICE_ID")
    if not vid:
        raise SystemExit("Set ELEVENLABS_VOICE_ID to run the smoke test.")

    r = synthesize("Hello from HintPlatform.", voice_id=vid, output_format="pcm_16000")
    out = Path("/tmp/hint_tts_test." + r.ext)
    out.write_bytes(r.audio)
    print(f"Wrote {len(r.audio)} bytes to {out}")