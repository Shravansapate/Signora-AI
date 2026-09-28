"""One bounded, reusable and killable CPU encoder; no catalog lock during inference."""

import logging
import multiprocessing
import threading
from collections import OrderedDict

from app.lifecycle import LifecycleError, require
from app.retrieval.encoder_model import ENCODING_POLICY, MODEL_REVISION


def worker(connection, directory):
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        from app.retrieval.encoder_model import verify

        verify(directory)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(directory / "onnx/model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        tokenizer = Tokenizer.from_file(str(directory / "onnx/tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        inputs = {item.name for item in session.get_inputs()}
        connection.send({"status": "READY"})
        while True:
            text = connection.recv()
            try:
                tokens = tokenizer.encode(text)
                if not 1 <= len(tokens.ids) <= 512:
                    raise ValueError("Encoder token limit; no silent truncation")
                values = {
                    "input_ids": np.array([tokens.ids], dtype=np.int64),
                    "attention_mask": np.array([tokens.attention_mask], dtype=np.int64),
                    "token_type_ids": np.array([tokens.type_ids], dtype=np.int64),
                }
                output = session.run(None, {k: v for k, v in values.items() if k in inputs})[0]
                mask = values["attention_mask"][..., None]
                vector = (output * mask).sum(axis=1) / mask.sum(axis=1)
                vector /= np.linalg.norm(vector, axis=1, keepdims=True)
                if vector.shape != (1, 384) or not np.isfinite(vector).all():
                    raise ValueError("Invalid encoder output")
                connection.send({"status": "READY", "vector": vector[0].tolist()})
            except Exception:
                connection.send({"status": "FAILED"})
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send({"status": "FAILED"})
        except (EOFError, OSError):
            pass
    finally:
        connection.close()


class LocalEncoder:
    def __init__(self, settings):
        self.directory = settings.encoder_model_path
        self.timeout = settings.encoder_timeout_seconds
        self.state = "NOT_CONFIGURED" if self.directory is None else "STARTING"
        self.lane = threading.Lock()
        self.lifecycle = threading.RLock()
        self.process = self.connection = self.thread = None
        self.closed = False
        self.cache = OrderedDict()

    def warm(self):
        if self.directory:
            self.thread = threading.Thread(target=self._warm, daemon=True)
            self.thread.start()

    def _warm(self):
        try:
            self.encode("warmup", "query")
        except LifecycleError:
            logging.getLogger("signora.encoder").warning("encoder_warmup_unavailable")

    def _receive(self):
        connection = self.connection
        require(
            connection is not None and connection.poll(self.timeout),
            "ENCODER_TIMEOUT",
            "Semantic encoder timed out",
            503,
        )
        result = connection.recv()
        require(
            result.get("status") == "READY",
            "ENCODER_UNAVAILABLE",
            "Semantic encoder unavailable",
            503,
        )
        return result

    def encode(self, text, kind):
        require(
            kind in {"query", "passage"} and 0 < len(text) <= 6000,
            "ENCODING_INPUT",
            "Invalid bounded encoding input",
            422,
        )
        require(self.lane.acquire(blocking=False), "ENCODER_BUSY", "Semantic encoder busy", 429)
        try:
            require(
                self.directory is not None and not self.closed,
                "ENCODER_UNAVAILABLE",
                "Semantic encoder not configured",
                503,
            )
            key = (MODEL_REVISION, ENCODING_POLICY, kind, text)
            if key in self.cache:
                self.cache.move_to_end(key)
                return list(self.cache[key])
            if self.process is None or not self.process.is_alive():
                self._terminate()
                context = multiprocessing.get_context("spawn")
                with self.lifecycle:
                    require(not self.closed, "ENCODER_UNAVAILABLE", "Encoder stopping", 503)
                    self.connection, child = context.Pipe()
                    self.process = context.Process(
                        target=worker, args=(child, self.directory), daemon=True
                    )
                    self.process.start()
                    child.close()
                self._receive()
            self.connection.send(f"{kind}: {text}")
            vector = self._receive()["vector"]
            self.state = "READY"
            self.cache[key] = vector
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
            return list(vector)
        except (LifecycleError, EOFError, OSError, ValueError, KeyError) as exc:
            self.state = "UNAVAILABLE"
            self._terminate()
            if isinstance(exc, LifecycleError):
                raise
            raise LifecycleError(
                "ENCODER_UNAVAILABLE", "Semantic encoder unavailable", 503
            ) from exc
        finally:
            self.lane.release()

    def _terminate(self):
        with self.lifecycle:
            process, connection = self.process, self.connection
            self.process = self.connection = None
            if process:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
            if connection:
                connection.close()

    def close(self):
        self.closed = True
        self._terminate()
        if self.thread:
            self.thread.join(timeout=3)
        self.cache.clear()
