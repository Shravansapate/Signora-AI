---
name: isl-retrieval-production
description: Implement, debug, review, and verify Signora AI's production retrieval-based Indian Sign Language railway/public-announcement system, including separate backend/frontend development, typed and voice input, PostgreSQL/pg_trgm/pgvector retrieval, GLB ingestion and version management, FastAPI/WebSocket delivery, developer/admin asset lifecycle, and persistent-avatar playback. Use for continued Signora retrieval-system development; keep MP4-to-GLB conversion out of scope unless explicitly requested.
---

# ISL Retrieval Production

## Project contract

Use the existing project and its current approved implementation plan. Treat the converter as completed; work from supplied GLBs through meaning/metadata, retrieval, manifests, delivery, and avatar playback. Preserve working repository conventions and components.

The user's explicit task instructions take precedence over this skill when they conflict. Do not let a conservative interpretation of this skill block an authorized requested development task.

Treat the following as approved implementation-plan addenda even if an older external plan omits them: (1) typed text and push-to-talk voice are first-class inputs that converge into one common announcement pipeline; (2) runtime code is organized under separate top-level `backend/` and `frontend/` folders with API/WebSocket contracts between them. These addenda do not reopen the MP4-to-GLB converter.

Match the requested task: implement authorized development work, but keep a plan, explanation, or review request read-only unless the user also requests implementation. Do not create unrelated documents, scaffolds, or a parallel application. This skill does not authorize publishing, deployment, destructive actions, or broader project changes.

## Read only the relevant references

| Task | Reference |
|---|---|
| Architecture, content boundaries, inputs, or cross-layer changes | [Architecture](references/architecture.md) |
| Typed text, microphone/voice, ASR, transcript validation, or common input envelope | [Voice and text input](references/voice-text-input.md) |
| Repository layout, backend/frontend separation, or file placement | [Repository structure](references/repository-structure.md) |
| Models, migrations, eligibility, embeddings, or import metadata | [Database contract](references/database-schema.md) |
| Add/replace/deactivate/reactivate/rollback/revoke/delete or bulk ingestion | [GLB lifecycle](references/admin-glb-lifecycle.md) |
| Templates, hierarchical lookup, decomposition, slots, or manifests | [Retrieval and manifests](references/retrieval-playback.md) |
| Rig binding, animation queue, preloading, transitions, or caches | [Avatar playback](references/frontend-playback.md) |
| Announcements, revisions, sockets, priorities, expiry, or reconnect | [Realtime delivery](references/realtime-delivery.md) |
| Continue development or choose the next unfinished milestone | [Implementation milestones](references/implementation-milestones.md) |
| Verify changed behavior or assess completion | [Verification](references/verification.md) |

## Essential invariants

- Use verified ISL in production. Never use ASL, KSL, or Motion-S/KSL as linguistic fallback.
- Keep technical validity, linguistic approval, and composition approval separate. Approval attaches to the exact motion/version and applicable avatar/context; passing a file validator does not approve a sign.
- Give each production-eligible concept exactly one selected active motion version. Draft/inactive concepts may have no usable version. Preserve retained versions for valid snapshots and rollback.
- Keep concept identity, aliases, templates, and embeddings when only GLB quality changes. Recompute embeddings only when their semantic input or encoder contract changes.
- Store immutable GLB objects outside PostgreSQL and portable keys plus metadata inside it. Make new bytes durable/readable before committing an active pointer. Activate through constrained transactions and audit records.
- Compile complete approved meaning into an ordered, version-pinned manifest. Play compatible clips on one persistent avatar. Do not generate or merge sentence GLBs at request time.
- Preserve intent, negation, temporal meaning, entity roles, and exact operational values. Similarity cannot override these constraints. Never silently omit missing content.
- Distinguish active-catalog selection from playback of already published valid snapshots. Routine replacement, concept deactivation, and version revocation have different effects.
- Require complete-message readiness before operational signing. Define interruption boundaries linguistically; a digit/letter clip boundary inside an identifier is not safe by itself.
- Support both typed text and voice as first-class operator inputs. Voice must pass through ASR and transcript/critical-slot validation, then converge into the same typed meaning pipeline as text. Do not build a separate voice retrieval engine.
- Do not auto-publish from partial or uncertain ASR output. Low-confidence or conflicting train/platform/time/status values must return a confirmation/review state.

## Retrieval hierarchy

Use verified template -> exact sentence/phrase/concept -> reviewed alias -> pg_trgm fuzzy candidate -> pgvector semantic candidate -> approved phrase decomposition -> approved word decomposition -> typed number/name realization -> permitted verified ISL fingerspelling.

Extract and protect slots before lookup. Template concept IDs can resolve directly. Apply eligibility and meaning constraints to every candidate; do not make similarity scores into probabilities or automatic authority for unresolved critical meaning.

## Implementation choices

Prefer PostgreSQL + pg_trgm + pgvector, FastAPI/Pydantic/SQLAlchemy/Alembic, Next.js/React, Three.js or an existing React Three Fiber wrapper, GLTFLoader/AnimationMixer, and WebSockets backed by durable announcement records. Start with one modular backend and a storage abstraction over the current filesystem. Add object storage or distribution infrastructure when deployment or measurements justify it.

At the initial roughly 150+ GLB scale, benchmark exact vector search and real asset loading before adding approximate indexes, another vector database, a broker, or microservices. Keep runtime orchestration deterministic; do not introduce LangChain, LangGraph, CrewAI, AutoGen, or another autonomous-agent framework unless a measured requirement explicitly justifies it. Preserve equivalent working choices. A pretrained encoder is a fallback component; no new model training is required for the initial supported paths.

## Development and reporting

Inspect current code, applicable project instructions, migration state, and relevant tests before editing. Implement the requested milestone using the smallest coherent change. Read the focused references, run checks that exercise the changed behavior, and fix actionable failures within scope.

For bulk ingestion, use stable identities, checksums, persisted per-item progress, idempotency, and bounded retries; isolate bad assets and retain pending review states.

Resolve ordinary recoverable problems autonomously. If a genuine dependency or missing asset blocks completion, finish independent useful work, then state what passed, what is blocked, the exact failed component, and what is needed. Do not substitute mocks for missing production behavior or claim success from generated code.

Report changed files, checks actually run and their outcomes, remaining limitations, and the next milestone concisely. Mark unrun checks explicitly. Claim production readiness only for a defined supported scope with engineering and linguistic evidence.
