"""Bounded 16 kHz mono PCM input. Raw recordings are temporary, never registry assets."""

import array
import hashlib
import math
import struct
import sys
import wave

from app.lifecycle import require

MAX_AUDIO_BYTES = 2 * 1024 * 1024


def inspect_audio(path):
    require(
        44 <= path.stat().st_size <= MAX_AUDIO_BYTES, "AUDIO_SIZE", "Record at most 60 seconds", 422
    )
    with path.open("rb") as stream:
        header = stream.read(12)
    require(
        header[:4] == b"RIFF"
        and header[8:12] == b"WAVE"
        and struct.unpack("<I", header[4:8])[0] + 8 == path.stat().st_size,
        "AUDIO_FORMAT",
        "Use a complete PCM WAV recording",
        422,
    )
    try:
        with wave.open(str(path), "rb") as audio:
            require(
                audio.getnchannels() == 1
                and audio.getsampwidth() == 2
                and audio.getframerate() == 16000
                and audio.getcomptype() == "NONE",
                "AUDIO_FORMAT",
                "Use 16 kHz mono 16-bit PCM WAV",
                422,
            )
            frames = audio.getnframes()
            require(
                4000 <= frames <= 960000,
                "AUDIO_DURATION",
                "Record between 0.25 and 60 seconds",
                422,
            )
            raw = audio.readframes(frames)
            require(len(raw) == frames * 2, "AUDIO_TRUNCATED", "The recording is incomplete", 422)
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid WAV recording") from exc
    samples = array.array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    require(rms >= 0.001, "NO_SPEECH", "The recording is silent or too quiet; record again", 422)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    clipped_fraction = sum(abs(s) >= 32700 for s in samples) / len(samples)
    warnings = []
    if rms < 0.01:
        warnings.append("QUIET_AUDIO")
    if clipped_fraction > 0.01:
        warnings.append("CLIPPED_AUDIO")
    return {
        "audio_sha256": digest,
        "duration_seconds": frames / 16000,
        "rms_dbfs": round(20 * math.log10(rms), 1),
        "clipped_fraction": round(clipped_fraction, 4),
        "warnings": warnings,
    }
