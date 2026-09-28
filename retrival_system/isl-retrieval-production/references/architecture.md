# Architecture and content boundaries


## Repository boundary

Keep `backend/` and `frontend/` as separate top-level application folders. The backend owns FastAPI, PostgreSQL/retrieval, ASR, manifest generation, admin lifecycle, and WebSocket server logic. The frontend owns Next.js/React, operator/admin/display pages, microphone UI, Three.js avatar playback, clip caching, queueing, and the browser WebSocket client. They communicate through versioned REST/WebSocket schemas and asset URLs. See `repository-structure.md`.
## Scope and module boundaries

Implement the approved retrieval system from existing compatible GLBs. Keep railway as the initial complete domain; add other public domains through reviewed content and templates. Prefer a modular backend, one PostgreSQL database, and immutable asset storage. Do not require a new controller/CRUD layer when existing service conventions work.

Keep these responsibilities explicit: intake/normalization; typed meaning parser; template compiler; constrained retrieval; asset eligibility; manifest assembly; durable announcement dispatch; asset lifecycle; persistent player. Keep HTTP handlers thin and animation scheduling outside page components.

Treat the implementation plan's latency, workload, schedule, and study-size figures as proposed targets until measured. Determine actual GLB coverage, existing code, and enabled languages from evidence; do not infer arbitrary sentence coverage from the number of files.

## Typed meaning before retrieval

Preserve original text and source spans. Normalize whitespace, supported digit forms, and reviewed abbreviations without discarding negation, temporal qualifiers, or source/destination direction. Do not use blanket stop-word removal or reverse English word order as ISL translation.

Use an intermediate meaning record containing intent, typed slots, polarity, temporal state, station/source identity, source revision, priority, and validity window. Distinguish arriving now, already arrived, scheduled arrival, departure, delay, cancellation, and platform change where the input distinguishes them.

Preserve train identifiers as strings, including leading zeros. Preserve old/new platform roles, allowed platform suffixes, named-entity identity, and delay units. Resolve clock times with date and timezone; distinguish them from durations. Use Asia/Kolkata for local Indian station times where applicable. Reject unresolved critical ambiguity instead of inventing a value.

Support both typed text and push-to-talk voice as first-class operator inputs. Voice is transcribed through an ASR adapter, then validated and converted into the same common AnnouncementInput/typed meaning path used by text. Do not maintain separate voice retrieval logic, and do not publish partial or uncertain ASR hypotheses.

Prefer an authorized structured feed or operator form for live operational events. Do not assume live railway API access exists. Language support requires tested parsing, aliases, and templates; a multilingual encoder alone does not enable a new input language.

## Content and approval

Represent complete sentences, reusable phrases, contextual concepts, typed number/name realizations, and fingerspelling as distinct content capabilities. Attach stable sense/context identity rather than using filenames as meaning.

A baked sentence containing a particular platform or train ID cannot accept a different slot through metadata. Use independently reusable motions or annotated/reviewed subclips. Do not cut arbitrary frames at request time.

Keep three gates separate:

1. Structural and avatar compatibility checks.
2. ISL review of the rendered version and required manual/nonmanual channels.
3. Approval of the composed construction, timing, and boundaries.

Preserve required face/head/body cues. If the rig or clip cannot express required information, restrict its approved use; retrieval and visually smooth blending cannot repair missing linguistic content. Different required contextual realizations need explicit concept/variant identities rather than competing active versions of one meaning.

For an example such as "Train number 12870 is arriving on platform 3", extract the exact identifier and platform first. Obtain ordering, phrase choice, number convention, and nonmanual cues from the reviewed template. Any unreviewed gloss sequence shown in a design example is illustrative, not certified ISL.

## Availability and product behavior

Use READY, NEEDS_REVIEW, UNSUPPORTED, and ASSET_UNAVAILABLE or equivalent explicit results. READY means the backend has a complete eligible plan; the client separately confirms decoded asset readiness. Preserve accurate source text and an appropriate service state when signing is unavailable. Do not publish a partial operational sentence.

Keep passenger UI focused on the avatar, readable authorized text, and useful service status. Keep gloss IDs, retrieval diagnostics, and administrative controls in operator/admin views. Establish camera/framing from hand/face visibility on actual displays.

## Evidence boundaries

Use the project's approved content records, not confidence claims generated by the agent, as linguistic evidence. Relevant background includes [WFD/WASLI avatar guidance](https://wfdeaf.org/wfd-wasli-issue-statement-signing-avatars/) and [railway announcement prior work](https://aclanthology.org/2020.icon-demos.16/). Neither certifies this project's output. Do not introduce a new interpreter, synthesis, or converter pipeline merely because older research used one.
