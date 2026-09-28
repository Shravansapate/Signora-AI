"""Durable import/cleanup worker entry point; no in-process HTTP background jobs."""

import argparse
import logging
import signal
import threading

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import build_database
from app.imports import run_imports
from app.lifecycle import cleanup_one
from app.storage import LocalAssetStore


def main():
    parser = argparse.ArgumentParser(description="Process Signora's durable registry work")
    parser.add_argument(
        "--once", action="store_true", help="Drain currently runnable work and exit"
    )
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine, sessions = build_database(settings)
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        while not stopping.is_set():
            try:
                run_imports(sessions, settings, workers=arguments.workers, stop_event=stopping)
                with sessions() as session:
                    while not stopping.is_set() and cleanup_one(
                        session, LocalAssetStore(settings.storage_root)
                    ):
                        pass
            except (SQLAlchemyError, OSError):
                logging.error("registry_worker_dependency_unavailable")
                if arguments.once:
                    raise SystemExit(1) from None
            if arguments.once:
                return
            stopping.wait(2)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
