"""Durable publication and per-display reconciliation; no process-local replay authority."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text

from app.announcement_schema import AnnouncementPlan
from app.announcements import _compile, check_station_scope
from app.catalog import catalog_lock
from app.lifecycle import _event, require
from app.live_schema import LiveContext
from app.models import (
    AnnouncementInputRecord,
    AnnouncementManifestRef,
    AnnouncementTemplate,
    PlaybackRecord,
    Station,
    TemplateMotionBinding,
)
from app.playback import PlaybackUnavailable, validate_record
from app.templates import definition_hash


def now():
    return datetime.now(UTC)


def one(session, sql, **params):
    return session.execute(text(sql), params).mappings().first()


def stream_lock(session, station):
    session.execute(
        text("INSERT INTO station_streams(station_id) VALUES(:id) ON CONFLICT DO NOTHING"),
        {"id": station},
    )
    return one(session, "SELECT * FROM station_streams WHERE station_id=:id FOR UPDATE", id=station)


def append_event(session, station, message, revision, kind):
    return session.scalar(
        text("SELECT append_station_event(:station,:id,:rev,:kind)"),
        {"station": station, "id": message, "rev": revision, "kind": kind},
    )


def publication_result(session, message_id, revision):
    row = one(
        session,
        """SELECT a.id AS message_id,a.station_id,r.revision,r.source_revision,
      r.manifest_id,r.state,r.priority,r.valid_until FROM announcements a
      JOIN announcement_revisions r ON r.message_id=a.id WHERE a.id=:id AND r.revision=:rev""",
        id=message_id,
        rev=revision,
    )
    return dict(row)


def publish(session, store, request, identity, message_id=None, *, development=False):
    require(
        bool(identity.roles & {"operator", "admin"}),
        "ROLE_REQUIRED",
        "Publisher role required",
        403,
    )
    check_station_scope(identity, request.station_id)
    request_payload = request.model_dump(mode="json")
    if request.audience is None:
        # Preserve idempotent retries of station-wide requests committed before routing existed.
        for key in ("audience", "display_ids", "expected_routes", "emergency"):
            request_payload.pop(key)
    digest = definition_hash(request_payload)
    with session.begin():
        catalog_lock(session)
        station = session.get(Station, request.station_id)
        require(station is not None, "STATION_UNCONFIGURED", "Station not configured", 422)
        stream_lock(session, station.id)
        if message_id:
            message = one(session, "SELECT * FROM announcements WHERE id=:id", id=message_id)
            require(message is not None, "NOT_FOUND", "Announcement not found", 404)
            require(
                message["source_subject"] == identity.subject or "admin" in identity.roles,
                "SOURCE_SCOPE",
                "Another source owns this event",
                403,
            )
            require(
                message["station_id"] == station.id
                and message["source_event_id"] == request.source_event_id,
                "SOURCE_SCOPE",
                "Source identity must remain stable",
                422,
            )
        else:
            message = one(
                session,
                """SELECT * FROM announcements WHERE station_id=:station
              AND source_subject=:subject AND source_event_id=:source""",
                station=station.id,
                subject=identity.subject,
                source=request.source_event_id,
            )
        if message:
            prior = one(
                session,
                """SELECT * FROM announcement_revisions WHERE message_id=:id
              AND source_revision=:source""",
                id=message["id"],
                source=request.source_revision,
            )
            if prior:
                require(
                    prior["request_hash"] == digest,
                    "IDEMPOTENCY_CONFLICT",
                    "Source revision has different content",
                )
                return publication_result(session, message["id"], prior["revision"])
            require(
                request.source_revision > message["source_revision"],
                "STALE_SOURCE",
                "Source revision is obsolete",
            )
        current = message["current_revision"] if message else 0
        require(
            current == request.expected_revision, "STALE_REVISION", "Refresh announcement revision"
        )
        require(
            message is not None or not request.cancel,
            "NOT_FOUND",
            "Cannot cancel an absent event",
            404,
        )
        from app.display_control import assign, targets

        selected = (
            targets(session, station.id, request) if request.audience and not request.cancel else []
        )
        mid, rev = (message["id"] if message else uuid4()), current + 1
        plan, priority, expiry = None, 1, now() + timedelta(minutes=15)
        if not request.cancel:
            record = session.get(PlaybackRecord, request.preview_manifest_id)
            require(
                record is not None and record.owner_subject == identity.subject,
                "PREVIEW_SCOPE",
                "Own announcement preview required",
                403,
            )
            is_development = development and record.purpose == "CONTENT_REVIEW"
            require(
                (record.purpose == "ANNOUNCEMENT_PREVIEW" or is_development)
                and record.manifest_hash == request.preview_manifest_hash,
                "PREVIEW_REQUIRED",
                "Exact announcement preview identity required",
                422,
            )
            if is_development:
                preview, _ = validate_record(session, store, record, development=True)
                receipt = session.scalar(
                    select(AnnouncementInputRecord).where(
                        AnnouncementInputRecord.manifest_id == record.id,
                        AnnouncementInputRecord.owner_subject == identity.subject,
                    )
                )
                require(
                    receipt is not None and receipt.response.get("demo_mode"),
                    "PREVIEW_REQUIRED",
                    "Own development announcement required",
                    422,
                )
                preview_station = receipt.station_id
            else:
                preview = AnnouncementPlan.model_validate(record.payload)
                receipt = session.get(AnnouncementInputRecord, preview.announcement.input_id)
                preview_station = preview.announcement.station_id
            require(
                preview_station == station.id,
                "STATION_SCOPE",
                "Preview station differs",
                403,
            )
            require(
                receipt is not None
                and receipt.manifest_id == record.id
                and receipt.valid_until > now(),
                "INPUT_EXPIRED",
                "Prepare a fresh confirmed input",
            )
            priority = (
                1
                if not is_development
                and preview.announcement.meaning.intent in {"TRAIN_CANCELLATION", "PLATFORM_CHANGE"}
                else 2
            )
            if request.emergency:
                priority = 0
            count = session.scalar(
                text("""SELECT count(*) FROM announcements a JOIN announcement_revisions r
              ON r.message_id=a.id AND r.revision=a.current_revision WHERE a.station_id=:station
              AND a.state='LIVE' AND NOT r.targeted AND r.valid_until>now() AND a.id<>:id"""),
                {"station": station.id, "id": mid},
            )
            require(
                bool(request.audience) or count < 32,
                "QUEUE_CAPACITY",
                "Station has 32 live messages; resolve expired or obsolete work",
                429,
            )
            # Compile the same confirmed meaning against current reviewed selections, atomically.
            delivery = LiveContext(
                message_id=mid,
                revision=rev,
                source_event_id=request.source_event_id,
                source_revision=request.source_revision,
                priority=priority,
                supersedes_revision=current or None,
            )
            plan = (
                _development_plan(session, store, record, receipt, delivery)
                if is_development
                else _compile(
                    session,
                    store,
                    preview.announcement.meaning,
                    station,
                    receipt.id,
                    preview.caption_text,
                    identity.subject,
                    receipt.valid_until,
                    live=LiveContext(
                        message_id=mid,
                        revision=rev,
                        source_event_id=request.source_event_id,
                        source_revision=request.source_revision,
                        priority=priority,
                        supersedes_revision=current or None,
                    ),
                )
            )
            require(
                now() + timedelta(seconds=plan.estimated_duration_seconds) < plan.valid_until,
                "INPUT_EXPIRED",
                "Insufficient validity for complete signing",
            )
            expiry = plan.valid_until
            require(
                len(plan.items) - 1 in plan.safe_boundaries,
                "FINAL_BOUNDARY",
                "Published constructions require a reviewed complete-message boundary",
            )
            context = plan.announcement
            if not is_development:
                session.add(
                    AnnouncementManifestRef(
                        manifest_id=plan.manifest_id,
                        template_version_id=context.template_version_id,
                        review_id=context.review_id,
                        station_id=station.id,
                        input_id=receipt.id,
                    )
                )
        state = "CANCELLED" if request.cancel else "LIVE"
        if not message:
            session.execute(
                text("""INSERT INTO announcements
              (id,station_id,source_subject,source_event_id,current_revision,source_revision,state)
              VALUES(:id,:station,:subject,:source,:rev,:source_rev,:state)"""),
                {
                    "id": mid,
                    "station": station.id,
                    "subject": identity.subject,
                    "source": request.source_event_id,
                    "rev": rev,
                    "source_rev": request.source_revision,
                    "state": state,
                },
            )
        else:
            session.execute(
                text("""UPDATE announcements SET
              current_revision=:rev,source_revision=:source,state=:state
              WHERE id=:id"""),
                {"id": mid, "rev": rev, "source": request.source_revision, "state": state},
            )
        session.execute(
            text("""INSERT INTO announcement_revisions
(message_id,revision,source_revision,request_hash,manifest_id,state,priority,reason,actor,valid_until,targeted)
          VALUES(:id,:rev,:source,:hash,:manifest,:state,:priority,:reason,:actor,:expiry,:targeted)"""),
            {
                "id": mid,
                "rev": rev,
                "source": request.source_revision,
                "hash": digest,
                "manifest": plan.manifest_id if plan else None,
                "state": state,
                "priority": priority,
                "reason": request.reason,
                "actor": identity.subject,
                "expiry": expiry,
                "targeted": bool(request.audience),
            },
        )
        if selected:
            assign(
                session,
                selected,
                plan.manifest_id,
                identity,
                "EMERGENCY_BROADCAST"
                if request.emergency
                else "BROADCAST"
                if request.audience == "ALL"
                else "ASSIGN",
                request.reason,
            )
        if request.cancel:
            cancelled = (
                session.execute(
                    text("""SELECT d.display_id AS id FROM display_routes d
              JOIN announcement_revisions r ON r.manifest_id=d.manifest_id
              WHERE r.message_id=:id"""),
                    {"id": mid},
                )
                .mappings()
                .all()
            )
            assign(session, cancelled, None, identity, "WITHDRAW", request.reason)
        append_event(session, station.id, mid, rev, "CANCELLED" if request.cancel else "PUBLISHED")
        _event(
            session,
            identity.subject,
            "ANNOUNCEMENT_" + state,
            mid,
            {"revision": rev, "source_revision": request.source_revision, "station_id": station.id},
        )
        return publication_result(session, mid, rev)


def _development_plan(session, store, record, receipt, delivery):
    from app.live_schema import DevelopmentLivePlan
    from app.models import PlaybackItemRecord
    from app.playback import semantic_hash

    preview, _ = validate_record(session, store, record, development=True)
    pid = uuid4()
    payload = preview.model_dump(mode="json")
    payload.update(
        schema_version=5,
        purpose="PUBLISHED",
        operational=True,
        development=True,
        manifest_id=str(pid),
        issued_at=now(),
        announcement={"station_id": receipt.station_id, "input_id": receipt.id},
        delivery=delivery.model_dump(),
    )
    for ref in [payload["avatar"], *payload["items"]]:
        ref["asset_url"] = (
            f"/api/v1/playback/{pid}/assets/{ref['motion_version_id']}/{ref['sha256']}.glb"
        )
    plan = DevelopmentLivePlan.model_validate(payload)
    plan = plan.model_copy(update={"manifest_hash": semantic_hash(plan)})
    session.add(
        PlaybackRecord(
            id=pid,
            owner_subject=record.owner_subject,
            purpose="PUBLISHED",
            avatar_motion_version_id=record.avatar_motion_version_id,
            manifest_hash=plan.manifest_hash,
            issued_at=plan.issued_at,
            valid_until=plan.valid_until,
            payload=plan.model_dump(mode="json"),
            selection_revisions=record.selection_revisions,
        )
    )
    session.flush()
    session.add_all(
        [
            PlaybackItemRecord(
                manifest_id=pid, sequence_index=i, motion_version_id=item.motion_version_id
            )
            for i, item in enumerate(plan.items)
        ]
    )
    return plan


def validate_published(session, record, plan, rows):
    current = one(session, "SELECT * FROM announcements WHERE id=:id", id=plan.delivery.message_id)
    if plan.schema_version == 5:
        if (
            not current
            or current["state"] != "LIVE"
            or current["current_revision"] != plan.delivery.revision
            or current["station_id"] != plan.announcement.station_id
        ):
            raise PlaybackUnavailable("Development announcement superseded or withdrawn")
        return
    template = session.get(AnnouncementTemplate, plan.announcement.template_version_id)
    station = session.get(Station, plan.announcement.station_id)
    if (
        not current
        or current["state"] != "LIVE"
        or current["current_revision"] != plan.delivery.revision
        or not template
        or not template.enabled
        or template.status != "APPROVED"
        or template.definition_hash != plan.announcement.definition_hash
        or not station
        or station.revision != plan.announcement.station_revision
    ):
        raise PlaybackUnavailable("Announcement superseded, withdrawn or configuration changed")
    bound = set(
        session.scalars(
            select(TemplateMotionBinding.motion_version_id).where(
                TemplateMotionBinding.review_id == plan.announcement.review_id,
                TemplateMotionBinding.template_version_id == template.id,
            )
        )
    )
    for item in plan.items:
        motion, concept, avatar = rows[item.motion_version_id]
        if (
            motion.id not in bound
            or not concept.enabled
            or concept.meaning_status != "APPROVED"
            or motion.linguistic_review_status != "APPROVED"
            or motion.composition_review_status != "APPROVED"
            or motion.reviewed_sha256 != motion.sha256
            or motion.reviewed_avatar_profile_id != avatar.id
            or motion.reviewed_semantic_revision != concept.semantic_revision
            or record.selection_revisions.get(str(concept.id)) != concept.semantic_revision
            or avatar.status != "APPROVED"
            or avatar.source_sha256 != plan.avatar.sha256
        ):
            raise PlaybackUnavailable("The pinned published content is no longer approved")


def register_device(session, did, request, identity, token_days=90):
    require("admin" in identity.roles, "ROLE_REQUIRED", "Administrator required", 403)
    with session.begin():
        catalog_lock(session, write=True)
        require(
            session.get(Station, request.station_id) is not None,
            "NOT_FOUND",
            "Station not configured",
            404,
        )
        row = one(session, "SELECT * FROM display_devices WHERE id=:id FOR UPDATE", id=did)
        require(
            (row["revision"] if row else 0) == request.expected_revision,
            "STALE_REVISION",
            "Refresh device",
        )
        if row:
            require(
                not request.issue_access_token,
                "TOKEN_REPLACEMENT_REQUIRED",
                "Use the replace-token action for an existing display",
                422,
            )
            require(
                row["station_id"] == request.station_id and row["subject"] == request.subject,
                "DEVICE_IDENTITY",
                "Station/subject are immutable; register a new device",
                422,
            )
            session.execute(
                text("""UPDATE display_devices SET name=:name,enabled=:enabled,revision=revision+1,
              session_id=NULL,lease_until=NULL WHERE id=:id"""),
                {"id": did, "name": request.name, "enabled": request.enabled},
            )
        else:
            require(
                one(
                    session,
                    "SELECT id FROM display_devices WHERE subject=:subject",
                    subject=request.subject,
                )
                is None,
                "DEVICE_IDENTITY",
                "This subject already belongs to a registered display",
            )
            session.execute(
                text("""INSERT INTO display_devices(id,station_id,subject,name,enabled)
              VALUES(:id,:station,:subject,:name,:enabled)"""),
                {
                    "id": did,
                    "station": request.station_id,
                    "subject": request.subject,
                    "name": request.name,
                    "enabled": request.enabled,
                },
            )
        _event(session, identity.subject, "DISPLAY_CONFIGURED", did, {"enabled": request.enabled})
        result = dict(one(session, "SELECT * FROM display_devices WHERE id=:id", id=did))
        if request.issue_access_token:
            from app.display_credentials import issue_token

            result.update(issue_token(session, did, identity.subject, token_days))
            _event(session, identity.subject, "DISPLAY_TOKEN_ISSUED", did, {})
        else:
            result["token_expires_at"] = session.scalar(
                text("SELECT expires_at FROM display_credentials WHERE display_id=:id"),
                {"id": did},
            )
        return result


def device_access(session, did, identity, *, lock=False, session_id=None):
    device = one(
        session,
        "SELECT * FROM display_devices WHERE id=:id" + (" FOR UPDATE" if lock else ""),
        id=did,
    )
    require(
        device
        and device["enabled"]
        and "display" in identity.roles
        and device["subject"] == identity.subject
        and device["station_id"] in identity.station_ids
        and identity.expires_at > now(),
        "DISPLAY_SCOPE",
        "Display authorization unavailable",
        403,
    )
    credential = one(session, "SELECT * FROM display_credentials WHERE display_id=:id", id=did)
    if credential:
        require(
            identity.display_id == did
            and identity.credential_digest == credential["token_hash"]
            and credential["expires_at"] > now(),
            "DISPLAY_CREDENTIAL_REPLACED",
            "Reconnect with the current display token",
            403,
        )
    if session_id:
        require(
            device["session_id"] == session_id,
            "SESSION_REPLACED",
            "A newer display session owns this device",
            409,
        )
    return device


def connect_display(sessions, did, identity):
    with sessions() as session, session.begin():
        device_access(session, did, identity, lock=True)
        sid = uuid4()
        session.execute(
            text("UPDATE display_devices SET session_id=:sid,lease_until=NULL WHERE id=:id"),
            {"id": did, "sid": sid},
        )
        return sid


def reconcile(sessions, store, did, identity, sid, cursor, lease_seconds, development=False):
    with sessions() as session, session.begin():
        catalog_lock(session)
        device = device_access(session, did, identity)
        stream = stream_lock(session, device["station_id"])
        device_access(session, did, identity, lock=True, session_id=sid)
        require(
            cursor <= stream["cursor"],
            "CURSOR_AHEAD",
            "Display cursor exceeds committed stream",
            409,
        )
        rows = (
            session.execute(
                text("""SELECT a.id,r.manifest_id,r.revision,r.priority,r.valid_until
          FROM announcements a JOIN announcement_revisions r ON r.message_id=a.id AND
              r.revision=a.current_revision
          WHERE a.station_id=:station AND a.state='LIVE' AND r.valid_until>now()
          AND (EXISTS(SELECT 1 FROM display_routes dr WHERE dr.display_id=:display
                   AND dr.manifest_id=r.manifest_id)
            OR (NOT r.targeted AND NOT EXISTS(SELECT 1 FROM display_routes dr
                   WHERE dr.display_id=:display)))
          ORDER BY r.priority,r.created_at,a.id LIMIT 33"""),
                {"station": device["station_id"], "display": did},
            )
            .mappings()
            .all()
        )
        require(len(rows) <= 32, "QUEUE_CAPACITY", "Station queue exceeds supported capacity", 503)
        active = []
        for row in rows:
            record = session.get(PlaybackRecord, row["manifest_id"])
            try:
                plan, _ = validate_record(
                    session, store, record, verify_bytes=False, development=development
                )
            except PlaybackUnavailable:
                continue
            delivery = one(
                session,
                "SELECT state,attempt,boundary,error_code FROM display_deliveries WHERE "
                "display_id=:id AND manifest_id=:plan",
                id=did,
                plan=record.id,
            )
            active.append(
                {
                    "message_id": row["id"],
                    "revision": row["revision"],
                    "manifest_id": record.id,
                    "manifest_hash": record.manifest_hash,
                    "priority": row["priority"],
                    "caption_text": plan.caption_text,
                    "valid_until": row["valid_until"],
                    "last_boundary": len(plan.items) - 1,
                    "progress": dict(delivery) if delivery else None,
                }
            )
        events = (
            session.execute(
                text("""SELECT * FROM event_outbox WHERE station_id=:station AND cursor>:cursor
          ORDER BY cursor LIMIT 129"""),
                {"station": device["station_id"], "cursor": cursor},
            )
            .mappings()
            .all()
        )
        mode = "SNAPSHOT" if cursor < stream["replay_floor"] or len(events) > 128 else "REPLAY"
        # Only current state contains manifest references; obsolete events are cursor tombstones.
        delivered_events = [] if mode == "SNAPSHOT" else [dict(e) for e in events]
        for event in events[:128]:
            session.execute(
                text("""INSERT INTO delivery_attempts(display_id,event_id) VALUES(:id,:event)
              ON CONFLICT(display_id,event_id) DO UPDATE SET
              attempts=delivery_attempts.attempts+1,last_sent_at=now()"""),
                {"id": did, "event": event["id"]},
            )
        clock = now()
        lease = min(clock + timedelta(seconds=lease_seconds), identity.expires_at)
        session.execute(
            text("UPDATE display_devices SET offered_cursor=:cursor WHERE id=:id"),
            {"id": did, "cursor": stream["cursor"]},
        )
        # Lease is only persisted on a following authenticated client heartbeat/ack.
        return {
            "type": "SYNC",
            "mode": mode,
            "session_id": sid,
            "station_id": device["station_id"],
            "cursor": stream["cursor"],
            "events": delivered_events,
            "active": active,
            "server_time": clock,
            "lease_until": lease,
        }


def acknowledge(sessions, did, identity, sid, request, lease_seconds, development=False):
    with sessions() as session, session.begin():
        catalog_lock(session)
        device = device_access(session, did, identity, lock=True, session_id=sid)
        require(
            request.cursor <= device["offered_cursor"],
            "CURSOR_AHEAD",
            "Cannot acknowledge unseen stream",
        )
        if request.manifest_id:
            record = session.get(PlaybackRecord, request.manifest_id)
            require(
                record is not None
                and record.purpose == "PUBLISHED"
                and record.payload["announcement"]["station_id"] == device["station_id"],
                "MANIFEST_SCOPE",
                "Manifest is outside this display's station",
                403,
            )
            from app.display_control import assigned_to

            # Late terminal ACKs for an interrupted prior assignment are retained as history.
            previous_delivery = one(
                session,
                "SELECT state FROM display_deliveries WHERE display_id=:id AND manifest_id=:plan",
                id=did,
                plan=record.id,
            )
            if request.state not in {"FAILED", "COMPLETED"} or not previous_delivery:
                if not assigned_to(session, did, record.id):
                    raise PlaybackUnavailable("Announcement is no longer assigned to this display")
            require(request.state is not None, "ACK_STATE", "Playback state required", 422)
            row = one(
                session,
                "SELECT * FROM display_deliveries WHERE display_id=:id AND manifest_id=:plan "
                "FOR UPDATE",
                id=did,
                plan=record.id,
            )
            order = {"RECEIVED": 0, "ASSETS_READY": 1, "STARTED": 2, "COMPLETED": 3}
            before = row["state"] if row else None
            require(
                before is not None or request.state == "RECEIVED",
                "ACK_ORDER",
                "Receipt precedes playback",
            )
            retry = before == "FAILED" and request.state == "RECEIVED"
            if before and not retry:
                require(
                    request.state == before
                    or (request.state == "FAILED" and before != "COMPLETED")
                    or (
                        before in order
                        and request.state in order
                        and order[request.state] == order[before] + 1
                    ),
                    "ACK_ORDER",
                    "Playback acknowledgement is out of order",
                )
            if request.state == "STARTED" and before != "STARTED":
                validate_record(session, None, record, verify_bytes=False, development=development)
                require(
                    device["lease_until"] and device["lease_until"] > now(),
                    "LEASE_EXPIRED",
                    "Fresh heartbeat required",
                )
            if request.boundary >= 0:
                require(
                    request.boundary in record.payload["safe_boundaries"],
                    "UNSAFE_BOUNDARY",
                    "Cannot acknowledge partial semantic groups",
                    422,
                )
            require(
                not row or retry or request.boundary >= row["boundary"],
                "ACK_REGRESSION",
                "Playback progress cannot move backwards",
            )
            if request.state == "COMPLETED":
                require(
                    request.boundary == len(record.payload["items"]) - 1,
                    "INCOMPLETE",
                    "Complete message boundary required",
                )
            session.execute(
                text("""INSERT INTO
              display_deliveries(display_id,manifest_id,state,boundary,error_code)
              VALUES(:id,:plan,:state,:boundary,:error) ON CONFLICT(display_id,manifest_id) DO
              UPDATE
              SET state=:state,boundary=:boundary,error_code=:error,updated_at=now(),
              attempt=display_deliveries.attempt + :retry"""),
                {
                    "id": did,
                    "plan": record.id,
                    "state": request.state,
                    "boundary": request.boundary,
                    "error": request.error_code,
                    "retry": int(retry),
                },
            )
            session.execute(
                text("""INSERT INTO display_acknowledgements
              (display_id,manifest_id,attempt,state,boundary,error_code,session_id)
              SELECT display_id,manifest_id,attempt,state,boundary,error_code,:sid
              FROM display_deliveries WHERE display_id=:id AND manifest_id=:plan
              ON CONFLICT DO NOTHING"""),
                {"sid": sid, "id": did, "plan": record.id},
            )
        lease = min(now() + timedelta(seconds=lease_seconds), identity.expires_at)
        session.execute(
            text("""UPDATE display_devices SET received_cursor=greatest(received_cursor,:cursor),
          last_seen=now(),lease_until=:lease WHERE id=:id"""),
            {"id": did, "cursor": request.cursor, "lease": lease},
        )
    if request.manifest_id:
        logging.getLogger("signora.api.delivery").info(
            "display_progress display=%s manifest=%s state=%s boundary=%s error_code=%s",
            did,
            request.manifest_id,
            request.state,
            request.boundary,
            request.error_code,
        )
    return {
        "type": "ACKNOWLEDGED",
        "cursor": request.cursor,
        "manifest_id": request.manifest_id,
        "state": request.state,
    }
