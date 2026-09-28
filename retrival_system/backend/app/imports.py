"""Metadata-driven imports with immutable input snapshots and fenced recoverable leases."""

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.assets.khronos import KhronosValidationError
from app.assets.metadata import load_metadata, resolve_asset
from app.lifecycle import LifecycleError, _event, require
from app.models import ImportItem, ImportJob
from app.registry import RegistryConflict, stage_motion
from app.storage import LocalAssetStore, StorageError

logger = logging.getLogger("signora.imports")
LEASE_SECONDS = 300
MAX_SOURCE_ITEMS = 2000


def source_root(settings, alias):
    require(
        alias in settings.import_roots, "UNKNOWN_SOURCE", "The source alias is not configured", 422
    )
    root = settings.import_roots[alias].resolve(strict=True)
    for folder in ("metadata", "glb"):
        child = (root / folder).resolve(strict=True)
        require(
            child.is_relative_to(root) and child.is_dir(),
            "INVALID_SOURCE",
            "Source folders must remain inside the configured root",
            422,
        )
    return root, hashlib.sha256(str(root).casefold().encode()).hexdigest()


def metadata_path(root, name):
    directory = (root / "metadata").resolve(strict=True)
    path = (directory / name).resolve(strict=True)
    require(
        Path(name).name == name and path.parent == directory and path.is_file(),
        "INVALID_SOURCE_PATH",
        "Metadata must be a regular file within its source folder",
        422,
    )
    return path


def _refresh(session, job):
    if job.state == "PAUSED":
        return
    outstanding = session.scalar(
        select(func.count())
        .select_from(ImportItem)
        .where(
            ImportItem.job_id == job.id, ImportItem.state.in_(["DISCOVERED", "RUNNING", "RETRY"])
        )
    )
    job.state = "RUNNING" if outstanding else "COMPLETE"
    job.updated_at = datetime.now(UTC)


def create_import(session, settings, source_alias, request_id, actor):
    # Repeating a request ID returns the original input snapshot, even if source files changed.
    with session.begin():
        prior = session.scalar(select(ImportJob).where(ImportJob.request_id == request_id))
        if prior:
            require(
                prior.source_alias == source_alias and prior.owner_subject == actor,
                "IDEMPOTENCY_CONFLICT",
                "Request ID belongs to a different import request",
            )
            return {"job_id": str(prior.id), "state": prior.state, "unchanged_request": True}
    root, root_hash = source_root(settings, source_alias)
    names = []
    for path in (root / "metadata").iterdir():
        if path.name.casefold().endswith(".metadata.json"):
            names.append(path.name)
            require(
                len(names) <= MAX_SOURCE_ITEMS,
                "IMPORT_LIMIT",
                "A source snapshot is limited to 2000 metadata files",
                422,
            )
    require(bool(names), "EMPTY_SOURCE", "No metadata files found in the configured source", 422)
    items = []
    for name in sorted(names):
        item = {"id": uuid4(), "metadata_name": name, "state": "DISCOVERED"}
        try:
            path = metadata_path(root, name)
            metadata, digest, _ = load_metadata(path)
            item.update(
                metadata_sha256=digest,
                semantic_key=metadata.motion_identity.motion_code,
                asset_sha256=metadata.file_integrity.glb_sha256,
            )
        except (ValueError, OSError):
            item.update(
                state="FAILED",
                error_code="METADATA_INVALID",
                error_detail="Correct the metadata schema or source path and create a new snapshot",
            )
        items.append(item)
    with session.begin():
        job_id = uuid4()
        inserted = session.scalar(
            insert(ImportJob)
            .values(
                id=job_id,
                request_id=request_id,
                source_alias=source_alias,
                source_root_hash=root_hash,
                owner_subject=actor,
            )
            .on_conflict_do_nothing(index_elements=["request_id"])
            .returning(ImportJob.id)
        )
        if inserted is None:
            prior = session.scalar(select(ImportJob).where(ImportJob.request_id == request_id))
            require(
                prior.source_alias == source_alias and prior.owner_subject == actor,
                "IDEMPOTENCY_CONFLICT",
                "Request ID belongs to a different import request",
            )
            return {"job_id": str(prior.id), "state": prior.state, "unchanged_request": True}
        session.add_all([ImportItem(job_id=job_id, **item) for item in items])
        session.flush()
        job = session.get(ImportJob, job_id)
        _refresh(session, job)
        _event(
            session,
            actor,
            "IMPORT_CREATED",
            job_id,
            {"source_alias": source_alias, "discovered": len(items)},
        )
        return {"job_id": str(job_id), "state": job.state, "unchanged_request": False}


def import_status(session, job_id, limit=100, offset=0):
    job = session.get(ImportJob, job_id)
    require(job is not None, "NOT_FOUND", "Import job not found", 404)
    require(1 <= limit <= 100 and offset >= 0, "PAGINATION", "Invalid pagination", 422)
    counts = dict(
        session.execute(
            text("""
      SELECT count(*) AS discovered,
        count(*) FILTER (WHERE i.state='STAGED') AS staged,
        count(*) FILTER (WHERE i.state='UNCHANGED') AS unchanged,
        count(*) FILTER (WHERE i.state='FAILED') AS failed,
        count(*) FILTER (WHERE i.state IN ('DISCOVERED','RETRY')) AS waiting,
        count(*) FILTER (WHERE i.state='RUNNING') AS running,
        count(*) FILTER (WHERE e.id IS NOT NULL) AS eligible,
        count(*) FILTER (WHERE m.lifecycle_status='ACTIVE') AS active,
        count(*) FILTER (WHERE m.linguistic_review_status='REJECTED' OR
          m.composition_review_status='REJECTED' OR m.lifecycle_status='REJECTED') AS rejected,
        count(*) FILTER (WHERE m.id IS NOT NULL AND (m.linguistic_review_status='PENDING' OR
          m.composition_review_status='PENDING' OR c.meaning_status='PENDING'
          OR a.status='PENDING')) AS pending_review
      FROM import_items i LEFT JOIN motion_versions m ON m.id=i.motion_version_id
      LEFT JOIN sign_concepts c ON c.id=m.concept_id
      LEFT JOIN avatar_profiles a ON a.id=m.avatar_profile_id
      LEFT JOIN eligible_motion_versions e ON e.id=m.id WHERE i.job_id=:id
    """),
            {"id": job_id},
        )
        .mappings()
        .one()
    )
    rows = session.scalars(
        select(ImportItem)
        .where(ImportItem.job_id == job_id)
        .order_by(ImportItem.metadata_name)
        .limit(limit)
        .offset(offset)
    )
    fields = (
        "id",
        "metadata_name",
        "metadata_sha256",
        "semantic_key",
        "asset_sha256",
        "state",
        "attempts",
        "attempt_limit",
        "error_code",
        "error_detail",
        "retryable",
        "motion_version_id",
        "updated_at",
    )
    return {
        "job_id": str(job.id),
        "source_alias": job.source_alias,
        "state": job.state,
        "counts": counts,
        "items": [{field: getattr(row, field) for field in fields} for row in rows],
    }


def control_import(session, job_id, actor, *, pause=False, retry_failed=False):
    with session.begin():
        job = session.get(ImportJob, job_id, with_for_update=True)
        require(job is not None, "NOT_FOUND", "Import job not found", 404)
        job.state = "PAUSED" if pause else "RUNNING"
        if retry_failed and not pause:
            session.execute(
                update(ImportItem)
                .where(
                    ImportItem.job_id == job_id,
                    ImportItem.state == "FAILED",
                    ImportItem.retryable.is_(True),
                )
                .values(
                    state="RETRY", attempt_limit=ImportItem.attempts + 3, available_at=func.now()
                )
            )
        _refresh(session, job)
        _event(
            session,
            actor,
            "IMPORT_PAUSED" if pause else "IMPORT_RESUMED",
            job_id,
            {"retry_failed": retry_failed, "state": job.state},
        )
        return {"job_id": str(job.id), "state": job.state}


def _claim(sessions, job_id=None):
    now = datetime.now(UTC)
    runnable = or_(
        ImportItem.state == "DISCOVERED",
        (ImportItem.state == "RETRY") & (ImportItem.available_at <= now),
        (ImportItem.state == "RUNNING") & (ImportItem.lease_until <= now),
    )
    with sessions() as session, session.begin():
        query = select(ImportJob).where(
            ImportJob.state.in_(["QUEUED", "RUNNING"]),
            exists(select(ImportItem.id).where(ImportItem.job_id == ImportJob.id, runnable)),
        )
        if job_id:
            query = query.where(ImportJob.id == job_id)
        job = session.scalar(
            query.order_by(ImportJob.created_at, ImportJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        item = session.scalar(
            select(ImportItem)
            .where(ImportItem.job_id == job.id, runnable)
            .order_by(ImportItem.metadata_name)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item.attempts >= item.attempt_limit:
            item.state = "FAILED"
            item.lease_token = item.lease_until = None
            item.error_code = "RETRY_EXHAUSTED"
            item.error_detail = (
                "Worker recovery exhausted its retry budget; explicitly resume retryable items"
            )
            item.retryable = True
            item.updated_at = now
            session.flush()
            _refresh(session, job)
            _event(
                session,
                job.owner_subject,
                "IMPORT_ITEM_RESULT",
                item.id,
                {
                    "job_id": str(job.id),
                    "state": item.state,
                    "attempts": item.attempts,
                    "error_code": item.error_code,
                },
            )
            return {"exhausted": True}
        item.attempts += 1
        item.state = "RUNNING"
        item.lease_token = uuid4()
        item.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        item.updated_at = now
        job.state = "RUNNING"
        job.updated_at = now
        return {
            "item_id": item.id,
            "job_id": job.id,
            "lease_token": item.lease_token,
            "metadata_name": item.metadata_name,
            "metadata_sha256": item.metadata_sha256,
            "source_alias": job.source_alias,
            "source_root_hash": job.source_root_hash,
            "actor": job.owner_subject,
        }


def _heartbeat(sessions, claim, stop):
    while not stop.wait(30):
        try:
            with sessions() as session, session.begin():
                result = session.execute(
                    update(ImportItem)
                    .where(
                        ImportItem.id == claim["item_id"],
                        ImportItem.state == "RUNNING",
                        ImportItem.lease_token == claim["lease_token"],
                    )
                    .values(lease_until=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
                )
                if not result.rowcount:
                    return
        except SQLAlchemyError:
            logger.warning("import_lease_heartbeat_failed")


def _finish(sessions, claim, result=None, failure=None):
    with sessions() as session, session.begin():
        job = session.get(ImportJob, claim["job_id"], with_for_update=True)
        item = session.get(ImportItem, claim["item_id"], with_for_update=True)
        if item.lease_token != claim["lease_token"] or item.state != "RUNNING":
            return (
                False  # A recovered worker owns this item; obsolete completion cannot overwrite it.
            )
        item.lease_token = item.lease_until = None
        item.updated_at = datetime.now(UTC)
        if failure:
            code, detail, retryable = failure
            item.error_code, item.error_detail, item.retryable = code, detail, retryable
            item.state = "RETRY" if retryable and item.attempts < item.attempt_limit else "FAILED"
            item.available_at = datetime.now(UTC) + timedelta(seconds=min(60, 2**item.attempts))
        else:
            item.state = result["status"]
            item.motion_version_id = result["motion_version_id"]
            item.error_code = item.error_detail = None
            item.retryable = False
        session.flush()
        _refresh(session, job)
        _event(
            session,
            claim["actor"],
            "IMPORT_ITEM_RESULT",
            item.id,
            {
                "job_id": str(job.id),
                "state": item.state,
                "attempts": item.attempts,
                "error_code": item.error_code,
            },
        )
        return True


def process_one(sessions, settings, *, job_id=None):
    claim = _claim(sessions, job_id)
    if claim is None:
        return False
    if claim.get("exhausted"):
        return True
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(sessions, claim, stop), daemon=True)
    heartbeat.start()
    began = time.monotonic()
    try:
        result, failure = None, None
        try:
            root, root_hash = source_root(settings, claim["source_alias"])
            require(
                root_hash == claim["source_root_hash"],
                "SOURCE_CHANGED",
                "Source configuration changed; create a new import snapshot",
            )
            path = metadata_path(root, claim["metadata_name"])
            metadata, digest, _ = load_metadata(path)
            require(
                digest == claim["metadata_sha256"],
                "SOURCE_CHANGED",
                "Metadata changed after discovery; create a new import snapshot",
            )
            asset = resolve_asset(metadata, root / "glb")
            with sessions() as session:
                result = stage_motion(
                    path,
                    asset,
                    claim["actor"],
                    session,
                    LocalAssetStore(settings.storage_root),
                    expected_metadata_sha256=claim["metadata_sha256"],
                )
        except LifecycleError as exc:
            failure = (exc.code, str(exc), False)
        except RegistryConflict:
            failure = (
                "METADATA_OR_IDENTITY_CONFLICT",
                "Review the changed identity or metadata; existing catalog versions were preserved",
                False,
            )
        except KhronosValidationError:
            failure = (
                "VALIDATOR_UNAVAILABLE",
                "The structural validator failed or timed out; retry after restoring the validator",
                True,
            )
        except StorageError:
            failure = (
                "STORAGE_UNAVAILABLE",
                "Check object storage availability and integrity before retrying",
                True,
            )
        except SQLAlchemyError:
            failure = (
                "DATABASE_UNAVAILABLE",
                "The registry transaction failed; retry safely using the pinned input",
                True,
            )
        except (ValueError, OSError):
            failure = (
                "ASSET_OR_METADATA_INVALID",
                "Correct missing files, source checksums, metadata or GLB validation errors "
                "and create a new snapshot",
                False,
            )
        except Exception as exc:
            logger.error("import_item_unexpected_failure: %s", type(exc).__name__)
            failure = (
                "INTERNAL_ERROR",
                "Unexpected ingestion failure; inspect server diagnostics before retrying",
                False,
            )
        _finish(sessions, claim, result, failure)
        logger.info(
            "import_item_finished item=%s elapsed_ms=%s",
            claim["item_id"],
            round((time.monotonic() - began) * 1000),
        )
        return True
    finally:
        stop.set()
        heartbeat.join(timeout=20)


def run_imports(sessions, settings, *, workers=2, max_items=0, job_id=None, stop_event=None):
    require(
        1 <= workers <= 2 and max_items >= 0, "WORKER_LIMIT", "Use one or two import workers", 422
    )
    count, count_lock = [0], threading.Lock()

    def run():
        # Session advisory locks cap heavy validators across all worker processes.
        with sessions.kw["bind"].connect() as slot_session:
            slot = None
            for candidate in range(2):
                if slot_session.scalar(
                    text("SELECT pg_try_advisory_lock(1397311310,:slot)"), {"slot": 100 + candidate}
                ):
                    slot = 100 + candidate
                    break
            slot_session.commit()
            if slot is None:
                return
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return
                    with count_lock:
                        if max_items and count[0] >= max_items:
                            return
                        count[0] += 1
                    if not process_one(sessions, settings, job_id=job_id):
                        with count_lock:
                            count[0] -= 1
                        return
            finally:
                slot_session.execute(
                    text("SELECT pg_advisory_unlock(1397311310,:slot)"), {"slot": slot}
                )
                slot_session.commit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run) for _ in range(workers)]
        for future in futures:
            future.result()
    return count[0]
