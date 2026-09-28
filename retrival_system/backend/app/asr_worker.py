"""Private supervised process: warm one CPU model, then finalize bounded recordings."""

import math
import wave


def serve(connection, directory):
    try:
        from app.asr_model import verify_model

        manifest = verify_model(directory)
        import numpy as np
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(directory),
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            num_workers=1,
            local_files_only=True,
        )
        connection.send(
            {"status": "READY", "model": manifest["model_id"], "revision": manifest["revision"]}
        )
        while True:
            message = connection.recv()
            if message is None:
                return
            try:
                with wave.open(message["path"], "rb") as audio:
                    samples = (
                        np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(
                            np.float32
                        )
                        / 32768
                    )
                segments, _ = model.transcribe(
                    samples,
                    language="en",
                    task="transcribe",
                    beam_size=5,
                    temperature=0,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    word_timestamps=True,
                )
                text, diagnostics = [], []
                for segment in segments:
                    if len(diagnostics) >= 128:
                        raise ValueError("ASR output limit")
                    text.append(segment.text)
                    values = [segment.avg_logprob, segment.no_speech_prob]
                    if not all(math.isfinite(v) for v in values):
                        raise ValueError("Invalid ASR score")
                    diagnostics.append(
                        {
                            "start": segment.start,
                            "end": segment.end,
                            "average_log_probability": segment.avg_logprob,
                            "no_speech_score": segment.no_speech_prob,
                            "low_score_words": [
                                w.word for w in (segment.words or []) if w.probability < 0.8
                            ][:32],
                        }
                    )
                transcript = "".join(text).strip()
                if not transcript or len(transcript) > 2048:
                    raise ValueError("Empty or excessive ASR text")
                connection.send(
                    {
                        "status": "FINAL",
                        "id": message["id"],
                        "text": transcript,
                        "metadata": {
                            "final": True,
                            "source_text_language": "en",
                            "engine": "faster-whisper",
                            "model_id": manifest["model_id"],
                            "model_revision": manifest["revision"],
                            "segments": diagnostics,
                            "confirmation_required": True,
                        },
                    }
                )
            except Exception:
                connection.send({"status": "FAILED", "id": message["id"]})
    except (EOFError, BrokenPipeError):
        return
    except Exception:
        try:
            connection.send({"status": "UNAVAILABLE"})
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()
