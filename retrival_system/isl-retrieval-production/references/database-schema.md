# Database and metadata contract

## Introduce records with their milestone

Adapt existing models; use migrations for schema changes. Keep indexed invariant fields relational and variable descriptive details in JSONB. Do not add every suggested table during a narrow task.

| Record | Required responsibility |
|---|---|
| avatar_profiles | Canonical avatar version/hash, rig/rest-transform contract, required track/morph mapping |
| sign_concepts | Stable ID and sense/context key; gloss/meaning; ISL language; domain/level; verification; enabled/eligible flags; selected active version; semantic revision |
| motion_versions | Concept ID/version; immutable key, SHA-256, bytes; selected clip; duration; avatar compatibility; technical QC; version-specific ISL approval; lifecycle state; revocation; actor/time |
| sign_aliases | Reviewed normalized alias, source text language, concept and domain/context scope |
| sign_embeddings | Concept semantic revision; model and exact revision; dimension; input-text hash; vector; readiness |
| templates and dependencies | Versioned intent/slot schema and reviewed recipe; required concept foreign keys; semantic boundaries; enabled/approval state |
| announcement_revisions | Stable message ID/revision, source event/revision, station, original text, meaning, priority, validity, supersession/state |
| playback_manifests and items | Immutable plan/version/hash, template/avatar versions, ordered items referencing exact motion versions and semantic groups |
| displays and deliveries | Device/station scope, liveness, received/ready/started/completed/failed progress and cursors |
| event_outbox and station_streams | Committed dispatch events, stream order, retry/reconciliation progress |
| retrieval/audit logs | Methods, constraints, selected versions, latency, rejected reasons; actor, previous/new state, action and result |
| import jobs/items | Stable identity/checksum/metadata hash, stage, individual outcome, actionable error, attempts and progress |

## Active pointer and integrity

Use sign_concepts.active_motion_version_id as the authoritative catalog pointer. Enforce at most one ACTIVE motion version per concept with a partial unique index or equivalent database invariant. Enforce same-concept ownership of the pointer through a composite foreign key or equivalent checked constraint.

A foreign key alone does not guarantee pointer/status agreement or approval. Use a short constrained activation transaction and, where needed, a deferred constraint trigger or checked database operation. Lock the concept and compare the expected active version/revision for concurrent admin changes. Use unique semantic keys and unique (concept_id, version_no). Draft/inactive concepts may have zero usable production versions.

Centralize new-plan eligibility: ISL; enabled/eligible concept; approved meaning/context; valid selected pointer; correct version ownership and ACTIVE state; passed technical and version-specific linguistic checks; compatible avatar; no revocation. Do not omit filters in fuzzy/vector paths.

Authorize an already published snapshot separately: permit its exact retained ARCHIVED version after a routine replacement only while concept/template eligibility, approval, avatar compatibility, freshness, and nonrevocation still hold. Do not globally require ACTIVE for such snapshots or silently substitute the latest version. Rebuild an unpublished preview whose selected version is no longer current.

## Metadata provenance

Derive checksum, file size, clip names, duration, target tracks, and external-resource references from actual bytes. Obtain meaning, alias, source permission/provenance, review, supported context, and semantic boundaries from evidence. Leave unknown data pending; do not fabricate reviewer identity or approval.

GLB uses animation keyframe timestamps. Derive actual clip duration from those inputs; keep source-video FPS/frame count and optional export sampling data separate and nullable. Do not invent an FPS or infer a universal frame count for every GLB. Constant channels can validly express a held handshape.

Keep portable keys, for example railway/ARRIVE/v4/Arrive.glb, in the database. Resolve URLs through storage configuration. Keep bytes durable before activating references. Store explicit manifest item and template dependency references for retention/deletion checks rather than searching unstructured JSON alone.

## Embeddings and batching

GLB-only replacement does not change semantic embeddings. Change the embedding when the encoder contract or the embedded semantic text changes; do not mix encoder revisions or dimensions. Prepare heavy inference before locking catalog rows. A pending encoder job may coexist with deterministic asset eligibility; semantic lookup must exclude absent/stale embeddings.

Fetch all unique required concepts/versions in a batched query where practical, then reconstruct requested sequence order and repetitions. Resolve/persist a manifest consistently in a transaction/snapshot and revalidate mutable state at publication.

Use one SQLAlchemy session per request or independent concurrent task. Do not share AsyncSession across parallel import tasks or keep a transaction open for a socket's lifetime. Preserve working exception/session conventions.

See [PostgreSQL constraints](https://www.postgresql.org/docs/current/ddl-constraints.html), [locking](https://www.postgresql.org/docs/current/explicit-locking.html), and [glTF animation timing](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#animations) when implementation details require confirmation.
