# Announcement delivery and recovery

## Persisted publication

Separate translation preparation from publication. Publishing requires an authorized source/operator, current supported meaning, eligible template/assets, valid source revision, and freshness. Revalidate a prepared preview before publication and rebuild changed catalog selections.

Commit the announcement revision, pinned manifest/items, audit where applicable, and dispatch/outbox event together. Send only committed work. Persist delivery attempts and per-display progress; a process-local list or best-effort background callback is not durable announcement state.

Begin with one supervised dispatcher in the existing backend if appropriate. Resume pending work after restart. Use a worker from the same codebase only when needed; do not introduce a broker by habit. Multiple gateway processes need explicit fan-out/reconciliation; in-memory socket collections are not shared across workers.

PostgreSQL NOTIFY can carry an event ID as a wake-up hint after commit. Keep complete data in tables and periodically reconcile them. Register/commit LISTEN before reconciling state on startup. Do not rely on transient notifications as the replay log. See [NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html), [LISTEN](https://www.postgresql.org/docs/current/sql-listen.html), and the [FastAPI example's process limits](https://fastapi.tiangolo.com/advanced/websockets/).

## Identity, ordering, and acknowledgements

Use immutable event IDs, stable message IDs/revisions, station scope, source-event/revision idempotency keys, and a station/display-stream cursor. Allocate ordered stream events under a short row lock and commit them together, or implement another explicitly proven ordering scheme. A sequence-generated numeric ID alone is not concurrent commit order.

Deliver at least once with idempotent clients. Track RECEIVED, ASSETS_READY, STARTED, COMPLETED, and FAILED distinctly. A socket send is not physical playback; receipt is not completion. Persist last contiguous received cursor separately from playback progress.

On reconnect, supply the last contiguous cursor and relevant message progress; return current authoritative state plus eligible missed events. Use a snapshot when replay history has been compacted. Never replay expired, cancelled, revoked, or superseded information merely because a cursor missed it. Reject older source revisions for the same event.

Do not promise exactly-once visible signing across crashes. A browser can fail after showing a sign but before persisting its acknowledgement. Recover at approved semantic boundaries or restart a still-current complete message under policy; never resume midway through an identifier.

## Priorities, corrections, and freshness

Default priority classes: P0 approved emergency, P1 cancellation/platform change or urgent correction, P2 arrival/departure, P3 general information. Apply the project's actual approved policy.

Update accurate urgent text immediately where appropriate. Change signing at an approved semantic boundary; a train number, name, phrase, or fixed sentence may be atomic. A completed individual GLB does not itself authorize interruption. Measure actual emergency response delay against these boundaries.

Discard superseded queued revisions and cancel obsolete preload callbacks. Reconsider a lower-priority message's validity before replay after interruption; do not append its tail to another message. Bound queue length and age. Account for signing throughput: if arrival rate times average signing duration approaches one, faster database queries cannot prevent queue growth. Supersede duplicate/obsolete events without dropping distinct critical meaning.

Separate routine replacement from defect revocation. Keep published valid manifests pinned during routine replacement. Send durable concept-deactivation/version-revocation events and prevent affected plans from starting; active playback follows safe interruption policy. A disconnected display cannot receive instant revocation or undo already shown signs.

Enforce heartbeat, reconnect backoff, expiry, and a configured freshness lease using authoritative timing. Once permitted freshness is lost, pause operational signing. Cached bytes do not establish current platform/train information. Distinguish internet outage with functioning local services from source-feed failure or display-server disconnection; approved static information may have a separate availability policy.

## Access and operations

Authenticate sockets, check browser origins, scope feeds/devices to stations, and revalidate expired sessions. A display may receive/acknowledge but must not publish or approve assets. Bound payloads and avoid long-lived credential URLs/logs.

Observe dispatch lag, queue age, device last-seen, asset readiness/failures, and completion. Restore PostgreSQL and immutable assets as a recoverable pair and test a restored manifest with its referenced bytes. Do not infer service health from an open socket alone.
