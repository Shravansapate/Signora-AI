# Repository structure and backend/frontend separation

The production repository must keep backend and frontend as separate top-level folders.

Recommended target layout:

```text
signora-ai/
├── AGENTS.md
├── .env.example
├── docker-compose.yml
├── .codex/
│   └── skills/
│       └── isl-retrieval-production/
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   └── versions/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   ├── api/
│   │   │   └── v1/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── repositories/
│   │   ├── services/
│   │   │   ├── input/
│   │   │   ├── asr/
│   │   │   ├── retrieval/
│   │   │   ├── announcements/
│   │   │   └── admin/
│   │   └── storage/
│   └── tests/
│
├── frontend/
│   ├── package.json
│   ├── next.config.*
│   ├── src/
│   │   ├── app/
│   │   │   ├── operator/
│   │   │   ├── display/
│   │   │   └── admin/
│   │   ├── components/
│   │   ├── motion/
│   │   ├── services/
│   │   └── types/
│   └── tests/
│
├── motion_storage/
│   └── railway/
│
└── scripts/
    ├── bulk_import.py
    ├── validate_library.py
    └── seed_templates.py
```

This is a target organization. Do not create empty files or folders just to match it. Inspect the existing repository and reuse working structure where equivalent.

## Backend ownership

`backend/` owns:

- FastAPI application
- Pydantic API contracts
- SQLAlchemy models
- Alembic migrations
- PostgreSQL access
- pg_trgm and pgvector retrieval
- template/intent/slot/meaning compiler
- number/name/fingerspelling logic
- ASR adapter and audio preprocessing
- playback-manifest generation
- announcement persistence
- durable dispatch/outbox logic
- WebSocket server endpoints
- admin GLB lifecycle
- asset storage abstraction
- authentication/RBAC
- audit logs
- backend tests

Do not put React/Three.js/browser code here.

## Frontend ownership

`frontend/` owns:

- Next.js/React application
- operator typing/voice UI
- push-to-talk recorder
- transcript preview/confirmation
- passenger display
- admin motion-management UI
- persistent Three.js avatar
- GLTFLoader/AnimationMixer integration
- clip cache/preloading
- animation queue/state machine
- transition controller
- WebSocket browser client
- API client
- frontend tests

Do not put SQLAlchemy, database migrations, Python retrieval logic, or server secrets here.

## Communication boundary

Frontend and backend communicate only through explicit contracts:

- REST/HTTP APIs
- WebSocket events
- immutable/versioned asset URLs

Do not import backend Python modules into frontend code or frontend TypeScript modules into backend code.

Keep shared meaning in wire schemas rather than a cross-runtime source folder unless the existing repository already has a safe generated-contract workflow.

## Asset storage

Actual GLBs are not frontend source files and not PostgreSQL binaries.

Use a configured storage root such as:

```text
motion_storage/
└── railway/
    ├── ARRIVE/
    │   ├── v1/
    │   └── v2/
    ├── TRAIN/
    └── PLATFORM/
```

The backend owns asset registration, validation, version activation, and URL resolution. The frontend receives asset URLs/keys in the playback manifest.

## Development rule

When implementing a task, Codex must first decide whether it belongs to:

- backend only
- frontend only
- both through an API contract

If both are changed, implement the backend contract first, then update the frontend client against that contract, then run an end-to-end test.

Do not collapse the project into a single mixed `src/` tree.
