"""ASR has one bounded lane and a killable worker; failures never disable typed input."""

import logging
import multiprocessing
import threading
from uuid import uuid4

from app.asr_worker import serve
from app.lifecycle import LifecycleError, require

logger = logging.getLogger("signora.asr")


class LocalASR:
    def __init__(self, settings):
        self.directory = settings.asr_model_path
        self.timeout = settings.asr_timeout_seconds
        self.lane = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.process = None
        self.connection = None
        self.closed = threading.Event()
        self.state = "NOT_CONFIGURED" if self.directory is None else "STARTING"
        self.thread = None

    def warm(self):
        if self.directory is not None:
            self.thread = threading.Thread(target=self._warm, daemon=True)
            self.thread.start()

    def _warm(self):
        with self.lane:
            try:
                self._start()
            except Exception:
                self.state = "UNAVAILABLE"
                self._terminate()
                logger.warning("asr_warmup_unavailable")

    def _receive(self):
        connection = self.connection
        require(
            connection is not None and connection.poll(self.timeout),
            "ASR_TIMEOUT",
            "Speech recognition timed out; typed input remains available",
            503,
        )
        return connection.recv()

    def _start(self):
        require(
            self.directory is not None and not self.closed.is_set(),
            "ASR_UNAVAILABLE",
            "Local speech recognition is not configured",
            503,
        )
        if self.process is not None and self.process.is_alive() and self.state == "READY":
            return
        self._terminate()
        self.state = "STARTING"
        context = multiprocessing.get_context("spawn")
        with self.lifecycle_lock:
            require(not self.closed.is_set(), "ASR_UNAVAILABLE", "Speech service is stopping", 503)
            self.connection, child = context.Pipe()
            self.process = context.Process(target=serve, args=(child, self.directory), daemon=True)
            self.process.start()
            child.close()
        response = self._receive()
        require(
            response.get("status") == "READY" and not self.closed.is_set(),
            "ASR_UNAVAILABLE",
            "The local speech model is unavailable",
            503,
        )
        self.state = "READY"

    def transcribe(self, path):
        require(
            self.lane.acquire(blocking=False),
            "ASR_BUSY",
            "Speech recognition is busy; retry shortly",
            429,
        )
        try:
            self._start()
            request_id = str(uuid4())
            self.connection.send({"id": request_id, "path": str(path)})
            response = self._receive()
            require(
                response.get("id") == request_id and response.get("status") == "FINAL",
                "ASR_FAILED",
                "No reliable final transcript was produced; record again or type",
                422,
            )
            return response
        except LifecycleError as exc:
            if exc.status == 503:
                self.state = "UNAVAILABLE"
                self._terminate()
            raise
        except (EOFError, BrokenPipeError, OSError, ValueError) as exc:
            self.state = "UNAVAILABLE"
            self._terminate()
            raise LifecycleError(
                "ASR_UNAVAILABLE", "Speech recognition stopped; typed input remains available", 503
            ) from exc
        finally:
            self.lane.release()

    def _terminate(self):
        with self.lifecycle_lock:
            process, connection = self.process, self.connection
            self.process = self.connection = None
            if process is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
            if connection is not None:
                connection.close()

    def close(self):
        self.closed.set()
        self._terminate()
        if self.thread:
            self.thread.join(timeout=3)
