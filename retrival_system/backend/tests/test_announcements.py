"""Real registry/recipe integration; approvals below are isolated synthetic fixtures."""
# ruff: noqa: F811 -- isolated module PostgreSQL fixture reuse.

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_lifecycle import (  # noqa: F401
    approve_fixture,
    history,
    lifecycle_clients,
    request_ok,
    switch,
    variant,
)
from test_registry import registry_database  # noqa: F401

from alembic import command
from app.models import VoiceTranscript
from app.registry import stage_motion
from app.storage import LocalAssetStore


@pytest.fixture(scope="module")
def announcement_setup(registry_database, lifecycle_clients):
    settings, _, sessions = registry_database
    client, headers = lifecycle_clients
    library = Path(__file__).resolve().parents[2] / "metadata_json and glb"
    assets = {}
    for name in ("Train", "1_One", "Zero", "Arrive"):
        with sessions() as session:
            staged = stage_motion(
                library / f"metadata/{name}.metadata.json",
                library / f"glb/{name}.glb",
                "synthetic-phase4-fixture",
                session,
                LocalAssetStore(settings.storage_root),
            )
        approve_fixture(client, headers, staged["concept_id"], staged["motion_version_id"])
        switch(client, headers, staged["concept_id"], staged["motion_version_id"])
        assets[name] = staged
    request_ok(
        client,
        headers,
        "/api/v1/admin/stations/TEST",
        {
            "expected_revision": 0,
            "reason": "isolated synthetic station",
            "definition": {
                "name": "Isolated station",
                "platforms": ["1", "3", "4"],
                "places": [{"id": "SRC", "name": "Delhi"}, {"id": "DST", "name": "Mumbai"}],
            },
        },
    )
    avatar = client.get("/api/v1/review/avatars", headers=headers["reviewer"]).json()["items"][0][
        "id"
    ]
    ids = {name: row["concept_id"] for name, row in assets.items()}
    definition = {
        "intent": "TRAIN_ARRIVAL",
        "temporal_state": "ARRIVING_NOW",
        "polarity": "POSITIVE",
        "avatar_profile_id": avatar,
        "slot_types": {
            "train_identifier": "TRAIN_IDENTIFIER",
            "platform_identifier": "PLATFORM_IDENTIFIER",
        },
        "recipe": [
            {"kind": "CONCEPT", "concept_id": ids["Train"], "covers": ["intent"], "group": "train"},
            {
                "kind": "SLOT",
                "slot": "train_identifier",
                "group": "train",
                "policy": {
                    "mode": "EXACT_VALUES",
                    "units": {
                        "00110": [ids["Zero"], ids["Zero"], ids["1_One"], ids["1_One"], ids["Zero"]]
                    },
                },
            },
            {
                "kind": "CONCEPT",
                "concept_id": ids["Arrive"],
                "covers": ["temporal_state", "polarity"],
                "group": "event",
            },
            {
                "kind": "SLOT",
                "slot": "platform_identifier",
                "group": "platform",
                "policy": {"mode": "EXACT_VALUES", "units": {"1": [ids["1_One"]]}},
            },
        ],
        "safe_after_groups": ["train", "event", "platform"],
    }
    template = request_ok(
        client,
        headers,
        "/api/v1/admin/templates",
        {
            "template_key": "synthetic-arrival-fixture",
            "expected_version": 0,
            "definition": definition,
            "reason": "Synthetic recipe for engineering tests, not ISL evidence",
        },
        expected=201,
    )
    return assets, template


def payload(**changes):
    return {
        "request_id": str(uuid4()),
        "station_id": "TEST",
        "input_type": "TEXT",
        "text": "Train number 00110 is arriving on platform 1.",
        **changes,
    }


@pytest.fixture(scope="module")
def approved_template(announcement_setup, lifecycle_clients):
    assets, template = announcement_setup
    client, headers = lifecycle_clients
    pending = request_ok(client, headers, "/api/v1/translate", payload())
    assert pending["status"] == "NEEDS_REVIEW" and pending["manifest"] is None
    example = {
        "intent": "TRAIN_ARRIVAL",
        "temporal_state": "ARRIVING_NOW",
        "slots": {"train_identifier": "00110", "platform_identifier": "1"},
    }
    return assets, review_and_activate(client, headers, template, example)


def review_and_activate(client, headers, template, example):
    preview = request_ok(
        client,
        headers,
        f"/api/v1/review/templates/{template['id']}/prepare",
        {"station_id": "TEST", "example": example},
        role="reviewer",
    )
    assert preview["status"] == "READY"
    reviewed = request_ok(
        client,
        headers,
        f"/api/v1/review/templates/{template['id']}",
        {
            "expected_revision": template["revision"],
            "definition_hash": template["definition_hash"],
            "decision": "APPROVED",
            "preview_manifest_ids": [preview["manifest"]["manifest_id"]],
            "rendered_review_confirmed": True,
            "evidence": "synthetic isolated composition assertion",
            "reason": "Engineering fixture only; no production approval",
        },
        role="reviewer",
    )
    active = request_ok(
        client,
        headers,
        f"/api/v1/admin/templates/{template['id']}/activation",
        {
            "expected_revision": reviewed["revision"],
            "enabled": True,
            "reason": "isolated fixture",
        },
    )
    return active


def test_development_uses_existing_reviewed_recipe_for_equivalent_semantics(
    approved_template, registry_database
):
    from types import SimpleNamespace

    from app.announcement_schema import AnnouncementInput
    from app.announcements import translate

    assets, template = approved_template
    settings, _, sessions = registry_database
    identity = SimpleNamespace(subject="admin", roles={"admin"}, station_ids=set())
    # Extended wording maps to the exact existing semantic contract; no draft
    # NOW sign or guessed word order may replace this reviewed fixture recipe.
    with sessions() as session:
        result = translate(
            session,
            LocalAssetStore(settings.storage_root),
            AnnouncementInput.model_validate(
                payload(text="Train 00110 is arriving on platform number 1")
            ),
            identity,
            demo_mode_enabled=True,
        )
    assert result.status == "READY"
    assert result.retrieval["construction"]["source"] == "REVIEWED_RECIPE"
    assert result.retrieval["construction"]["template_version_id"] == template["id"]
    assert [str(item.motion_version_id) for item in result.manifest.items] == [
        assets[name]["motion_version_id"]
        for name in ["Train", "Zero", "Zero", "1_One", "1_One", "Zero", "Arrive", "1_One"]
    ]
    assert result.retrieval["retrieval_tokens"] == result.retrieval["gloss_tokens"]


def test_complete_template_order_pinning_private_preview_and_idempotency(
    approved_template, lifecycle_clients, registry_database
):
    assets, template = approved_template
    client, headers = lifecycle_clients
    request = payload()
    result = request_ok(client, headers, "/api/v1/translate", request)
    assert result["status"] == "READY", result
    manifest = result["manifest"]
    assert manifest["schema_version"] == 3 and manifest["operational"] is False
    assert manifest["purpose"] == "ANNOUNCEMENT_PREVIEW"
    assert manifest["safe_boundaries"] == [5, 6, 7]
    names = ["Train", "Zero", "Zero", "1_One", "1_One", "Zero", "Arrive", "1_One"]
    assert [item["motion_version_id"] for item in manifest["items"]] == [
        assets[n]["motion_version_id"] for n in names
    ]
    assert request_ok(client, headers, "/api/v1/translate", request) == result
    path = f"/api/v1/playback/{manifest['manifest_id']}"
    assert client.get(path, headers=headers["admin"]).status_code == 200
    assert client.get(path, headers=headers["reviewer"]).status_code == 403
    assert (
        client.get(manifest["items"][0]["asset_url"], headers=headers["admin"]).status_code == 200
    )
    changed = client.post(
        "/api/v1/translate", headers=headers["admin"], json={**request, "text": "other"}
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    with registry_database[1].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM announcement_manifest_refs WHERE manifest_id=:id"),
                {"id": manifest["manifest_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM template_motion_bindings WHERE template_version_id=:id"),
                {"id": template["id"]},
            )
            == 4
        )


def test_text_structured_and_confirmed_voice_share_meaning(
    approved_template, lifecycle_clients, registry_database
):
    client, headers = lifecycle_clients
    typed = request_ok(client, headers, "/api/v1/translate", payload())
    structured = payload(
        input_type="STRUCTURED",
        structured={
            "intent": "TRAIN_ARRIVAL",
            "temporal_state": "ARRIVING_NOW",
            "slots": {"train_identifier": "00110", "platform_identifier": "1"},
        },
    )
    del structured["text"]
    structured = request_ok(client, headers, "/api/v1/translate", structured)
    with registry_database[2]() as session, session.begin():
        transcript = VoiceTranscript(
            owner_subject="admin",
            text=typed["original_text"],
            audio_sha256="a" * 64,
            asr_metadata={
                "final": True,
                "low_confidence": True,
                "fixture": "synthetic finalized ASR receipt",
            },
            valid_until=datetime.now(UTC) + timedelta(minutes=15),
        )
        session.add(transcript)
        session.flush()
        transcript_id = str(transcript.id)
    unconfirmed = request_ok(
        client,
        headers,
        "/api/v1/translate",
        payload(input_type="VOICE", transcript_id=transcript_id),
    )
    assert unconfirmed["status"] == "NEEDS_CONFIRMATION" and unconfirmed["manifest"] is None
    voice = request_ok(
        client,
        headers,
        "/api/v1/translate",
        payload(input_type="VOICE", transcript_id=transcript_id, transcript_confirmed=True),
    )
    assert voice["status"] == structured["status"] == typed["status"] == "READY"
    assert voice["meaning_hash"] == typed["meaning_hash"] == structured["meaning_hash"]
    assert [i["motion_version_id"] for i in voice["manifest"]["items"]] == [
        i["motion_version_id"] for i in typed["manifest"]["items"]
    ]
    stolen = client.post(
        "/api/v1/translate",
        headers=headers["reviewer"],
        json=payload(input_type="VOICE", transcript_id=transcript_id, transcript_confirmed=True),
    )
    assert stolen.status_code == 403


@pytest.mark.parametrize(
    "source,status",
    [
        ("Train 00110 is arriving on platform 3.", "NEEDS_REVIEW"),
        ("Train 00111 is arriving on platform 1.", "NEEDS_REVIEW"),
        ("Train 00110 is not arriving on platform 1.", "NEEDS_REVIEW"),
        ("Train 00110 has arrived on platform 1.", "NEEDS_REVIEW"),
        ("Train 00110 is departing on platform 1.", "NEEDS_REVIEW"),
        ("Train 00110 is arriving on platform 99.", "NEEDS_CONFIRMATION"),
        ("Train 00110 is arriving on platform 1 and leaving soon.", "UNSUPPORTED"),
    ],
)
def test_incomplete_or_different_meaning_never_reuses_nearby_template(
    source, status, approved_template, lifecycle_clients
):
    result = request_ok(*lifecycle_clients, "/api/v1/translate", payload(text=source))
    assert result["status"] == status and result["manifest"] is None
    assert result["original_text"] == source and result["issues"]


def test_station_authorization_and_changed_configuration_invalidates_preview(
    approved_template, lifecycle_clients
):
    client, headers = lifecycle_clients
    assert (
        client.post("/api/v1/translate", headers=headers["display"], json=payload()).status_code
        == 403
    )
    assert (
        client.post("/api/v1/translate", headers=headers["operator"], json=payload()).status_code
        == 403
    )
    result = request_ok(client, headers, "/api/v1/translate", payload())
    capability = client.get("/api/v1/input/capabilities", headers=headers["admin"]).json()
    station = next(row for row in capability["stations"] if row["id"] == "TEST")
    request_ok(
        client,
        headers,
        "/api/v1/admin/stations/TEST",
        {
            "expected_revision": station["revision"],
            "definition": station["definition"],
            "reason": "isolated station revision",
        },
    )
    response = client.get(
        f"/api/v1/playback/{result['manifest']['manifest_id']}", headers=headers["admin"]
    )
    assert response.status_code == 409


def test_missing_storage_fails_whole_message_and_keeps_prior_receipt(
    approved_template, lifecycle_clients, registry_database
):
    client, headers = lifecycle_clients
    store = LocalAssetStore(registry_database[0].storage_root)
    result = request_ok(client, headers, "/api/v1/translate", payload())
    digest = result["manifest"]["items"][1]["sha256"]
    path = store.resolve(f"sha256/{digest[:2]}/{digest}.glb")
    displaced = path.with_suffix(".isolated-unavailable")
    path.rename(displaced)
    try:
        failed = request_ok(client, headers, "/api/v1/translate", payload())
        assert failed["status"] == "ASSET_UNAVAILABLE" and failed["manifest"] is None
    finally:
        displaced.rename(path)


def test_template_immutability_and_downgrade_guards_retained_plans(
    approved_template, registry_database
):
    _, engine, _ = registry_database
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE announcement_templates SET definition='{}'::jsonb"))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="Retained phase 4 plans"):
        command.downgrade(config, "0003_lifecycle_import")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0010_display_credentials"
        )


def test_dependency_deactivation_blocks_new_and_retained_previews(
    approved_template, lifecycle_clients
):
    assets, _ = approved_template
    client, headers = lifecycle_clients
    prepared = request_ok(client, headers, "/api/v1/translate", payload())
    cid = assets["Arrive"]["concept_id"]
    concept = client.get(f"/api/v1/review/signs/{cid}", headers=headers["reviewer"]).json()[
        "concept"
    ]
    changed = request_ok(
        client,
        headers,
        f"/api/v1/admin/signs/{cid}/deactivate",
        {"expected_revision": concept["revision"], "reason": "isolated dependency change"},
    )
    try:
        assert (
            request_ok(client, headers, "/api/v1/translate", payload())["status"] == "NEEDS_REVIEW"
        )
        assert (
            client.get(
                f"/api/v1/playback/{prepared['manifest']['manifest_id']}", headers=headers["admin"]
            ).status_code
            == 409
        )
    finally:
        request_ok(
            client,
            headers,
            f"/api/v1/admin/signs/{cid}/reactivate",
            {"expected_revision": changed["revision"], "reason": "restore fixture"},
        )


def test_concurrent_input_retry_creates_one_manifest(approved_template, lifecycle_clients):
    request = payload()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: request_ok(*lifecycle_clients, "/api/v1/translate", request), range(4)
            )
        )
    assert responses[0]["status"] == "READY"
    assert all(result == responses[0] for result in responses)


def test_arbitrary_motion_preview_cannot_approve_a_recipe(approved_template, lifecycle_clients):
    assets, template = approved_template
    client, headers = lifecycle_clients
    preview = request_ok(
        client,
        headers,
        "/api/v1/review/prepare",
        {
            "avatar_motion_version_id": assets["Train"]["motion_version_id"],
            "motion_version_ids": [assets["Train"]["motion_version_id"]],
        },
        role="reviewer",
    )
    rejected = client.post(
        f"/api/v1/review/templates/{template['id']}",
        headers=headers["reviewer"],
        json={
            "expected_revision": template["revision"],
            "definition_hash": template["definition_hash"],
            "decision": "APPROVED",
            "preview_manifest_ids": [preview["manifest"]["manifest_id"]],
            "rendered_review_confirmed": True,
            "evidence": "isolated invalid review",
            "reason": "isolated test",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "CONSTRUCTION_PREVIEW_REQUIRED"


def test_voice_role_format_and_actual_upload_size_are_checked_before_asr(lifecycle_clients):
    from app.audio import MAX_AUDIO_BYTES

    client, headers = lifecycle_clients
    for role, status in [("display", 403), ("admin", 422)]:
        response = client.post(
            "/api/v1/voice/transcribe",
            headers=headers[role],
            files={"audio": ("bad.wav", b"invalid", "audio/wav")},
        )
        assert response.status_code == status
    # Header-based checks alone cannot bound streamed multipart bodies.
    oversized = client.post(
        "/api/v1/voice/transcribe",
        headers={**headers["admin"], "Content-Length": "12"},
        files={"audio": ("large.wav", b"x" * (MAX_AUDIO_BYTES + 65537), "audio/wav")},
    )
    assert oversized.status_code == 413
    json_body = client.post(
        "/api/v1/translate",
        headers={**headers["admin"], "Content-Length": "12", "Content-Type": "application/json"},
        content=b"x" * 32769,
    )
    assert json_body.status_code == 413


@pytest.mark.parametrize("event", ["DEPARTURE", "PLATFORM_CHANGE"])
def test_complete_departure_and_platform_change_preserve_all_values(
    event, approved_template, lifecycle_clients
):
    # These recipes prove compiler mechanics only. Reused fixture motions are not ISL recipes.
    assets, arrival = approved_template
    client, headers = lifecycle_clients
    definition = deepcopy(arrival["definition"])
    if event == "DEPARTURE":
        definition.update(intent="TRAIN_DEPARTURE", temporal_state="DEPARTING_NOW")
        source = "Train number 00110 is departing from platform 1."
        expected = {"train_identifier": "00110", "platform_identifier": "1"}
    else:
        definition.update(intent="PLATFORM_CHANGE", temporal_state="CURRENT_CHANGE")
        definition["slot_types"] = {
            "train_identifier": "TRAIN_IDENTIFIER",
            "old_platform": "PLATFORM_IDENTIFIER",
            "new_platform": "PLATFORM_IDENTIFIER",
        }
        definition["recipe"][-1:] = [
            {
                "kind": "SLOT",
                "slot": "old_platform",
                "group": "platform_change",
                "policy": {"mode": "EXACT_VALUES", "units": {"3": [assets["Zero"]["concept_id"]]}},
            },
            {
                "kind": "SLOT",
                "slot": "new_platform",
                "group": "platform_change",
                "policy": {"mode": "EXACT_VALUES", "units": {"4": [assets["1_One"]["concept_id"]]}},
            },
        ]
        definition["safe_after_groups"] = ["train", "event", "platform_change"]
        source = "Platform for train 00110 has changed from 3 to 4."
        expected = {"train_identifier": "00110", "old_platform": "3", "new_platform": "4"}
    example = {
        "intent": definition["intent"],
        "temporal_state": definition["temporal_state"],
        "slots": expected,
    }
    template = request_ok(
        client,
        headers,
        "/api/v1/admin/templates",
        {
            "template_key": "isolated-" + event.lower(),
            "expected_version": 0,
            "definition": definition,
            "reason": "Synthetic engineering recipe, not language evidence",
        },
        expected=201,
    )
    active = review_and_activate(client, headers, template, example)
    result = request_ok(client, headers, "/api/v1/translate", payload(text=source))
    assert result["status"] == "READY", result
    assert {k: v["value"] for k, v in result["meaning"]["slots"].items()} == expected
    assert result["manifest"]["announcement"]["template_version_id"] == active["id"]
    assert result["manifest"]["caption_text"] == source
    if event == "PLATFORM_CHANGE":
        assert result["manifest"]["safe_boundaries"] == [5, 6, 8]
        assert [i["motion_version_id"] for i in result["manifest"]["items"][-2:]] == [
            assets[n]["motion_version_id"] for n in ("Zero", "1_One")
        ]
        reverse = request_ok(
            client, headers, "/api/v1/translate", payload(text=source.replace("3 to 4", "4 to 3"))
        )
        assert reverse["status"] == "NEEDS_REVIEW" and reverse["manifest"] is None


def test_quality_replacement_requires_fresh_composition_then_pins_new_bytes(
    approved_template, lifecycle_clients, registry_database, tmp_path
):
    assets, template = approved_template
    client, headers = lifecycle_clients
    cid = assets["Arrive"]["concept_id"]
    before = history(client, headers, cid)["concept"]
    old = request_ok(client, headers, "/api/v1/translate", payload())
    paths = variant(tmp_path, before["semantic_key"], source="Arrive", marker="quality-only")
    with registry_database[2]() as session:
        candidate = stage_motion(
            *paths,
            "isolated quality fixture",
            session,
            LocalAssetStore(registry_database[0].storage_root),
        )
    assert candidate["concept_id"] == cid
    approve_fixture(client, headers, cid, candidate["motion_version_id"])
    switch(client, headers, cid, candidate["motion_version_id"])
    assert (
        history(client, headers, cid)["concept"]["semantic_revision"] == before["semantic_revision"]
    )
    blocked = request_ok(client, headers, "/api/v1/translate", payload())
    assert (
        blocked["status"] == "NEEDS_REVIEW" and blocked["issues"][0]["code"] == "STALE_COMPOSITION"
    )
    assert (
        client.get(
            f"/api/v1/playback/{old['manifest']['manifest_id']}", headers=headers["admin"]
        ).status_code
        == 409
    )
    review_and_activate(
        client,
        headers,
        template,
        {
            "intent": "TRAIN_ARRIVAL",
            "temporal_state": "ARRIVING_NOW",
            "slots": {"train_identifier": "00110", "platform_identifier": "1"},
        },
    )
    new = request_ok(client, headers, "/api/v1/translate", payload())
    assert new["status"] == "READY"
    assert new["manifest"]["items"][6]["motion_version_id"] == candidate["motion_version_id"]
    assert new["manifest"]["items"][6]["concept_id"] == cid
    assert new["manifest"]["items"][6]["sha256"] != old["manifest"]["items"][6]["sha256"]
    assert (
        new["manifest"]["announcement"]["definition_hash"]
        == old["manifest"]["announcement"]["definition_hash"]
    )
