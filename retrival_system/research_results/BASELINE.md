# Baseline implementation audit

Evaluation target: current local build, PostgreSQL schema `0009_display_routing`, station NAGPUR, development preview enabled. Source hashes and pre-existing uncommitted changes are captured in `raw/source_hashes_before.json` and `raw/environment.json`. Evaluation scripts do not change runtime source files.

| Stage | Observed implementation and boundary |
|---|---|
| Text / voice | `AnnouncementConsole.jsx:prepare/startRecording/stopRecording`; `announcement_api.py:prepare_input/transcribe_audio`. Local ASR reports READY. Actual voice accuracy needs recorded audio; a READY flag alone is not an ASR test. |
| Normalization | `meaning.py:normalize`, `lexical.py:normalize_domain`; Unicode digit, case, whitespace and bounded domain typo normalization. |
| Classification / entities | `meaning.py:parse_meaning/_domain_match/_slot`. Anchored English rules support five intents. No emergency intent in `announcement_schema.py:Intent`. Unmatched input has lexical recovery, not a successful typed semantic parse. |
| Safety | `_slot` checks configured format/platform/time/name validity. `retrieval/search.py:compatible_text` filters conflicting candidate text. No external trusted timetable adapter in the input schema. This is not a train-fact verification service. |
| Gloss | `gloss.py:construct_gloss/recover_lexical` uses configurable draft domain order or lexical recovery. Function words can be skipped. Output explicitly says it is not linguistically validated. |
| Strict construction | `announcements.py:_compile` and `templates.py` compile reviewed recipes. Current database has one template but zero eligible motion versions, zero approved retrieval profiles and zero embeddings. |
| Development retrieval | `demo.py:prepare_demo/_select` scans usable staged/active versions, performs longest phrase, word forms, aliases, lexical fuzzy correction, digits and spelling. Limits: 64 clips and 600 seconds. Skips unavailable units and uses compatible rigs. No unrelated last-resort motions. |
| Sentence hierarchy | Strict templates/exact content are separate from development lexical lookup. All 149 current catalog records have level WORD, including some multiword labels. There is no current sentence-level motion corpus on which to measure sentence selection. |
| Approximate search | `retrieval/search.py:search/query_rows` has pg_trgm and optional E5/vector suggestions with critical-text filtering. This is a separate review candidate endpoint, not automatically the development preview's fallback. Current eligible/profile/embedding tables contain no usable search population. |
| Versioned GLBs | `storage.py:LocalAssetStore`, `playback.py:_persist`, `playback_api.py`. 149 unique registered GLB objects exist on disk; hashes/sizes/version IDs recorded. Existence alone does not establish linguistic quality. |
| Avatar | `AvatarViewer.jsx` creates one Three.js scene and renderer. `playback-controller.mjs:prepare/start/update/activate` loads and validates all clips before starting one persistent avatar and mixer. |
| Stitching | Ordered full-clip cuts with rest-pose restoration. No runtime merged GLB, no motion transition cost optimizer, no random-vs-optimized variant selector, no implemented crossfade comparison. |
| Live display | `live.py`, `display_control.py`, `DisplayConsole.jsx`, `services/live-display.mjs`: durable assignments, WebSockets, leases, acknowledgments and reconnect. Research trials use private preview and do not broadcast to station displays. |

No fake frontend animation or mocked network response is used by the evaluation harness. Availability, actual correctness and measurement evidence are reported separately in the final report.

# Predefined measurement rules

- Dataset: 224 author-labelled synthetic English cases (202 distinct texts), seven equal classes. Initial ten phrasing families per class have three value variants; fourteen additional distinct cases enforce the distinct-input requirement. Repeated general/emergency wording is counted transparently. No training split or broad generalization claim. Labels are defined before test outputs.
- Unsupported classification is **ABSTAIN**, not silently classified Other. Confusion matrix includes an additional ABSTAIN prediction column. Per-class false negatives include abstentions.
- Entity metrics compare normalized explicit field values with independently authored labels. Raw names are evaluated for extraction; configured-ID resolution is separate. Platform-change old/new values are additional fields, never silently treated as a single platform. Unsupported entities count as missed expected fields.
- Semantic coverage is an **engineering lexical/identifier retention proxy**, not ISL comprehension. It requires full spelling/digit representation of an annotated unit; partial spelling is not complete coverage. Order, temporal relationships and sign meaning still require separate review.
- Retrieval metrics on independent labelled concept probes assess catalog identity, not the visual correctness of the sign. Main preview does not expose ranked top-k alternatives; Recall@3/5 cannot be inferred by copying top-1 accuracy.
- Sixty trusted-statement conflicts plus ten controls test the existing candidate compatibility function in isolation. This is **not** evidence of an integrated timetable safety service. Integrated safety F1 remains unavailable.
- Browser length benchmark: 20 attempts each at 1/5/10/13/20/30 repeated TRAIN clips, real UI/API/GLBs, same warm persistent-avatar session. First trial is retained as cold-session evidence. Repeated assets control length but do not represent unique-asset download scaling. No playback-rate acceleration.
- Time to first sign: UI submission to first observed changed bone-pose hash while PLAYING and mixer time advances. Application samples bones every 250 ms; this measurement has sampling delay. Preparation latency: submission to first READY/PLAYING observation. Complete-animation duration is distinct.
- Repeated playback: 50 consecutive complete sequences, mixed one/two/eight-clip messages. No forced reload or GC between successful trials. Failure stops the run for diagnosis and is retained.
- Browser FPS comes from animation-frame intervals while PLAYING, not a hardware GPU profiler. Estimated missed 60-Hz frame opportunities are labelled as an estimate. Software/headless graphics results are not target-display certification.
- Ablations are explicitly evaluation-only candidate filtering and evaluation-only spelling-disabled selector experiments against existing lookup functions, not claims that the UI implements separate modes. Missing transition optimization is N/A.
- Synthetic scalability uses temporary PostgreSQL tables and synthetic duplication, with measured actual row counts. No insertion into the user's real library. No API-scale claim from SQL or Python-only timings.
- Missing features/ground truth are recorded as **Not measurable with current implementation**, never numeric zero. Human comprehension/naturalness: **Requires human participant evaluation.**
- Raw failures, unsuccessful attempts and interruptions are retained. Interrupted trials may be separately flagged, never silently removed. No research result is used to tune the runtime during this evaluation.
