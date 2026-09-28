"""Durable content-addressed storage. Source GLBs are never moved or overwritten."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol

MAX_OBJECT_BYTES = 128 * 1024 * 1024


class StorageError(ValueError):
    pass


class AssetStore(Protocol):
    def put(self, source: BinaryIO, expected_sha256: str) -> str: ...
    def verify(self, key: str, expected_sha256: str) -> Path: ...
    def delete(self, key: str, expected_sha256: str) -> None: ...


class LocalAssetStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, key: str) -> Path:
        if not re.fullmatch(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}\.glb", key):
            raise StorageError("Invalid immutable asset key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("Asset escaped storage root")
        return path

    def verify(self, key: str, expected_sha256: str) -> Path:
        path = self.resolve(key)
        try:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            raise StorageError("Immutable asset is unavailable") from exc
        if digest != expected_sha256:
            raise StorageError("Immutable asset checksum mismatch")
        return path

    def delete(self, key: str, expected_sha256: str) -> None:
        """Only the catalog cleanup worker calls this after locking and checking references."""
        path = self.resolve(key)
        if not path.exists():
            return  # Recovery after an unlink followed by a database commit failure.
        self.verify(key, expected_sha256)
        path.unlink()

    def put(self, source: BinaryIO, expected_sha256: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise StorageError("Invalid asset checksum")
        key = f"sha256/{expected_sha256[:2]}/{expected_sha256}.glb"
        destination = self.resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, length = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                while chunk := source.read(1024 * 1024):
                    length += len(chunk)
                    if length > MAX_OBJECT_BYTES:
                        raise StorageError("Asset exceeds 128 MiB")
                    digest.update(chunk)
                    temporary.write(chunk)
                if not length or digest.hexdigest() != expected_sha256:
                    raise StorageError("Uploaded bytes do not match the inspected checksum")
                temporary.flush()
                os.fsync(temporary.fileno())
            except BaseException:
                temporary.close()
                path.unlink(missing_ok=True)
                raise
        try:
            # Hard linking is atomic and fails if the destination already exists.
            # It never exposes partially copied or overwritten immutable bytes.
            try:
                os.link(path, destination)
            except FileExistsError:
                self.verify(key, expected_sha256)
            if os.name != "nt":
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            path.unlink(missing_ok=True)
        self.verify(key, expected_sha256)
        return key

    @contextmanager
    def stage(self, source: BinaryIO, max_bytes: int = MAX_OBJECT_BYTES) -> Iterator[Path]:
        staging = self.root / ".staging"
        staging.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                length = 0
                while chunk := source.read(1024 * 1024):
                    length += len(chunk)
                    if length > max_bytes:
                        raise StorageError("Upload exceeds the configured size limit")
                    temporary.write(chunk)
            except BaseException:
                temporary.close()
                path.unlink(missing_ok=True)
                raise
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)
