"""Cryptographic verification for signer-owned production approvals.

The converter must never trust reviewer identity or approval fields merely
because they appeared in a local JSON file.  This module verifies an Ed25519
signature against an operator-managed registry and returns an allowlisted,
sanitized record with a system-generated verification result.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SIGNED_FIELDS = (
    "schema_version",
    "gloss",
    "signer_verdict",
    "isl_verified",
    "reviewer",
    "reviewed_at",
    "collision_review",
    "hash_binding",
    "notes",
    "review_method",
)


def canonical_approval_bytes(approval: Mapping[str, Any]) -> bytes:
    """Return the deterministic byte string covered by an approval signature."""

    payload = {name: approval.get(name) for name in SIGNED_FIELDS if name in approval}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_signer_approval(
    approval: Mapping[str, Any],
    trusted_registry: Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    """Verify and sanitize one signer approval, raising on any trust failure."""

    if not isinstance(approval, Mapping):
        raise ValueError("Signer approval must be a JSON object.")
    registry = _load_registry(trusted_registry)
    signature = approval.get("signature")
    if not isinstance(signature, Mapping):
        raise ValueError("Signer approval requires a signature object.")

    algorithm = signature.get("algorithm")
    key_id = signature.get("key_id")
    encoded_signature = signature.get("value_base64")
    if algorithm != "Ed25519":
        raise ValueError("Signer approval signature.algorithm must be Ed25519.")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("Signer approval signature.key_id is required.")
    if not isinstance(encoded_signature, str) or not encoded_signature.strip():
        raise ValueError("Signer approval signature.value_base64 is required.")

    keys = registry.get("keys")
    if not isinstance(keys, Mapping):
        raise ValueError("Trusted signer registry requires a keys object.")
    trusted = keys.get(key_id)
    if not isinstance(trusted, Mapping):
        raise ValueError(f"Signer key is not trusted: {key_id}")
    if trusted.get("active") is not True:
        raise ValueError(f"Signer key is not active: {key_id}")

    trusted_reviewer = trusted.get("reviewer")
    supplied_reviewer = approval.get("reviewer")
    if not isinstance(trusted_reviewer, str) or not trusted_reviewer.strip():
        raise ValueError(f"Trusted signer key has no reviewer identity: {key_id}")
    if supplied_reviewer != trusted_reviewer:
        raise ValueError("Signer approval reviewer does not match the trusted key owner.")

    try:
        public_key_bytes = base64.b64decode(
            str(trusted.get("public_key_base64") or ""), validate=True
        )
        signature_bytes = base64.b64decode(encoded_signature, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature_bytes, canonical_approval_bytes(approval))
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise ValueError("Signer approval signature verification failed.") from exc

    sanitized = {
        name: approval.get(name)
        for name in SIGNED_FIELDS
        if name in approval
    }
    sanitized["signature"] = {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "value_base64": encoded_signature,
    }
    sanitized["signature_verification"] = {
        "status": "PASS",
        "verified": True,
        "algorithm": "Ed25519",
        "key_id": key_id,
        "reviewer": trusted_reviewer,
    }
    return sanitized


def _load_registry(value: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"Trusted signer registry not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Trusted signer registry is invalid JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Trusted signer registry must contain a JSON object.")
    return payload
