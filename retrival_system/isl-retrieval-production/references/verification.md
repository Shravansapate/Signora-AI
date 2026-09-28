# Verification and completion evidence

Select checks for the changed behavior and its dependencies. Run existing relevant tests first; add meaningful behavioral coverage where a concrete risk is otherwise untested. Do not build tests that merely match implementation wording or require every gate for an unrelated small edit. Label unrun checks explicitly.

| Changed area | Required relevant evidence |
|---|---|
| Database | Migrations apply; same-concept pointer and ACTIVE uniqueness hold; invalid/unapproved/wrong-language/rig/revoked candidates are excluded; concurrent activation cannot silently overwrite |
| Import | Rerun creates no unintended duplicates; interruption resumes; bad items isolate; metadata/checksum edits are classified; pending review/embeddings are honest |
| Lifecycle | Valid switch; failed upload/storage/transaction leaves old version active; audit/event commits with state; deactivate/reactivate; reviewed retained rollback; deletion checks actual references |
| Retrieval | Exact and aliases; implemented fuzzy/vector fallbacks; ambiguity rejection; complete meaning and exact slots; repeated occurrences/order; unsupported content; deterministic behavior when encoder fails |
| Manifest | Pinned versions and template/avatar identities; stale unpublished preview rebuilt; published archived valid version permitted after routine replacement; deactivation/revocation/expiry blocked; transport URL renewal retains identity |
| Player | One/three/ten-plus clips; repeated same clip; full operational readiness; one persistent avatar; no T-pose flash; binding and required channels; protected transitions; stale callback cancellation; bounded cache/cleanup |
| Realtime | Crash before/after send, socket reconnect/browser reload, duplicate/out-of-order delivery, stale source revision, correction during preload/playback, semantic-boundary interruption, expiry/lease and snapshot recovery |
| Interfaces/access | Operator publish and exact-version review; unauthorized mutations/subscriptions and station mismatch rejected; display role cannot publish/admin |
| Voice/text input | Same announcement yields equivalent meaning from typed text and finalized voice transcript; critical values preserved; partial/ambiguous ASR does not publish; ASR outage leaves deterministic typed paths available |
| Deployment | Target-device download/decode/render and queue behavior; an actual database-plus-assets restore; required-service readiness and observable display completion |

## Meaning cases

Exercise ARRIVE/DEPART, arriving/already-arrived, CANCELLED/not-cancelled, OPEN/CLOSED, source/destination swaps, old/new platform direction, leading zeros, repeated digits, supported suffixes, clock time/date rollover versus delay units, unresolved names, unsupported scripts/grammar, and fuzzy/semantic ties.

Use an independently labeled development set and held-out evaluation. Keep simple slot substitutions and near-duplicate paraphrases from inflating generalization claims. Measure accepted-plan correctness, critical errors, and coverage/review rate together; rejecting everything is not useful success. Candidate recall is different from correctness of the final published plan.

## Linguistic and composition checks

Use actual approval of the rendered exact-version content and its intended construction. Review handshape, orientation, location, movement/holds, required nonmanual cues, and difficult transitions. Include finger-heavy, two-handed, contact, and near-face assets relevant to the task. Do not infer sign correctness from a validator, embedding score, or passing unit tests.

For a pilot, evaluate concrete comprehension of train, platform, action, correction, and time/delay with Deaf/ISL users. Examine signing without captions as well as the integrated display. Report actual sample and scope; do not claim all ISL users are represented or invent reviewer feedback. Use multichannel metrics only where suitable annotations exist, and do not call them raw GLB-quality measures.

Any observed critical semantic error blocks the affected content until corrected/retested. Zero observed errors is evidence from that test set, not a universal guarantee. A supported production release needs both engineering and linguistic evidence.

## Report completion

State changed files, checks actually run/results, affected behavior, material remaining limits, and next milestone. Separate completed code from content review or deployment work that remains. If blocked, name the exact missing dependency/failed component and required next input, while retaining useful completed work. Never claim production readiness from code generation, a mock, or an unexecuted plan.

## Repository-boundary verification

- backend runtime code stays under `backend/`
- frontend runtime code stays under `frontend/`
- no frontend secrets or database credentials are shipped to the browser
- API/WebSocket schemas match on both sides
- GLB files are served through storage/asset URLs rather than bundled as arbitrary frontend source files
