# Signora frontend — UI contributor guide

This guide is for someone joining the project to improve its interface. Start with the page map and styling files below, then use the existing API and avatar components while changing the design.

## Brief project overview

Signora is a retrieval-based Indian Sign Language (ISL) railway announcement application. An operator enters an announcement or records speech; the backend finds existing sign motions, builds an ordered playback manifest, and the browser plays those GLB animations on a persistent 3D avatar. A separate live display receives station announcements.

The application retrieves existing animations; it does not generate new signs or convert videos into GLBs in the browser.

```text
Text / recorded voice
        ↓
Backend: transcription when needed, normalization, meaning and motion retrieval
        ↓
Playback manifest: ordered motion versions and GLB URLs
        ↓
Frontend: load assets → prepare persistent avatar → play sequence
        ↓
Station publication → connected live display
```

There are separate `frontend/` and `backend/` applications. The backend uses FastAPI, PostgreSQL and stored GLB assets. The frontend owns the screens, microphone recording, API interactions and Three.js rendering.

**Current development behavior:** when the backend enables development mode, available motions can play without linguistic or construction approval. Grammar failure falls back to lexical retrieval, with typo recovery and available letters. Unknown input does not produce unrelated sample motions. The announcement screen sends development announcements to explicitly selected displays, or broadcasts to all enabled displays at the station. With no selected targets, playback stays private. Strict reviewed behavior remains available when development mode is disabled.

Playback success is not proof of correct ISL translation. Keep draft, lexical-recovery and incomplete-output messages visible and accurate during a redesign.

## Technology

Versions currently declared in [package.json](package.json):

| Area | Implementation |
|---|---|
| Framework | Next.js 16.3.5, App Router |
| UI | React 19.3.0, JavaScript/JSX |
| 3D avatar | Three.js 0.186.0 |
| Styling | Plain CSS in `src/app/globals.css` |
| Automated tests | Node.js test runner; Playwright browser verification scripts |
| Node requirement | Node.js 20.19.0 or newer |

There is no Tailwind or external component library in the current dependencies. You can redesign the interface with the existing React and CSS setup.

## Run the frontend

From the repository root, in PowerShell or a terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:3000**. Next.js updates the page as you edit source files.

If another instance already occupies port 3000, use:

```sh
npm run dev -- --port 3001
```

The frontend can render its initial screens alone, but working authentication, station selection, motion lists, transcription and playback require the backend, database and registered assets. Backend provisioning and the existing local-PC launcher are described in the [main project README](../README.md). That launcher is for an already configured machine; it is not a fresh-machine installer.

### Backend connection

[next.config.mjs](next.config.mjs) proxies `/api/*` to `http://127.0.0.1:8000` by default. Browser API requests use relative URLs.

To use another backend origin, set this **server-side** variable before starting Next.js. PowerShell example:

```powershell
$env:SIGNORA_BACKEND_URL = 'http://127.0.0.1:8000'
npm run dev
```

Use an HTTP(S) origin without a path or embedded credentials. Restart Next.js after changing its configuration. Live display connections also need the backend WebSocket route to work through the app origin.

Obtain appropriate development access tokens from the project owner through a private channel. Tokens are entered into each workspace; the current UI does not have a username/password login form. Do not put tokens into source files, screenshots, this README or `NEXT_PUBLIC_*` variables. Display access additionally needs a registered **display UUID**, not a station name.

## Pages and where to edit them

Paths below are relative to `frontend/`.

| URL | Screen | Main component | Purpose |
|---|---|---|---|
| `/` | Content review | [ReviewConsole.jsx](src/components/ReviewConsole.jsx) | Motion library browsing, exact-text lookup, prepared review plans and private sequence playback |
| `/announcements` | Announcements | [AnnouncementConsole.jsx](src/components/AnnouncementConsole.jsx) | Station selection; Type, Structured fields and Record voice tabs; retrieval details and avatar output |
| `/admin` | Library & operations | [AdminConsole.jsx](src/components/AdminConsole.jsx) | Management workspace with role-dependent tabs |
| `/display` | Live display | [DisplayConsole.jsx](src/components/DisplayConsole.jsx) | Display connection, current announcement caption and automatic avatar playback |

Management tabs are **Library**, **Templates & coverage**, **Imports**, **Stations**, **Displays** and **Audit**. Administrators can access all six; reviewers see Library and Templates & coverage.

Motion Library is part of the review/management workspaces, not a separate `/library` route.

The Library now supports separate new-concept/version uploads, current metadata download and revision uploads, activation, archive/restore, rollback and visible GLB + metadata deletion with exact-sign confirmation. Development deletion supports immediate delete/re-upload testing and invalidates old test plans; production protections remain. Retain these handlers during a UI redesign: upload alone does not activate a new version; restore alone does not select it. In development, the explicit library selection controls the next retrieval without restarting. Production approval remains separate. See the [Library workflow and template examples](../README.md#library-upload-metadata-and-retained-versions) for exact steps and deletion limitations.

Under Templates & coverage, **Load arrival example from library** builds editable JSON using actual catalog IDs. Its allowed examples are train 1201 or 1202 on platform 1 or 2. It is a draft, not an approved construction. The helper is [template-example.mjs](src/services/template-example.mjs); preserve its slot values, meaning coverage and ID validation when editing its presentation.

## Frontend source map

| File or folder | Responsibility |
|---|---|
| [src/app/globals.css](src/app/globals.css) | Shared colors, typography, buttons, panels, layout, responsive rules and avatar container sizing; start visual changes here |
| [src/app/layout.jsx](src/app/layout.jsx) | Root HTML layout, global CSS import and page metadata |
| `src/app/**/page.jsx` | Small route entry points that render the main console components |
| [src/components/workspace.jsx](src/components/workspace.jsx) | Shared navigation, form fields, feedback and workspace request helpers |
| [src/components/Icon.jsx](src/components/Icon.jsx) | Local SVG icons, section headings and status badges; no external icon dependency |
| [src/components/WorkspaceGuidance.jsx](src/components/WorkspaceGuidance.jsx) | Contextual help for station and display configuration |
| [public/images/railway-hero.png](public/images/railway-hero.png) | Supplied railway illustration used by the announcement header and live-display empty state |
| [src/components/LibraryManager.jsx](src/components/LibraryManager.jsx) | Motion/concept/version management UI |
| [src/components/TemplateManager.jsx](src/components/TemplateManager.jsx) | Construction templates and coverage UI |
| [src/components/OperationsManager.jsx](src/components/OperationsManager.jsx) | Station, display, dataset import and audit management components |
| [src/components/ControlRoom.jsx](src/components/ControlRoom.jsx) | Per-display targets, station broadcast, emergency priority, stop commands, platform labels and live status/history |
| [src/components/PublicationPanel.jsx](src/components/PublicationPanel.jsx) | Reviewed announcement publication controls |
| [src/components/AvatarViewer.jsx](src/components/AvatarViewer.jsx) | Three.js scene and persistent avatar; exposes playback methods to the consoles |
| [src/services/api.mjs](src/services/api.mjs) | Authenticated requests, errors, timeouts and manifest revalidation |
| [src/services/recorder.mjs](src/services/recorder.mjs) | Microphone recording; works with [pcm-recorder-worklet.js](public/pcm-recorder-worklet.js) |
| [src/services/live-display.mjs](src/services/live-display.mjs) | WebSocket connection, delivery queue, progress and acknowledgements |
| [src/services/station-setup.mjs](src/services/station-setup.mjs) | Station setup form data handling |
| [src/motion/asset-store.mjs](src/motion/asset-store.mjs) | GLB asset loading and caching |
| [src/motion/rig-validation.mjs](src/motion/rig-validation.mjs) | Animation-to-avatar compatibility checks |
| [src/motion/playback-controller.mjs](src/motion/playback-controller.mjs) | Sequence preparation, animation timing and playback state |
| `tests/` | Automated API-client, recording, station setup, player and display tests |
| `scripts/` | Browser and real-asset verification tools |

## How UI actions reach the avatar

1. `AnnouncementConsole.prepare()` sends text, structured input or the finalized voice transcript to `POST /api/v1/translate`. Voice audio first goes to `POST /api/v1/voice/transcribe`.
2. The backend returns status, meaning/retrieval diagnostics and, when available, a manifest containing the ordered motion assets.
3. The console calls `AvatarViewer.prepare(manifest)`. Asset loading and playback preparation happen through the existing motion modules.
4. In development behavior, the console starts playback automatically and sends the manifest through `POST /api/v1/development/announcements` for station delivery.
5. `DisplayConsole` uses `LiveDisplay` to receive announcements, play them and acknowledge progress. A connected but idle display can simply mean there is no current announcement.

Keep retrieval decisions on the backend. UI styling should not create new motion IDs, reorder manifest items, replace GLB URLs or simulate successful playback.

## Recommended UI editing approach

1. Read `globals.css` and the component for the screen you want to change.
2. Change typography, spacing, colors, panel layouts, navigation and responsive behavior.
3. Extract repeated presentation into small components where useful, while preserving the existing state and event handlers.
4. Check connected, disconnected, loading, empty, error, ready, playing and complete states—not just the initial page.
5. Test the real announcement-to-avatar flow before handing the changes back.

Preserve these working behaviors:

- Keep `AvatarViewer` mounted during normal state updates. Changing its React `key` or conditionally replacing it during playback can destroy the persistent scene.
- Keep a visible, nonzero-height avatar container. Its canvas fills its parent; a collapsed parent makes working playback look blank.
- Preserve form labels, focus indicators, keyboard controls, status messages and readable captions. Do not communicate status by color alone.
- Keep role permissions, request cancellation, token handling and disabled/busy states connected to their current logic.
- Keep playback controls and error reporting wired to the actual player. Do not replace handlers with visual-only buttons.
- Several Playwright scripts locate controls by labels, button text and IDs such as `announcement-token` and `announcement-text`. Update those scripts if a deliberate UI change affects their selectors.
- Leave credentials in memory. Live-display progress storage contains IDs/cursors; it is not a place for tokens or announcement text.
- Avoid changes to `src/motion/` and backend contracts for a purely visual redesign unless the task specifically requires them.

## Validate your changes

Run from `frontend/`:

```sh
npm test
npm run build
```

To serve the built frontend, stop the development instance on the same port, then run `npm start`.

For manual checks, connect with suitable development credentials and try:

| Input | Expected in the currently configured development dataset |
|---|---|
| `Train 1201 arrives at platform 2` | Eight clips, preserving digit order `1 → 2 → 0 → 1`; no spelling of `at` |
| `Train 1201 Arrives at platfrm 2` | Corrects `platfrm` to `platform`; same eight-clip sequence |
| `Train ACCIDENT` | TRAIN followed by ACCIDENT; two clips |

These counts depend on the backend mode and installed dataset. Check that the avatar visibly moves, the sequence finishes, captions remain readable and errors are displayed. Also inspect narrow/mobile widths, keyboard navigation, management tabs and the live display. Microphone testing requires browser permission and a secure browser context such as localhost or HTTPS.

For deeper verification, use [verify-private-preview.mjs](scripts/verify-private-preview.mjs), [verify-development-display.mjs](scripts/verify-development-display.mjs) and [verify-lexical-recovery.mjs](scripts/verify-lexical-recovery.mjs). Read their required environment variables first: they use the real backend and assets, and browser announcement tests can publish to the configured development station.

## Handoff to the project owner

Share the changed source files or a pull request, screenshots of the main screens at desktop and mobile sizes, and the checks you ran. Mention any changed labels/selectors, added dependencies or backend assumptions. Keep `.env` files, access tokens, `node_modules/` and `.next/` build output out of the handoff.

For system setup, backend behavior and broader API documentation, see the [main README](../README.md).
