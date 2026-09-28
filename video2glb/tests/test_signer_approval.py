from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src.qc.signer_approval import canonical_approval_bytes, verify_signer_approval


def _signed_approval() -> tuple[dict, dict]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    approval = {
        "schema_version": "1.0",
        "gloss": "CHANGE",
        "signer_verdict": "PASS",
        "isl_verified": True,
        "reviewer": "Signer One",
        "reviewed_at": "2026-09-04T10:00:00+05:30",
        "collision_review": {"status": "PASS", "method": "full clip"},
        "hash_binding": {
            "source_video_sha256": "a" * 64,
            "glb_sha256": "b" * 64,
            "comparison_video_sha256": "c" * 64,
        },
    }
    approval["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "signer-one-2026",
        "value_base64": base64.b64encode(
            private_key.sign(canonical_approval_bytes(approval))
        ).decode("ascii"),
    }
    registry = {
        "keys": {
            "signer-one-2026": {
                "reviewer": "Signer One",
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "active": True,
            }
        }
    }
    return approval, registry


def test_valid_signature_returns_sanitized_verified_approval():
    approval, registry = _signed_approval()
    approval["untrusted_extra"] = "discard me"

    verified = verify_signer_approval(approval, registry)

    assert verified["signature_verification"]["verified"] is True
    assert verified["signature_verification"]["status"] == "PASS"
    assert "untrusted_extra" not in verified


@pytest.mark.parametrize("mutation", ["meaning", "hash", "reviewer"])
def test_signed_content_tampering_fails_closed(mutation: str):
    approval, registry = _signed_approval()
    if mutation == "meaning":
        approval["signer_verdict"] = "FAIL"
    elif mutation == "hash":
        approval["hash_binding"]["glb_sha256"] = "d" * 64
    else:
        approval["reviewer"] = "Another Person"

    with pytest.raises(ValueError):
        verify_signer_approval(approval, registry)


def test_unknown_or_disabled_key_fails_closed():
    approval, registry = _signed_approval()
    registry["keys"]["signer-one-2026"]["active"] = False

    with pytest.raises(ValueError, match="not active"):
        verify_signer_approval(approval, registry)


def test_missing_signature_fails_closed():
    approval, registry = _signed_approval()
    del approval["signature"]

    with pytest.raises(ValueError, match="requires a signature"):
        verify_signer_approval(approval, registry)
