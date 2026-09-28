# Signora

## Project Overview

Signora is a retrieval-based Indian Sign Language (ISL) announcement system for railway stations. It converts railway announcements into reviewed sequences of 3D sign-language motions and plays them through a persistent avatar on an operator preview screen and connected passenger displays.

The project addresses a practical accessibility problem: important railway information is usually communicated through audio and written text, which can exclude passengers who rely on visual communication. Signora provides a controlled visual signing workflow for announcements such as train arrivals, departures, delays, cancellations, and platform changes.

## How the System Solves the Problem

Signora does not generate arbitrary animations from English words. It uses a controlled and reviewable pipeline:

1. An operator enters text, structured fields, or a voice recording.
2. The backend extracts the announcement meaning, including event, time, polarity, train number, and platform values.
3. The meaning is converted into typed ISL gloss units rather than blindly splitting English words.
4. The retrieval layer finds reviewed sign concepts, phrases, or words from the motion library.
5. A reviewed construction template determines the complete signing order and safe interruption boundaries.
6. Every GLB asset, avatar, checksum, review, and version is validated before playback.
7. The 3D avatar performs the complete motion sequence.
8. In live operation, the approved announcement is published to connected station displays.
9. Displays acknowledge receipt, preparation, playback, completion, or failure.

If the system cannot produce a complete reviewed construction, it reports the missing coverage instead of silently presenting an unreliable translation.

## Main Screens

### Content Preview - `/`

The private reviewer workspace. It provides:

- Searchable motion library
- Exact motion-version selection
- Avatar selection
- Ordered sequence building and reordering
- Exact registered-text lookup
- Prepared template-review playback
- 3D avatar preview
- Clip progress and playback controls

This screen is used to inspect and verify content. It does not automatically publish an announcement.

### Announcements - `/announcements`

The railway operator workspace. It provides:

- Station selection
- Typed announcement input
- Structured announcement fields
- Voice recording and transcript review
- Train, platform, time, delay, cancellation, and polarity fields
- Meaning and retrieval diagnostics
- Avatar playback
- Preview and live-delivery status

In the local development configuration, a ready announcement can be played automatically and sent to the connected live display. Production mode keeps preview and publication as separate confirmed actions.

### Central control room

The Announcements page now lists the selected station's registered displays, their platform labels, assigned captions, connection freshness and acknowledged playback status. The table refreshes every second; displays receive committed assignments over the existing authenticated WebSockets.

1. Register a distinct display identity and credential for every screen in Library & operations → Displays. Open `/display` on each screen with its own ID/token.
2. In Announcements, select one or more display checkboxes, enter text or record voice, and select **Play announcement**. In development this sends the available sequence; production still requires the reviewed preview/publication workflow. With no selected targets, development playback stays private.
3. Select another screen and send different text to give it an independent assignment. Sending replaces only the selected displays' assignments.
4. Choose **Broadcast to all displays** to replace every enabled display's assignment at this station. **Emergency priority** records priority 0. Offline screens receive only their latest unexpired assignment when they reconnect. Previous assignments do not automatically resume after a broadcast.
5. Use **Stop selected displays** to clear the selected assignments. Use each row's platform selector to change its physical platform label; this does not rewrite announcement content or move it to another screen. To move content, send it to the new target and stop the old target.

Text updates on the next WebSocket reconciliation (normally about one second). Signing must load its assets and switches at the existing safe animation boundary, so exact simultaneous frames or zero-latency emergency signing are not promised. A completed assignment plays once; it does not loop indefinitely. Correct emergency signing still depends on available/approved content under the configured retrieval mode.

Routing is persisted in `display_routes`, with optimistic route revisions and append-only `display_route_history`. Publication, route changes and station events commit together. Stop commands are idempotent; conflicts require refreshed targets. The operator history shows the latest 100 assignment actions; all actions remain in PostgreSQL. Migration `0009_display_routing` preserves old station-wide delivery for displays not yet individually controlled. Rollback after routing has been used requires a paired pre-upgrade backup or forward repair.

The local-PC launcher remains loopback-only. Separate physical display computers need a reachable same-origin frontend/reverse proxy and explicitly allowed WebSocket origins; `127.0.0.1` on a remote screen refers to that screen itself. No firewall or external listener is opened automatically.

### Library and Operations - `/admin`

The administrator and content-management workspace contains these sections:

- **Library:** concepts, motion versions, approvals, aliases, retrieval profiles, uploads, activation, rollback, revocation, and protected deletion.
- **Templates & coverage:** reviewed announcement constructions, dependencies, examples, rendered review, and activation.
- **Imports:** durable bulk import jobs, item results, retries, pause, and resume.
- **Stations:** station identity, platforms, train rules, and advanced configuration.
- **Displays:** display registration, connection health, delivery backlog, and playback history.
- **Audit:** recorded lifecycle, review, configuration, and publication actions.

### Live Display - `/display`

The passenger-facing display connects using a registered display identity and credential. It shows:

- Connection state
- Current announcement caption
- Signing state
- Persistent 3D avatar
- Automatic incoming announcement playback
- Waiting and error states

The display downloads and verifies all required motion assets before starting playback.

## Technical Architecture

```text
Next.js frontend
	|
	v
FastAPI API  ---- PostgreSQL registry and audit history
	|                    |
	|                    +-- reviewed signs, versions, templates, stations
	|                    +-- playback manifests and publication revisions
	|
	+-- GLB motion storage
	+-- local speech recognition
	+-- optional semantic retrieval encoder
	+-- WebSocket live-display delivery
```

The backend owns meaning parsing, retrieval, review rules, template compilation, asset authorization, publication, and recovery. The frontend owns operator workflows, review tools, avatar rendering, recording, and display playback.

## Current Capabilities

- 149 supplied GLB motion assets registered and validated
- FastAPI and PostgreSQL registry
- Durable import worker
- Exact motion-version lifecycle management
- Meaning, motion, avatar, alias, and template review
- Typed, structured, and voice announcement input
- Local speech-to-text support
- Gloss-based and reviewed retrieval
- 3D avatar playback using GLB assets
- Immutable playback manifests
- Station-scoped access control
- Live display delivery and reconnect recovery
- Announcement revisions and withdrawal
- Audit history

## Important Limitations

The current project is an engineering prototype and local demonstration platform. The following still require real-world validation:

- ISL linguistic and composition correctness
- Accuracy with noisy railway field audio
- Performance on target passenger-display hardware
- Complete vocabulary coverage
- Production deployment and backup recovery
- Nationwide station configuration
- Operational pilot acceptance

Development gloss rules are explicitly marked as draft rules. They are useful for demonstrating the complete technical pipeline, but they do not by themselves establish linguistic correctness.

## Quick Start

The local PC installation uses PostgreSQL, the FastAPI backend, a durable import worker, and the Next.js frontend. From the repository root in PowerShell:

```powershell
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File backend/tools/local-pc.ps1 start
```

Open:

- Admin: http://127.0.0.1:3000/admin
- Content preview: http://127.0.0.1:3000
- Announcements: http://127.0.0.1:3000/announcements
- Live display: http://127.0.0.1:3000/display
- API documentation: http://127.0.0.1:8000/docs

For complete setup, credentials, testing, deployment notes, and recovery procedures, continue reading this README and [SETUP.md](SETUP.md).

Implementation follows `isl-retrieval-production/SKILL.md` and the supplied Signora implementation plan. Backend and frontend remain separate. The converter and supplied asset metadata are unchanged.

Current implementation through Phase 7: asset inspection, the FastAPI/PostgreSQL registry, durable bulk import, exact-version review and lifecycle APIs, exact whole-expression retrieval, reviewed template/phrase/word construction compilation, scoped alias and calibrated fuzzy/semantic candidate search, common typed/structured/voice announcement input, immutable playback manifests, authenticated asset delivery, durable publication and display recovery, and Next.js operator, content-management and live-display workspaces with one persistent Three.js avatar. Actual station configuration and reviewed construction/realization policies determine the available announcement coverage; test recipes do not authorize production content.

## Running on this PC

The local installation uses `D:\SignoraData` for PostgreSQL, immutable assets, private credentials and logs. It has a separate database on `127.0.0.1:55432`; the installed PostgreSQL service is unchanged. The backend reads the ignored `backend/.env` using a restricted application database account. This is a local installation; external deployment is deferred.

From the repository root in PowerShell:

```powershell
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File backend/tools/local-pc.ps1 start
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File backend/tools/local-pc.ps1 status
# When finished:
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File backend/tools/local-pc.ps1 stop
```

`start` starts PostgreSQL, FastAPI, the durable import worker and the existing frontend build, checks readiness, and leaves them running in the background. Repeating it reuses owned processes. `stop` verifies process identity before terminating those process trees and stopping this database; committed work is retained and unfinished imports recover on restart. Logs are under `D:\SignoraData\logs`. Start after a PC restart using the same command. After changing frontend code, stop the applications, run `npm run build` from `frontend/`, then start again.

- Developer/admin: [http://127.0.0.1:3000/admin](http://127.0.0.1:3000/admin). Copy the `admin.token` from `D:\SignoraData\local-credentials.json` into **Management access token**. An administrator enters **Station name** and **Number of platforms** under Stations; the initial configuration is **Nagpur**, internal ID `NAGPUR`, platforms **1–8**. The generated station ID can be edited before creation. Save with a reason and confirmation. Advanced configuration supports custom platform identifiers, train rules and named entities. Updates preserve unchanged advanced fields and withdraw affected live announcements. Library and Imports provide the existing ingestion, review, version rollback and reference-protected deletion controls.
- Content playback: [http://127.0.0.1:3000](http://127.0.0.1:3000). Use the admin/reviewer token, **Connect workspace**, **Load more motions** if needed, select the persistent avatar source, add exact versions, **Prepare sequence**, then **Play sequence**.
- Operator: [http://127.0.0.1:3000/announcements](http://127.0.0.1:3000/announcements). Use `operator.token`, connect, enter text and click **Play announcement**. Or choose **Record voice**, record, then **Stop and transcribe**; the final transcript starts playback automatically. Nagpur and the local speech model are configured.
- Passenger display: [http://127.0.0.1:3000/display](http://127.0.0.1:3000/display). Use the display ID in `D:\SignoraData\local-display.json` and `display.token`. An empty current queue correctly shows no announcement.
- API documentation and readiness: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs), [http://127.0.0.1:8000/health/ready](http://127.0.0.1:8000/health/ready).

The local credential file records expiry (initially 22 October 2026). It and the migration-owner credentials in `D:\SignoraData\local-admin.json` are private local files, excluded from the application source. Keep raw tokens out of screenshots and shared logs. Each additional station uses the same administrator setup flow; separately scope operator/display credentials and register its displays. Adding a station does not establish nationwide content coverage or language comprehension.

This PC has `SIGNORA_DEMO_MODE_ENABLED=true` in `backend/.env`. Text and finalized voice transcripts share conservative domain spelling normalization and the railway semantic parser. Existing reviewed recipes retain priority when they cover the complete meaning; otherwise configurable draft gloss rules apply. If grammar parsing fails, independent lexical recovery preserves available phrases, words, aliases, identifiers and relationship words in input order, with partial slot diagnostics. Missing meaningful words try conservative catalog spelling recovery and available letters; grammatical function words are skipped unless part of a matched phrase. Identifiers preserve repeated digits. No unrelated sample motions are substituted: zero relevant matches returns an explicit empty result. Semantic status and retrieval status are separate; lexical recovery and draft rules are **not** linguistically validated ISL. **Motion lookup details** exposes corrections, semantic/partial interpretation, typed gloss, matches, spelling, missing signs and clip count.

**Play announcement** (or a final voice transcript) starts the preview and sends the same sequence to the station's connected live displays through `/api/v1/development/announcements`. Connect `/display` in another tab before entering an announcement; it loads and plays automatically. Publication retries reuse the same input identity. Displays report completion and avoid replaying completed sequences after reload. A failed handoff shows **Retry live delivery**. There is no frontend mode switch. Set the backend option to `false` and restart to disable development delivery and restore the strict reviewed input flow. The separate production publication endpoint retains its reviewed behavior.

## Validation

The earlier semantic/gloss revision passed 12 real API cases in [acceptance responses](artifacts/gloss-verification/acceptance-responses.json), including names, leading zeros, negation and temporal distinctions. `Train 1201 arrives at platform 2` generates `TRAIN | 1201 | PLATFORM | 2 | ARRIVE`, retrieves eight clips, and does not spell `at`. That revision reported missing signs for `DEPART`, `FUTURE`, `SHORTLY`, `ALREADY` and `NOT`; the current development retriever also attempts available letters for missing meaningful gloss. The proper station name test retains 33 alphabet occurrences in a 40-clip sequence. Reproduce with `frontend/scripts/verify-gloss-pipeline.mjs` using operator credentials supplied only through environment variables.

The corrected sequence also passed a real two-page Chromium test: the operator received the manifest and automatically published it; the live display downloaded seven GLB objects, showed changing bone poses and advancing mixer time, completed **8/8 clips**, and persisted a `COMPLETED` acknowledgement. Reload did not replay the completed announcement. See [live verification](artifacts/gloss-verification/live/verification.json), [API response and GLB URLs](artifacts/gloss-verification/live/mandatory-response.json), and [rendered playback](artifacts/gloss-verification/live/live-playing.png). The browser verifier captures the terminal snapshot before normal display reconciliation returns to idle.

Historical evidence from the previous revision: the [private preview verification](artifacts/gloss-verification/private/verification.json) separately completed the same eight-clip announcement and three explicitly labelled sample clips on one persistent avatar, with changing bone transforms and advancing mixer time. Verification for this correction passed 50 parser/gloss/retrieval tests, 24 database-backed preview/announcement tests, all 65 frontend tests, Ruff checks and the frontend build. Reviewed-recipe reuse and finalized voice-transcript routing are covered by the backend checks; this correction did not repeat microphone/ASR accuracy testing.

The current lexical-recovery correction passed 81 parser/gloss/retrieval tests, 34 database-backed tests, 65 frontend tests, Ruff and the frontend build. All 33 real API acceptance cases passed; see [responses](artifacts/lexical-recovery/acceptance-responses.json). Real Chromium playback completed the exact arrival (8/8), typo arrival (8/8), and `Train ACCIDENT` (2/2) on one persistent avatar with changing bone transforms, advancing mixer time, successful GLB downloads and no browser errors: [verification](artifacts/lexical-recovery/private/verification.json). Reproduce using `frontend/scripts/verify-lexical-recovery.mjs` and `frontend/scripts/verify-private-preview.mjs`. The current catalog labels the danger asset as `danger hazard peril` with no separate `danger` alias; an isolated `danger` therefore uses letters, without inventing catalog aliases. Finalized voice routing is covered by database tests; microphone/ASR accuracy was not retested for this correction.

Historical raw-English retrieval evidence remains under `artifacts/private-preview` and `artifacts/development-display`. Its ten-clip `at` spelling and fourteen-clip ASR result proved animation mechanics only; they are not evidence of correct translation and have been superseded by the semantic/gloss pipeline. The reusable browser verifiers are `frontend/scripts/verify-private-preview.mjs` and `frontend/scripts/verify-development-display.mjs`; credentials are supplied through the environment, never saved in the evidence.

The current Phase 8 local-PC record is [artifacts/phase-8-local-verification.json](artifacts/phase-8-local-verification.json): 16 backend recovery/registry checks, 64 frontend tests, a successful production build, real Chromium station setup and avatar playback, and the Nagpur display WebSocket connection. All 149 supplied assets are registered (148 staged by the import and one already staged), with zero import failures. Restart persistence and both local model readiness states passed. The database retains pending review states; no operational construction approvals were invented. Full deployment, target-device performance and pilot acceptance remain unfinished.

The Phase 7 verification record is [artifacts/phase-7-verification.json](artifacts/phase-7-verification.json): 237 distinct passing backend checks, 63 frontend tests and all four production-browser workflows, with no unresolved failures. The broad backend run passed 233 checks; separate browser runs complete the coverage. The single remaining skip is the full-library import already verified in Phase 3. Source hashes for all 149 GLBs and metadata files are unchanged, and the dependency audits found no known vulnerabilities. The record includes the corrected form labels, upload proxy limit, revision-history refresh and voice-test selector, along with failed-run evidence and passing rechecks. Phase 7 is verified for the engineering scope. Phase 8 has begun with local PC setup and actual paired recovery tests; deployment and evaluated-pilot acceptance remain separate.

The Phase 6 verification record is [artifacts/phase-6-verification.json](artifacts/phase-6-verification.json): 233 distinct passing backend checks, 61 frontend tests, all three production-browser workflows, clean dependency audits and unchanged hashes for all 149 source GLBs and metadata files. The long regression was interrupted after 109 recorded results; the remaining modules and final live-delivery module passed separately. All 234 collected checks are accounted for, including the single intentional full-library import skip already covered in Phase 3. Durable delivery, reconnect/reload recovery, correction during signing, semantic boundaries, expiry, retained-version behavior and protected downgrade are verified for that engineering scope. Phase 8 deployment/pilot acceptance remains separate.

The Phase 5 verification record is [artifacts/phase-5-verification.json](artifacts/phase-5-verification.json): 220 distinct passing backend checks across regression and targeted rechecks, 54 frontend tests, both production-browser workflows, real E5/pgvector and ASR checks, clean dependency audits and unchanged source hashes. It records the initial content-browser timeout and successful unchanged recheck. Phase 5 is complete for this engineering scope; candidate calibration and synthetic construction checks do not establish linguistic or pilot readiness.

The Phase 4 verification record is [artifacts/phase-4-verification.json](artifacts/phase-4-verification.json). It records 189 distinct passing backend checks across the final regression and browser runs, 54 frontend tests, actual local ASR checks, dependency scans and unchanged hashes for all 149 supplied GLBs and metadata files. It also records the intentional skips and remaining content, field-audio and deployment acceptance limits.

From `backend/`, run `uv sync --locked --extra asr --extra retrieval`, then `npm ci --prefix tools`. From `frontend/`, run `npm ci`.

```powershell
# backend/
uv run signora-inventory '../metadata_json and glb' --output '../artifacts/phase-0-inventory.json'
uv run ruff check app tests alembic
New-Item -ItemType Directory -Path artifacts -Force | Out-Null
$testPath = Join-Path (Get-Location) ('artifacts/pytest-' + [guid]::NewGuid().ToString('N'))
uv run --extra asr --extra retrieval pytest -q --basetemp $testPath

# frontend/
npm test
npm run build
npm run verify:bindings
```

PostgreSQL tests create and stop an isolated local cluster, with a random password and port. They do not use the installed service's databases. Set `SIGNORA_TEST_PG_BIN` to the PostgreSQL binary directory if it differs from `C:/Program Files/PostgreSQL/18/bin`. Without those binaries integration tests explicitly skip.

`artifacts/phase-0-inventory.json` records all 149 exact hashes, embedded clip selectors, duration differences, and unchanged source statuses. `artifacts/phase-0-bindings.json` records full-library headless checks. Texture decoding, WebGL appearance, linguistic quality and passenger-display performance are not established by these checks.

The opt-in browser test builds the production frontend, starts disposable PostgreSQL/API/frontend processes, and plays the unchanged TRAIN, 1_ONE, and ZERO GLBs through Chromium with real texture decoding. It checks 1/3/12 occurrences, repetition, the same avatar instance, warm caching, missing-asset failure, preserved unsupported text, mobile layout, and credentials held only in memory. Port 8000 must be free; all processes started by the harness are stopped afterward. From `frontend/`, first run `npx playwright install chromium`. Then from `backend/`:

```powershell
$env:SIGNORA_TEST_BROWSER = '1'
$testPath = Join-Path (Get-Location) ('artifacts/pytest-browser-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/test_browser_playback.py -q --basetemp $testPath
Remove-Item Env:SIGNORA_TEST_BROWSER
```

Browser screenshots and measured timings are saved under `artifacts/phase-2-browser/`. These are local Chromium engineering checks; they do not establish target-display performance or linguistic/composition approval.

Set `SIGNORA_TEST_FULL_IMPORT=1` to include `tests/test_full_import.py`. It imports all 149 supplied assets into disposable storage/PostgreSQL, resumes after five items, and verifies an unchanged rerun. It requires approximately 9 GB of free space for its retained test artifacts and writes `artifacts/phase-3-full-import.json`. Lifecycle integration tests exercise real staged GLBs and explicit synthetic decisions only inside isolated test databases; they do not approve the supplied library. The regular suite also checks process restart, bad-item isolation, bounded retries, concurrent revisions, failed activation transactions, shared storage, and cleanup recovery.

## Registry application

Set `SIGNORA_DATABASE_URL` to a dedicated `postgresql+psycopg://...` database URL and `SIGNORA_STORAGE_ROOT` to a persistent directory. The migration account needs schema creation and `pg_trgm`/`vector` installation privileges. Install the pgvector extension binaries for the exact PostgreSQL major version before applying migrations. Use a separate restricted application account after migration. Phase 5 was tested with PostgreSQL 18 and pgvector 0.8.6 built from its pinned upstream tag; the isolated Windows test distribution is `backend/artifacts/postgres18`, so set `SIGNORA_TEST_PG_BIN` to its `bin` directory here. The installed PostgreSQL service was not changed.

Set `SIGNORA_PRINCIPALS` to a JSON object mapping SHA-256 credential digests to `{ "subject": "actor-id", "roles": ["admin"], "expires_at": "timezone-aware expiry" }`. Announcement operators also require an explicit `station_ids` list. Admins and content reviewers can access configured stations globally; display credentials cannot submit announcements or recordings. Generate random credentials outside source control; supply their original values as Bearer tokens. Empty credentials deny all admin access. Use HTTPS at the deployment boundary. Do not expose the local development listener publicly.

From `backend/`:

```powershell
uv run alembic upgrade head
uv run --extra asr --extra retrieval uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

The authenticated API provides `GET /api/v1/admin/signs` and `POST /api/v1/admin/motions/stage`, with multipart file fields `metadata` and `motion`. `/docs` exposes the typed request contract. `/health/live` and `/health/ready` distinguish process health from registry readiness. Asset uploads are bounded and limited to two concurrent validations per process. Voice uploads use a separate single lane with a 2 MiB file limit plus 64 KiB multipart overhead. Other request bodies are limited to 32 KiB. Both declared length and actual received bytes are checked before processing; authentication and upload roles are checked before multipart parsing.

Staging stores immutable checksum-addressed bytes before committing registry, audit and event records. Repeating identical input returns `UNCHANGED`; changed metadata requires explicit review. Source review values remain evidence and are not automatically promoted into runtime approvals. Failed registration may leave an unreferenced immutable object, never an active pointer to missing bytes.

Migration head is `0008_library_reupload`. It permits a fresh version ID after deletion and records development cleanup scope; `0007_library_workflow` added explicit development-library selections and metadata revision history. Downgrading from 0008 is refused once duplicate historical checksums exist: use a forward repair or the pre-upgrade database backup. Apply migrations with the database owner before starting the API; the local-PC launcher does not migrate automatically. Phase 6 adds durable publication, display delivery and recovery. Phase 5 adds scoped alias review bindings, reviewed retrieval profiles and immutable 384-dimensional embeddings. An exact cosine scan shares the central eligibility view; no approximate vector index is installed. Migration tests cover an empty round trip and preservation of existing assets and pinned playback records across a populated round trip. Existing reviews without a semantic-revision binding remain stored but require an explicit bound review before new selection. Phase 4 adds station revisions, immutable template definitions/dependencies, exact construction-review bindings, finalized transcript receipts and immutable input/manifest references. Retained construction reviews protect their motion versions from deletion.

Stop workers and API writers and take a paired database-and-assets backup before deployment. The Phase 4 downgrade refuses to discard retained announcement previews; rolling back after these exist requires a compatible paired pre-upgrade backup or a forward repair. Without announcement previews, downgrading drops Phase 4 station/template/input/transcript records. Downgrading to `0002_playback` also drops Phase 3 review/job/deletion records and review bindings; downgrading to `0001_registry` drops playback records; `alembic downgrade base` **removes registry data**. Schema downgrade cannot restore physically deleted objects. No production backup/restore or pilot readiness is claimed by these tests.

## Bulk import and lifecycle

### Library: upload, metadata and retained versions

Open `/admin`, connect with an administrator token, and select **Library**.

1. **New sign:** choose **Completely new concept**, supply its matching metadata JSON and GLB, then **Validate and stage upload**. Identity comes from `motion_identity.motion_code`, not the filename. Existing identities are rejected in this mode; select the existing concept to add a version instead.
2. **Replacement:** search/select the concept, choose **New GLB version for**, and upload the matching pair. Different GLB bytes create a new immutable version. Uploading alone does not switch retrieval. Identical bytes and metadata return `UNCHANGED`.
3. **Activate:** choose the version, enter a change reason, confirm the intended change, and click **Activate selected version**. The local development configuration uses that version on the next retrieval request without restarting. Production activation still requires its existing reviews. Development activation does not grant those approvals.
4. **Metadata:** expand **Metadata file and revisions**, download the current JSON, edit it, choose the updated file, and save. GLB hashes, sizes, animation identity and concept identity must remain consistent. Meaning/label/alias edits create a semantic revision and disable selection until reactivation. The original metadata and prior revisions remain stored.
5. **Retain/undo:** replacement retains the previous GLB. Select an archived version and use rollback to switch retrieval back. **Archive** removes that version from new selection; **Restore** returns it to staging, then activate it explicitly. **Deactivate** disables the selected concept without deleting bytes; **Reactivate** validates and re-enables it. Revocation is a separate, irreversible withdrawal of a defective version.
6. **Delete and readd:** select the sign, then click **Delete GLB + metadata** beside the version selector. Enter a reason and type its exact identity (for A: `ISL_A_01`). Confirm with **Permanently delete selected GLB + metadata**. On the development server this removes the selection, skips the retention wait and invalidates old content previews/development display plans; production plans, construction bindings and canonical avatars remain protected. Production mode still requires archival and the configured retention interval. Wait for **Managed GLB cleanup: COMPLETE**, then upload the original matching JSON + GLB under **New GLB version for: A** and activate it. Identical re-uploaded bytes receive a fresh version ID; old plans remain invalid. The new-concept choice also accepts a retained identity whose versions are all deleted in development mode. Minimal IDs, checksums and audit records remain. Original source files are preserved. Shared bytes remain until no retained version uses them.

GLB uploads are limited to 128 MiB and metadata to 32 MiB. A GLB replacement retains concept identity and aliases. Already prepared manifests pin their old versions; activation changes new retrieval requests, not those snapshots. Metadata changes are versioned independently of GLB bytes; rollback selects an older GLB, not an older concept meaning.

The additional routes, all under `/api/v1`, are:

| Method and route | Purpose |
|---|---|
| `POST /admin/motions/stage` | Multipart new concept; `new_concept_only=true` prevents accidentally targeting an existing identity |
| `POST /admin/signs/{concept}/motions` | Multipart replacement version for that concept |
| `GET /review/signs/{concept}/motions/{version}/metadata` | Current metadata and revision descriptors |
| `POST /admin/signs/{concept}/motions/{version}/metadata` | Multipart metadata update with `expected_revision` and `reason` |
| `POST /admin/signs/{concept}/motions/{version}/archive` or `/restore` | Version retention actions with revision/reason |
| `DELETE /admin/signs/{concept}/motions/{version}` | Protected cleanup; optional `purge_metadata: true` |

### Template and coverage: an example to try

**Library** is the vocabulary: TRAIN, PLATFORM, ARRIVE and number signs, each mapped to retained GLB versions. **Template** is an ordered recipe with variable slots, such as train number and platform number. **Coverage** asks whether its allowed inputs can be realized using the required concepts, compatible avatar and, in the strict workflow, exact reviewed versions. Having every vocabulary item is only one part of strict coverage.

In **Templates & coverage**, expand **Try a template and understand coverage**, then click **Load arrival example from library**. This builds an editable draft using your real concept IDs, with TRAIN, PLATFORM, NOW RIGHT NOW, ARRIVE and digits 0, 1, 2. It does not create approvals or publish anything. Click **Stage template version** to save the draft, inspect **Exact dependency bindings**, choose Nagpur as the example station, and enter the train/platform fields.

| Example announcement | What it demonstrates |
|---|---|
| `Train 1201 is arriving at platform 2.` | Values explicitly included in the example recipe; preserves repeated digit 1 |
| `Train 1202 is arriving at platform 1.` | The same pattern with different supported slot values |
| `Train 9999 is arriving at platform 2.` | Outside the example's train values, even if digit 9 exists in the library |
| `Train 1201 is arriving at platform 3.` | Outside the example's platform values, even if station platform 3 exists |

Use **Prepare construction example** to see the strict result. Unreviewed dependencies can correctly report unavailable even when development announcement playback works. After genuine content and construction review, the same controls enable the strict template. Replacing a dependency can invalidate its exact-version construction review without removing the vocabulary concept. The generated recipe is a learning draft, not linguistic evidence.

Configure `SIGNORA_IMPORT_ROOTS` as a JSON mapping of administrator-selected aliases to existing source directories, for example `{"library":"D:/ai_retrival_based system/metadata_json and glb"}`. Each root contains `metadata/` and `glb/`; the API accepts configured aliases, never arbitrary filesystem paths. The API and worker must use the same database, storage root and import-source configuration. Use a persistent storage directory outside the supplied source library.

An admin creates a snapshot with `POST /api/v1/admin/imports`, providing `source_alias` and a fresh UUID `request_id`. Repeating that ID returns the original job. `GET /api/v1/admin/imports/{job_id}` reports counts and paginated per-item outcomes (`limit` up to 100, `offset`), including actionable errors. Run the durable worker from `backend/`:

```powershell
uv run signora-worker --workers 2
# To drain only currently runnable work, then exit:
uv run signora-worker --once --workers 2
```

The worker limits import validation to two slots across worker processes using the same database. It pins metadata/checksums, renews five-minute item leases, isolates invalid items, and retries temporary failures up to three attempts with backoff. A restarted worker resumes persisted work; `--once` does not wait for future retry times. `POST /api/v1/admin/imports/{job_id}/pause` prevents new claims while allowing current items to finish. `/resume` accepts `{"retry_failed":true}` to grant retryable failures another three attempts. Corrected source files require a new snapshot. Importing different GLB bytes stages a candidate without changing the selected active version.

Reviewers use `GET /api/v1/review/signs/{concept_id}` for concept revisions, aliases, version history, reference counts and review evidence, and `GET /api/v1/review/avatars` for exact avatar identities. Typed review routes are available in `/docs`. Meaning review is separate from source metadata. Motion review requires the current concept revision, exact SHA-256, avatar, semantic revision, a current content preview owned by that reviewer, explicit rendered-review confirmation, and recorded evidence. Motion composition approval covers exact content; multi-clip construction requires the separate template review below. Playing a preview or importing source review fields does not silently grant approval.

Admins add candidates with `POST /api/v1/admin/signs/{concept_id}/motions` using multipart `metadata` and `motion` fields. `/activate` and `/rollback` require the candidate version, expected current pointer and concept revision. `/deactivate`, `/reactivate`, and `/motions/{version_id}/revoke` require the current revision and a reason. Changes preserve concept identity and aliases; GLB quality replacement preserves the semantic revision. Activation and rollback verify retained bytes and current approvals, then switch the pointer and commit audit/events atomically. Deactivation preserves the selected version; revocation blocks that exact version from playback authorization.

`DELETE /api/v1/admin/signs/{concept_id}/motions/{version_id}` requires the current revision, exact checksum confirmation and reason. It refuses active or protected versions and enforces `SIGNORA_DELETION_RETENTION_DAYS` (default 30, minimum 1) since the latest version activity/review. Retained manifests, canonical avatars and selected versions remain protected. Accepted deletion preserves the version record and queues physical cleanup after commit. Shared objects remain stored while another live version needs them. The same worker processes cleanup, and `GET /api/v1/admin/cleanup/{job_id}` exposes its result. After three failed attempts, an admin can use `/retry` with `expected_attempts` and a reason to grant three more; the cumulative history is preserved. `GET /api/v1/admin/audit` provides paginated lifecycle/import audit records.

## Content playback

From `frontend/`, run `npm run dev` and open `http://127.0.0.1:3000`. For a production build, run `npm run build` followed by `npm start`. Set `SIGNORA_BACKEND_URL` before building if the backend differs from `http://127.0.0.1:8000`; it must be an origin without credentials. Browser requests use same-origin API rewrites, and access tokens are held in memory only.

Use a configured `reviewer` or `admin` token to browse staged motions, choose the avatar source, arrange a review sequence, prepare every asset, and play. The avatar stays fixed until the session is reset. Source metadata and runtime approvals remain unchanged. Explicit review cuts provide content inspection, not an approved sentence construction.

`GET /api/v1/review/motions` and `POST /api/v1/review/prepare` support that workspace. `POST /api/v1/playback/prepare` accepts exact English canonical text and an avatar profile ID for admin/reviewer/operator roles. Whole-expression matching preserves whitespace-normalized text and returns `UNSUPPORTED`, `NEEDS_REVIEW`, or `ASSET_UNAVAILABLE` when it cannot produce a complete plan; it never decomposes arbitrary announcements into words.

Prepared plans are private to the creating principal and expire after 15 minutes. They contain at most 64 occurrences and ten minutes of motion. The client verifies SHA-256, embedded resources, texture decoding, rig compatibility, bindings, and complete readiness before signing. `GET /api/v1/playback/{manifest_id}` revalidates authorization, freshness, eligibility where applicable, and all pinned bytes immediately before each play/replay. Revoked or unavailable versions block the entire plan. Immutable asset responses require the same principal and exact manifest/version/hash membership. Published snapshots follow the retained-version lifecycle rules under live delivery below.

## Announcement input and reviewed constructions

Open `/announcements` with a station-scoped operator or reviewer/admin credential. Keyboard text, structured fields and finalized voice transcripts enter `POST /api/v1/translate`. The request contains a UUID `request_id`, `station_id`, `input_type` and either text or structured fields. Structured input generates its own caption. Repeating the same request ID/content returns the original receipt after freshness checks; reusing the ID for different content returns a conflict. Editing fields in the browser invalidates the previous preview and voice confirmation.

The initial parser supports bounded English arrival/departure states, cancellation, explicit delay quantities and directional platform changes. It preserves leading zeros, repeated digits, configured suffixes, negation, temporal state and source/destination roles. Examples of the input grammar are `Train number 00110 is arriving on platform 3.`, `Train 00110 has not departed from platform 3.`, and `Platform for train 00110 has changed from 3 to 4.` These describe parsing, not approved signing sequences. Unknown grammar, extra clauses, multiple announcements, missing dependencies or ambiguous coverage produce a complete-message review/unsupported response with the source text retained. Clock times require a service date and explicit AM/PM for otherwise ambiguous 01:00–12:59 values, interpreted in Asia/Kolkata. Delays require an explicit minutes/hours unit. Name lookup requires configured unambiguous entities.

Configure production content through the typed API contract in `/docs`:

1. An administrator writes `POST /api/v1/admin/stations/{station_id}` with the actual name, platform inventory, train identifier rules, optional place/train names and aliases, an `expected_revision` (0 for creation), and a reason. No real station is seeded from illustrative examples.
2. Stage a version with `POST /api/v1/admin/templates`, providing its stable key, `expected_version`, approved candidate definition and reason. Definitions explicitly cover intent, temporal state, polarity and every slot exactly once. Baked values must declare exact `fixed_slots`. Realization policies use explicit concept IDs: exact values, identifier characters with all ten digits and optional suffixes, or a reviewed ISL alphabet plus an explicit entity-to-spelling map for names. Quantity/clock slots cannot use identifier spelling. Semantic groups are contiguous; safe interruption points can only follow complete groups.
3. A reviewer calls `POST /api/v1/review/templates/{id}/prepare` with a station and structured example. Open the returned manifest ID under **Prepared review** in the existing content workspace with the same reviewer identity. Inspect actual rendered signing order, repeated occurrences, full-clip cuts and group boundaries. Preview examples must collectively include every dependency used by the recipe and its policies.
4. Record the decision through `POST /api/v1/review/templates/{id}` with the expected revision, definition hash, preview IDs, rendered-review confirmation, evidence and reason. Arbitrary manually assembled motion previews cannot approve a template. An administrator then enables the reviewed version through `/activation` with its current revision. `GET /api/v1/review/templates` lists versions and bindings.

A complete unique approved template produces a schema-3 `ANNOUNCEMENT_PREVIEW` with `operational: false`, pinned motion bytes, the template/review/station revisions, complete meaning, hash and ordered semantic groups. Individual digit or letter boundaries inside an identifier/name are not safe interruptions. Every dependency must remain eligible; quality replacement preserves concept identity but invalidates the construction binding until it is reviewed against the new exact motion version. Prior definitions, reviews and manifests remain retained. Changes to station configuration or template activation also invalidate private previews. No sentence GLB is generated.

## Local push-to-talk speech

ASR is an optional deployment dependency. Install and provision its pinned model explicitly from `backend/`:

```powershell
uv sync --locked --extra asr --extra retrieval
uv run --extra asr --extra retrieval signora-asr-download artifacts/asr-model
$env:SIGNORA_ASR_MODEL_PATH = (Resolve-Path artifacts/asr-model).Path
uv run --extra asr --extra retrieval uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Provisioning downloads `Systran/faster-whisper-base.en` revision `3d3d5dee26484f91867d81cb899cfcf72b96be6c` and records checksums for its four required files. Serving checks that snapshot and uses local CPU INT8 inference in a reusable child process; it never downloads model weights. A single inference lane per API process rejects overload with 429. Startup warming is independent of deterministic API readiness. `GET /api/v1/input/capabilities` reports ASR state and authorized station definitions. `SIGNORA_ASR_TIMEOUT_SECONDS` defaults to 90 (5–120 allowed); a stalled worker is terminated, and a subsequent request can restart it. Typed input stays available during ASR failure.

Browser capture requires HTTPS or localhost and user microphone consent. Push-to-talk uses AudioWorklet to capture 16 kHz mono PCM WAV, limited to 0.25–60 seconds. `POST /api/v1/voice/transcribe` accepts the `audio` multipart field, validates its actual format/duration and rejects silence before inference. Raw audio is staged temporarily and removed after inference. The database retains an owner-bound final transcript, model revision, diagnostics and audio checksum. Voice translation requires that server-issued receipt and explicit confirmation of the transcript and critical fields; no partial transcript or client-supplied ASR score authorizes signing. Correction is supported and invalidates previous browser confirmation.

Receipts and previews expire for use after at most 15 minutes. Expiry does not delete their audit records or raw text; define database access and retention for the deployment before live use. Raw transcripts are excluded from routine request/event logs. No publication is performed by these routes.

Phase 4 tests cover conservative meaning parsing, exact typed realization, complete arrival/departure/platform-change mechanics, station/owner authorization, concurrent retries, actual upload size enforcement, stalled ASR termination, exact construction-review evidence and motion replacement invalidation. Synthetic recipes and approvals exist only in disposable test databases. Run the complete optional browser/ASR checks after provisioning the model and Chromium:

```powershell
# backend/; uses the synthetic WAV under artifacts/, not a live microphone
$env:SIGNORA_TEST_BROWSER = '1'
$env:SIGNORA_TEST_ASR = '1'
$testPath = Join-Path (Get-Location) ('artifacts/pytest-phase4-' + [guid]::NewGuid().ToString('N'))
uv run --extra asr --extra retrieval pytest -q --basetemp $testPath
Remove-Item Env:SIGNORA_TEST_BROWSER, Env:SIGNORA_TEST_ASR
```

The local ASR check uses synthetic speech and verifies finalized recognition, worker reuse and confirmation requirements; it does not establish accuracy for noisy railway audio. The browser check uses Chromium's fake audio device with that WAV, the real capture worklet, actual local ASR, the production Next.js build and supplied GLBs. Reports and screenshots are under `artifacts/phase-4-*`. Field audio accuracy, linguistic/composition approval, hardware microphone interoperability, target-device latency and the operational pilot remain separate acceptance work.


## Progressive retrieval

Reviewer/admin routes under `/api/v1/review/retrieval` add explicit content review and candidate assistance. They reuse existing Bearer identities, revision checks, immutable review evidence and audit events:

- `POST /concepts/{concept_id}` reviews a curated description and a sense signature (`semantic_class`, `polarity`, `temporal_state`, exact `literals`) within the concept's reviewed `domain`/`context`. Include `expected_revision`, `decision`, `evidence` and `reason`. Changing the sense requires a new semantic meaning revision.
- `POST /concepts/{concept_id}/aliases` approves or rejects an English alias in that same scope. Source aliases remain unavailable until this explicit review. Rejection withdraws that alias and invalidates the previous embedding input hash.
- `POST /index` accepts up to 32 `concept_ids`. Each item returns `INDEXED`, `UNCHANGED`, or `PENDING` with a reason code. Repeat the request to resume; successful immutable rows are reused. Inference holds no database transaction or catalog lock, and changed semantic inputs are rejected before insertion. Quality-only GLB replacement keeps existing embeddings.
- `POST /search` accepts `text`, `source_language: "en"`, `domain`, `context`, `avatar_profile_id`, optional `levels`, optional `sense`, and `semantic`. Exact identity and reviewed aliases run first. Approximate candidates retain separate trigram/cosine scores, method, selected motion/checksum, ambiguity flags, model revision and diagnostic trace. Candidates are revalidated after inference. Without an explicit matching sense, even exact matches require review. No search response contains a playable announcement manifest.
- `GET /status` reports encoder readiness separately from deterministic service readiness. Operators and display credentials cannot call these review routes.

Provision the optional encoder once from `backend/`:

```powershell
uv sync --locked --extra asr --extra retrieval
uv run --extra asr --extra retrieval signora-encoder-download artifacts/e5-model
$env:SIGNORA_ENCODER_MODEL_PATH = (Resolve-Path artifacts/e5-model).Path
uv run --extra asr --extra retrieval uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

The encoder is `intfloat/multilingual-e5-small` revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, using its FP32 ONNX weights, query/passage prefixes, attention-masked mean pooling and L2 normalization. Local snapshot hashes are verified at startup; serving does not download models. One bounded child process per API worker is warmed once and reused, with a 64-entry revision/policy/prefix-aware cache. `SIGNORA_ENCODER_TIMEOUT_SECONDS` defaults to 60 (allowed 5?120); timeout or corruption stops the child, and busy inference returns an explicit reason. Missing optional dependencies/model files disable semantic assistance while exact/alias/template construction paths continue.

Candidate policy `e5-trgm-review-v1` uses development-calibrated minima 0.20 (trigram) and 0.83 (cosine), and a 0.03 ambiguity margin. These are raw score cutoffs, not confidence probabilities. The fixed English engineering split uses separate development and held-out intent families, plus critical opposite/value/role tests. Its scope is small candidate-assistance evaluation, not railway field accuracy or linguistic acceptance. All approximate candidates remain review-only. The reproducible test and measured report are `backend/tests/test_retrieval_evaluation.py` and `backend/artifacts/phase-5-retrieval-evaluation.json`; the report also labels its 149-row exact-scan benchmark as synthetic.

Complete announcement compilation uses the existing reviewed construction system: `retrieval_stage` defaults to `TEMPLATE`, followed by `EXACT`, `PHRASE`, then `WORD`. Each stage must cover all meaning and resolve eligible version-bound dependencies. An exact construction has one complete concept and fixed literal slots; phrase/word stages enforce their declared content level. A stage without a usable covering can fall back to the next separately reviewed construction. Competing valid constructions at the same stage require review. Typed number/name policies and permitted ISL fingerspelling preserve order, repeats, exact values and atomic semantic groups. No English word splitting or approximate automatic publication is introduced.

To verify Phase 5 locally, set `SIGNORA_TEST_PG_BIN` to the extension-enabled PostgreSQL `bin`, set `SIGNORA_TEST_ENCODER=1`, and run the backend suite with both extras. `SIGNORA_TEST_ASR=1` and `SIGNORA_TEST_BROWSER=1` include actual local voice and production-browser regressions. The encoder tests require the explicitly provisioned model; they skip without the opt-in flag.

Before a Phase 5 schema downgrade, stop writers and take a paired backup. Downgrading to `0004_announcement_input` drops retrieval profiles, embeddings and new alias review bindings; retained alias text alone cannot regain approval on upgrade. The shared vector extension is retained. Restore a compatible backup or re-review/reindex these records to recover retrieval assistance. Older Phase 4 binaries also do not honor the new construction-stage priority: disable new `EXACT`/`PHRASE`/`WORD` constructions before an application rollback, or restore the paired pre-upgrade database. This rollback procedure has no live deployment or disaster-recovery acceptance claim.

## Live publication and display recovery

Phase 6 adds migration `0006_live_delivery`. Publication, its immutable revision/manifest, audit record and outbox event commit together. A station row lock orders committed events. Each gateway reconciles PostgreSQL directly, so reconnecting to another API process uses the same durable state. PostgreSQL remains the authority; no separate broker or process-local replay queue is required.

An operator/admin first prepares a current confirmed announcement through `/api/v1/translate`, then calls `POST /api/v1/announcements` with `station_id`, a stable `source_event_id`, positive `source_revision`, `expected_revision: 0`, the returned `preview_manifest_id` and `preview_manifest_hash`, and a reason. The preview must belong to that publisher. Publication recompiles the confirmed meaning against the current reviewed catalog. Repeating identical source identity/revision/content returns its original receipt; conflicting content or obsolete revisions fail. No voice or translation route publishes automatically.

Use `POST /api/v1/announcements/{message_id}/revisions` for corrections, with the same source identity, a higher source revision, the expected current revision and a fresh preview. An explicit withdrawal uses `cancel: true` and omits preview fields. This withdrawal removes that event from live signing; a signed train-cancellation announcement instead requires a reviewed `TRAIN_CANCELLATION` construction. Published plans retain their original validity, at most 15 minutes from the input receipt, and must have sufficient remaining time for a complete performance. Expired work cannot restart. A station accepts at most 32 distinct unexpired live messages; overload returns 429. Supported cancellation/platform-change constructions receive P1 and arrival/departure work P2. The scheduler understands P0/P3, but this phase does not invent emergency/general-information constructions or allow arbitrary operator priority escalation.

Configure a separate expiring, station-scoped principal with the `display` role and register its exact subject with `POST /api/v1/admin/displays/{display_id}`. The admin request supplies `station_id`, `subject`, `name`, `expected_revision: 0` and optional `enabled`. Updating registration requires its current revision and invalidates the existing session; device subject and station remain fixed. Open `/display` and enter that ID and credential. Tokens remain in memory. Only cursor and up to 128 completed manifest IDs persist in local storage.

Socket origins are denied by default. Configure the exact frontend origin, and apply the transport payload limit when running the API:

```powershell
# backend/; existing database, storage and principal settings must also be configured
$env:SIGNORA_WEBSOCKET_ORIGINS = '["http://127.0.0.1:3000"]'
uv run --extra asr --extra retrieval uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --ws-max-size 4096
```

The frontend's same-origin `/api/v1` rewrite carries HTTP and WebSockets. Deployment requires its actual HTTPS origin and WSS-capable proxy configuration. `SIGNORA_DISPLAY_LEASE_SECONDS` defaults to 15, `SIGNORA_DISPLAY_POLL_SECONDS` to 1, and `SIGNORA_DISPLAY_CONNECTION_LIMIT` to 64 per API process. Keep the polling interval comfortably shorter than the lease. Authentication is the first bounded `HELLO` frame, never a URL parameter. The server checks enabled device, subject, station, credential expiry and session identity during reconciliation and acknowledgements. A newer connection fences an older session. Authentication/protocol rejection stops automatic reconnect; transient network/database failures use bounded backoff.

Each `SYNC` contains authoritative current messages, stream cursor, server time and a freshness lease. Missed obsolete events carry cursor tombstones, never playable obsolete manifests. If the replay range exceeds 128 events or predates the replay floor, the complete snapshot replaces replay. Outbox/history records remain retained; automated retention/compaction is not implemented. Displays acknowledge `RECEIVED`, `ASSETS_READY`, `STARTED`, `COMPLETED` and `FAILED` separately. Delivery attempts, current progress and immutable acknowledgement history persist in PostgreSQL. A committed offer is an attempt, not proof of display receipt or physical signing.

The player downloads and verifies every required asset before starting on one persistent avatar. Corrections update captions immediately and replace signing only after reviewed semantic groups; a digit inside a train number is not a safe stop. Superseded preload callbacks cannot start. Lost freshness pauses signing even with cached GLBs. A reconnect suppresses completed manifests and restarts any still-current incomplete message from its beginning after revalidation, never from the middle of an identifier. If local completion survived but its server acknowledgement was lost after `STARTED`, the display reports completion without replay. A crash before either completion record persists can still cause a complete-message repeat; visible exactly-once signing is not promised.

Routine quality replacement preserves valid published snapshots pinned to retained reviewed versions. Concept deactivation, version revocation, rejected approval, template withdrawal or changed station configuration invalidates affected live work and emits durable withdrawal events. Disconnected displays enforce their freshness lease; they cannot receive immediate revocations. `GET /api/v1/admin/displays/{display_id}` reports received cursor lag, last-seen time, lease and the latest 64 delivery states/failures. Structured delivery logs carry IDs/state/error codes, excluding credentials and announcement text. This provides the backend for Phase 7 monitoring interfaces.

For Phase 6 verification, run the backend suite with `SIGNORA_TEST_PG_BIN`, `SIGNORA_TEST_BROWSER=1`, `SIGNORA_TEST_ASR=1`, and `SIGNORA_TEST_ENCODER=1` after provisioning the existing models and Chromium. The additional live-browser harness uses a production Next.js build, actual WebSocket transport, disposable PostgreSQL and source GLBs. All publication/review fixtures remain synthetic and isolated. Source-feed integration, target-display performance and an operational pilot are not established by these checks.

Before upgrading, stop writers and retain the matching application build, PostgreSQL backup and immutable asset storage snapshot. Empty Phase 6 schemas support downgrade/upgrade testing. Once any announcement revision exists, downgrade deliberately refuses to erase publication history. Roll back by restoring the paired pre-upgrade database/assets and matching application while display/publisher processes are stopped; Phase 5 binaries cannot serve schema-4 live plans. Actual backup restoration and deployment fault acceptance remain Phase 8 work.

## Operator and management workspaces

Phase 7 uses the existing authentication, review, publication and lifecycle services. It adds no database migration: the required head remains `0006_live_delivery`. Open `/announcements` with an operator/admin credential to prepare typed, structured or voice input, inspect the exact values and play the complete private preview. Publication requires an explicit reason and confirmation. The station table supports correction, withdrawal and revision history; display status distinguishes a fresh connection from reported playback completion. An ambiguous publication failure exposes **Retry same publication request**, preserving the source identity and request body. Status tables are snapshots with explicit refresh controls.

The Next.js same-origin rewrite allows the backend's bounded multipart envelope (160 MiB plus 64 KiB) and a 180-second upstream timeout, so source GLBs are not truncated by the default 10 MB buffer. These pinned Next.js proxy settings are experimental; the Phase 8 deployment proxy should route `/api/v1` directly to FastAPI and enforce upload size, concurrency and timeouts at ingress. The browser limits a GLB to 128 MiB and metadata to 1 MiB; the backend independently enforces received bytes, authenticated roles, validation concurrency and content checks.

Open `/admin` with a reviewer or administrator credential. Credentials stay in memory and disconnect clears the workspace. Backend role checks apply independently of which tabs are visible:

- **Library:** search concepts; inspect technical findings, semantic status, exact motion hashes, review history and affected templates; preview a selected candidate, current active version or canonical avatar on the persistent player. Review evidence applies to the exact rendered version. Administrators can stage supplied JSON/GLB files, activate, deactivate, reactivate, revoke or select a retained version for rollback. Permanent deletion requires the exact checksum and remains subject to server reference/retention checks. Cleanup failures expose the existing retry workflow.
- **Templates & coverage:** inspect intent, temporal state, polarity, literal values, realization policies and exact dependency bindings. Render enough examples to cover the construction before recording review evidence. Administrators stage complete definition JSON and enable or disable reviewed versions. Staging alone does not authorize publication.
- **Imports:** administrators inspect persisted jobs and individual validation failures, pause/resume within the retry policy and recover an identical import request. Processing still requires the existing configured worker.
- **Stations:** administrators inspect and revise actual station definitions with expected-revision checks and an explicit warning that affected live work is withdrawn. No station is seeded by the interface.
- **Displays:** administrators register identities against separately provisioned display credentials, inspect freshness/backlog and recorded delivery failures, and enable or disable a registration. Updating a registration fences its previous session.
- **Audit:** administrators page through committed changes and their recorded details.

New authorized read routes are `GET /api/v1/session`, `/review/signs`, `/review/signs/{id}/impact`, `/review/templates/{id}`, `/admin/imports`, `/announcements`, `/announcements/{id}` and `/operations/displays`. Station activity requires `station_id`; paged lists accept bounded `limit`/`offset`. Search parameters are bound as data. Responses exclude credential hashes, display session secrets and host import paths. Existing mutation routes retain optimistic revision checks and conflict responses. Canonical-avatar results identify the retained source motion used for exact playback; validation findings and retrieval-profile status are available in concept details.

The opt-in `backend/tests/test_browser_workspaces.py` runs these screens against disposable PostgreSQL, actual source GLBs, a production Next.js build and a real display WebSocket. All staged variants and approval records belong to that isolated test fixture. Run with `SIGNORA_TEST_BROWSER=1` and the existing PostgreSQL setting. Phase 7 does not establish linguistic comprehension, target-device performance or deployment acceptance.

For application rollback, stop writers, retain the paired database/assets backup and restore the matching Phase 6 application build on schema `0006_live_delivery`; no Phase 7 schema downgrade is needed. Content changes made through the interface are durable existing lifecycle operations and are not undone by changing the application build. Use retained-version lifecycle rollback where appropriate. The protected Phase 6 downgrade and recovery requirements still apply.

## Paired backup and isolated restore

`backend/app/recovery.py` provides an administrator CLI, using PostgreSQL tools of the same major version as the server. Backup shares one exported repeatable-read snapshot between the database inventory and `pg_dump`, and holds the catalog's shared lock until every retained, nondeleted GLB has been copied and verified. Lifecycle changes and cleanup wait for this lock; schedule backups outside busy maintenance periods. Historical/revoked/staged objects are included when retained. Deleted objects awaiting cleanup are not required. Every copied object and the database dump has a SHA-256 digest and byte count. A completed backup directory appears only after the manifest is written and the temporary directory renamed. Failed `.partial-*` directories are retained for diagnosis.

From `backend/`, with the normal local `.env`:

```powershell
$backupDirectory = 'D:\SignoraData\backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
uv run --extra asr --extra retrieval python -m app.recovery backup $backupDirectory --pg-bin artifacts/postgres18/bin
uv run --extra asr --extra retrieval python -m app.recovery verify $backupDirectory
```

Restore accepts only a **trusted backup**, an **empty offline PostgreSQL database** and a **new asset directory**. It never cleans or overwrites a target. Matching schema `0006_live_delivery`, PostgreSQL major version and extension versions are checked. Use a target owner account with extension-creation privileges; pass its URL through `SIGNORA_RESTORE_DATABASE_URL`, never as a command-line argument. The restore has no fallback to the source `.env` database. After provisioning that empty target and setting its URL:

```powershell
uv run --extra asr --extra retrieval python -m app.recovery restore $backupDirectory --storage-root D:/SignoraData/restored-assets --pg-bin artifacts/postgres18/bin
Remove-Item Env:SIGNORA_RESTORE_DATABASE_URL
```

The database restore, inventory validation, withdrawal of restored live announcements, display-session fencing, disabled display registrations and restore audit commit in one transaction. Prior immutable manifests, reviews, revisions and delivery history remain retained. This prevents a backup from replaying an announcement cancelled after its snapshot. A successful restore writes `restore-receipt.json` in the new asset directory. Failure leaves target assets for diagnosis; do not start services against an unsuccessful target. No automatic cutover occurs. Reapply restricted application-account grants, check readiness and retained content playback, reconcile the authoritative announcement source, then deliberately re-enable display registrations and prepare fresh announcements before switching an installation.

Retain the matching application source/build, dependency locks, model snapshots and private configuration separately. Backups contain operational data and must stay in an access-controlled location. Checksums detect corruption, not an untrusted backup author; PostgreSQL archives contain executable database definitions. Backup scheduling, retention, off-PC copies and recovery-time objectives are deferred with deployment. The real isolated recovery tests in `backend/tests/test_recovery.py` cover retained-manifest asset delivery, corruption and incompatible-input rejection, populated-target refusal, cleanup exclusion, and rollback when the operational fence fails. They do not establish an operational pilot or an eight-hour display rehearsal.
