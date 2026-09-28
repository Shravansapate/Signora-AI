import hashlib
import io

import pytest

from app.storage import LocalAssetStore, StorageError


def test_immutable_put_idempotent_and_checksum_verified(tmp_path):
    store = LocalAssetStore(tmp_path)
    content = b"immutable test bytes"
    checksum = hashlib.sha256(content).hexdigest()
    key = store.put(io.BytesIO(content), checksum)
    assert store.put(io.BytesIO(content), checksum) == key
    assert store.verify(key, checksum).read_bytes() == content
    assert len(list(tmp_path.rglob("*.glb"))) == 1


def test_failed_copy_cannot_replace_existing_object(tmp_path):
    store = LocalAssetStore(tmp_path)
    checksum = hashlib.sha256(b"correct").hexdigest()
    key = store.put(io.BytesIO(b"correct"), checksum)
    with pytest.raises(StorageError, match="checksum"):
        store.put(io.BytesIO(b"wrong"), checksum)
    assert store.verify(key, checksum).read_bytes() == b"correct"


@pytest.mark.parametrize("key", ["../outside", "C:/secret", "sha256/../secret", "/root/file"])
def test_untrusted_keys_rejected(tmp_path, key):
    with pytest.raises(StorageError):
        LocalAssetStore(tmp_path).resolve(key)


def test_staging_is_bounded_and_cleaned_on_error(tmp_path):
    store = LocalAssetStore(tmp_path)
    with pytest.raises(StorageError):
        with store.stage(io.BytesIO(b"too large"), 2):
            pytest.fail("Oversized source was admitted")
    assert not list((tmp_path / ".staging").iterdir())
    with pytest.raises(RuntimeError):
        with store.stage(io.BytesIO(b"valid")) as staged:
            assert staged.exists()
            raise RuntimeError("interrupted")
    assert not list((tmp_path / ".staging").iterdir())


def test_missing_or_corrupt_object_fails_verification(tmp_path):
    store = LocalAssetStore(tmp_path)
    checksum = hashlib.sha256(b"expected").hexdigest()
    key = store.put(io.BytesIO(b"expected"), checksum)
    store.resolve(key).write_bytes(b"corrupted")
    with pytest.raises(StorageError, match="checksum"):
        store.verify(key, checksum)
    store.resolve(key).unlink()
    with pytest.raises(StorageError, match="unavailable"):
        store.verify(key, checksum)
