# Signora experimental evaluation

This report evaluates the current implementation; it does not validate the linguistic correctness of ISL. No runtime retrieval, approval, avatar or display implementation was changed to improve results. The experiment uses the existing development preview, with unreviewed motions and partial output permitted.

## Environment and provenance

Baseline: `2026-09-27T19:35:30+0530`, Python 3.12.4, Microsoft Windows 11 Home Single Language; CPU 12th Gen Intel(R) Core(TM) i5-12450H, 7.65 GiB RAM. GPU devices: Intel(R) UHD Graphics, NVIDIA GeForce RTX 2050.

PostgreSQL: PostgreSQL 18.4 on x86_64-windows, compiled by msvc-19.44.35227, 64-bit; baseline database 23,172,799 bytes, schema `0009_display_routing`. Registered concepts/motions: 149/149; unique GLB objects: 149. Eligible production motions, embeddings and retrieval profiles: all zero. Full dependency/runtime configuration is in `raw/environment.json`; it contains no credentials.

Browser runs use headless Chromium with a 1280×900 viewport and the unsafe-SwiftShader fallback flag. Browser version and renderer identity are recorded in browser environment JSON files where captured. rAF measurements are not hardware GPU performance counters. Existing station services remain running, so these are local-system measurements, not isolated laboratory timing.

The working tree was committed during the session interruption. `raw/source_integrity_after_commit.json` distinguishes line-ending normalization from the one changed realtime-delivery source file. Core parser, gloss, selector, asset store and avatar controller hashes remained unchanged. Realtime changes are not attributed to this evaluation. Research scripts and result artifacts are the evaluation modifications.

## Dataset and metrics

224 cases; 202 distinct texts; 84 phrasing families; 32 cases per class. The set is author-labelled and synthetic. It includes 22 repeated notices/value variants, so results must not be presented as 224 independent real-world observations. Fourteen distinct cases were added to the initial 210-case set to exceed 200 distinct texts. No output was used as a class label.

Classification measures the typed `meaning.intent`; unsupported input is ABSTAIN, even if lexical recovery can play clips. Entity extraction includes typed slots and explicit singleton partial-recovery slots. Names are scored as extracted raw names, not successful resolution to configured station IDs. Old/new platform roles are scored separately. Exact match is over the union of expected and observed fields; true-negative absent fields are not used to inflate it.

Lexical coverage checks complete catalog-label or alphabet/digit representation of independently required units. It does not certify sign meaning, word order, timing, negation scope, visual legibility, or comprehension. Partial spelling is not full coverage. The retrieval probe score checks decoded catalog identities on 35 declared queries, including one function-word omission control. Ranked Recall@k is unavailable for this preview; there is no approved indexed corpus for the separate candidate-search endpoint.

## Measured summary

| Metric | Measured result |
|---|---:|
| Template Accuracy | 46.43% |
| Template Macro F1 | 55.42% |
| Template Weighted F1 | 55.42% |
| Entity Extraction F1 | 89.29% |
| Safety Validation F1 (integrated timetable) | Not measurable with current implementation |
| Safety False Negative Rate (integrated timetable) | Not measurable with current implementation |
| Retrieval Accuracy (35 engineering probes) | 100.00% |
| Recall@1 | Not measurable with current implementation |
| Recall@3 | Not measurable with current implementation |
| Recall@5 | Not measurable with current implementation |
| Translation Coverage (lexical retention proxy) | 92.59% |
| Fingerspelling Rate (represented units) | 28.87% |
| Mean End-to-End Preparation Latency (repeated-motion workload) | 336.44 ms |
| P95 Preparation Latency (repeated-motion workload) | 366.30 ms |
| Time-to-First-Sign (repeated-motion workload) | 695.38 ms |
| 13 Clip Processing Time (repeated TRAIN, mean preparation) | 267.51 ms |
| Transition Improvement | Not measurable with current implementation |
| Average Avatar FPS (rAF estimate) | 20.82 |

Detailed per-class precision/recall/F1 and entity metrics are in `tables/classification_metrics.csv` and `tables/entity_metrics.csv`. Case outputs, selected motion IDs, normalization, gloss and missing units are retained in raw API responses and `processed/gloss_traces.json`.

## Classification and entity failures

- Arrival: F1 89.66%, recall 81.25%, support 32; 6 missed/abstained cases.
- Departure: F1 89.66%, recall 81.25%, support 32; 6 missed/abstained cases.
- Delay: F1 69.39%, recall 53.12%, support 32; 15 missed/abstained cases.
- Cancellation: F1 81.48%, recall 68.75%, support 32; 10 missed/abstained cases.
- Platform change: F1 57.78%, recall 40.62%, support 32; 19 missed/abstained cases.
- Emergency: F1 0.00%, recall 0.00%, support 32; 32 missed/abstained cases.
- Other: F1 0.00%, recall 0.00%, support 32; 32 missed/abstained cases.

Emergency and Other have no typed intent in the parser contract. Lexical playback does not turn those cases into successful semantic classifications. Train names and platform-change roles expose extraction gaps. Inspect `processed/entity_failures.json` for exact examples; confidence in a small number of time examples must not be generalized to arbitrary timetables.

## Safety scope and false negatives

The existing candidate compatibility guard was tested on 60 conflicting pairs and 10 matching controls: TP=50, TN=10, FP=0, **FN=10**, precision=100.00%, recall=83.33%, F1=90.91%, false-negative rate=16.67%.

**This is not integrated safety validation.** `AnnouncementInput` has no trusted timetable payload, and development preview bypasses production review blocking. The guard operates in the separate candidate-search path. These constructed pairs do not prove that false operational announcements are prevented. Integrated safety F1 and false-negative rate are unavailable.

## Retrieval, fallback and gloss

Required annotated units represented: 1112/1201 (92.59%). Fingerspelled units: 392/1358 (28.87%). The label-equivalence audit flags 16 cases where an existing equivalent catalog label was available; these are saved for inspection and are not expert judgements.

Fallback evidence in `raw/fallbacks.json` removes a phrase or word from an evaluation-only candidate list and reruns the actual selector. Existing phrase, word, digits and alphabet motions are used. No library item is deleted. No complete sentence motion exists in this catalog, so sentence-available fallback and sentence-specific gains cannot be demonstrated.

The default gloss order is a draft engineering rule. Function words are intentionally omitted when not consumed by a phrase. The mandatory `Train 1201 arrives at platform 2` case is retained under the `mandatory` raw API result, with its actual motion IDs. Successful spelling of a concept is weaker evidence than selecting an appropriate lexical sign.

## Browser playback and latency

Repeated-motion series: 120 attempts, 120 movement-verified successes. Consecutive series: 50/50 completed, with persistent avatar identity checked. The series includes one-, two- and eight-clip messages. No forced GC/reload or time-scale acceleration was used.

A movement check requires changing rendered bone-pose hashes and advancing mixer time; API 200 alone is insufficient. The application samples pose hashes every 250 ms, adding observation delay to time-to-first-sign. Animation duration and preparation latency are different quantities. Length trials stop only after movement has been observed; the separate consecutive run waits for COMPLETE and checks all clips finished.

| Clips | Attempts / successes | Mean preparation (ms) | Mean first sign (ms) | P95 first sign (ms) |
|---:|---:|---:|---:|---:|
| 1 | 20 / 20 | 601.83 | 1001.87 | 1172.96 |
| 5 | 20 / 20 | 318.22 | 725.16 | 893.56 |
| 10 | 20 / 20 | 251.45 | 559.36 | 674.00 |
| 13 | 20 / 20 | 267.51 | 596.20 | 711.56 |
| 20 | 20 / 20 | 276.50 | 640.79 | 747.09 |
| 30 | 20 / 20 | 303.11 | 648.87 | 792.30 |

These timings reuse TRAIN and therefore mostly measure warm-cache preparation. Their weak dependence on sequence length is not evidence of sub-linear diverse-asset loading. The first cold-session trial is retained rather than silently discarded. Mean, median, min, max, sample standard deviation and P95 are in `tables/latency_summary.csv`.

### Distinct-motion sensitivity series

- 1 distinct motions: 20/21 successes; mean preparation 541.41 ms, first-use preparation 6232.20 ms, subsequent-trial mean 241.90 ms.
- 5 distinct motions: 20/20 successes; mean preparation 889.13 ms, first-use preparation 7382.80 ms, subsequent-trial mean 547.36 ms.
- 10 distinct motions: 20/20 successes; mean preparation 1416.89 ms, first-use preparation 10769.60 ms, subsequent-trial mean 924.64 ms.
- 13 distinct motions: 20/20 successes; mean preparation 1479.43 ms, first-use preparation 7744.10 ms, subsequent-trial mean 1149.71 ms.
- 20 distinct motions: 20/20 successes; mean preparation 2629.35 ms, first-use preparation 18972.80 ms, subsequent-trial mean 1769.17 ms.
- 30 distinct motions: 20/20 successes; mean preparation 3664.32 ms, first-use preparation 26681.50 ms, subsequent-trial mean 2452.89 ms.

The approximately four-second / 12–13-clip claim is workload- and cache-dependent, not a fixed property of the system. Compare the measured 13-clip repeated and distinct-motion groups above. No value was hardcoded to reproduce that observation.

Average rAF FPS: 20.82; minimum instantaneous FPS: 9.99; P95 frame interval: 66.60 ms. Estimated missed 60-Hz opportunities: 53,647. This estimate is not an actual GPU dropped-frame counter.

JavaScript heap and CDP performance metrics are recorded per trial; process RSS/CPU time are sampled in the distinct-motion run. Heap growth alone does not prove a leak because garbage collection and cached clips remain live. Texture counts and maximum texture size are captured from the actual avatar. GPU utilization is not directly measured.

## Transition measurements and ablations

- mean_joint_angle_degrees: 14.658348 (occurrence-weighted).
- mean_joint_displacement_model_units: 0.150145 (occurrence-weighted).
- mean_hand_displacement_model_units: 0.163795 (occurrence-weighted).
- mean_hand_velocity_jump_model_units_per_second: 0.048896 (occurrence-weighted).

Measured 467 distinct source-GLB transition pairs. Endpoint quaternion differences and model-space joint/hand displacements characterize cuts, not perceptual naturalness. The 40-ms velocity estimate uses spherical quaternion interpolation. No optimized selection exists, so percentage improvement cannot be calculated.

Ablations disable spelling and numeric units only inside an evaluation namespace and restrict multiword candidates for Word-only. Production code is unchanged. Phrase+Word and Sentence+Phrase+Word have the same available catalog because there are no sentence records; identical results are not evidence that sentence retrieval is ineffective. E (transition-aware) is unimplemented. Do not interpret the small, authored 35-query probe set as an ISL-quality benchmark.

## Scalability

| Entries | Synthetic | Mean catalog + selection (ms) | Trials |
|---:|---|---:|---:|
| 149 | False | 19.69 | 5 |
| 1000 | True | 57.15 | 5 |
| 5000 | True | 276.94 | 5 |
| 10000 | True | 597.79 | 5 |
| 25000 | True | 2236.82 | 5 |
| 50000 | True | 5606.26 | 5 |
| 100000 | True | 12732.80 | 5 |

The actual development catalog SELECT, ORM materialization, candidate construction and selector were timed. Larger sizes duplicate vocabulary and metadata identities in connection-private PostgreSQL temporary tables; GLB files are shared, not duplicated. Timing excludes synthetic setup, aliases, HTTP and asset verification/download. This is not an API or semantic-ANN scalability result. Temporary tables are rolled back after every size. The runtime role correctly denied temporary-table creation; the local maintenance account was used without changing permissions. An initial slow synthetic setup was cancelled and corrected to avoid repeatedly serializing unused large metadata; no runtime query was optimized.

## Graph interpretation

1. **Core system:** Separate task metrics; no composite accuracy. High lexical retention or small-probe identity accuracy can coexist with weak typed-intent classification. Integrated safety is N/A.
2. **Retrieval:** Top-k columns are N/A because the tested sequence API has no ranking. The identity and coverage columns measure different labelled datasets.
3. **Announcement types:** Compare the per-class F1 values above; unsupported Emergency/Other intents remain visible as failures rather than being hidden by lexical playback.
4. **Latency vs clips:** Repeated-asset caching dominates this curve; do not fit a scaling law from these six means. The distinct-motion sensitivity data tests a different loading workload.
5. **Pipeline breakdown:** Parser timing combines detection, entity extraction and validation. Inclusive normalization timings overlap it. These bars are not an additive decomposition; separate GLB/decode/stitch/render stages remain unavailable without invasive instrumentation.
6. **Ablations:** Numeric/alphabet fallback changes engineering coverage; sentence and transition-specific gains cannot be demonstrated with the present catalog/implementation.
7. **Retrieval levels:** Percentages are per output clip, so spelling a long word contributes many letters. Compare with unit-level fingerspelling rate before interpreting dependence.
8. **Transitions:** Only existing full-clip cuts have measured endpoint discontinuity. The missing optimized bar must not be reported as zero or a percentage gain.
9. **Scalability:** The full eligible-catalog scan/materialization grows with duplicated row count. These synthetic measurements do not establish performance on a diverse 100K-sign language library.
10. **Safety:** Counts and metrics belong only to the candidate compatibility guard. They cannot support a claim that the live preview detects inaccurate trusted railway facts.
11. **Confusion matrix:** ABSTAIN is an extra prediction column so parser rejection is visible. It is not relabelled as Other.

## Assessment and limitations

- **Working in the measured scope:** local text API, actual GLB loading, persistent-avatar movement, sequential playback, available-word/digit/alphabet fallback, 50 consecutive complete previews.
- **Implemented but constrained:** regex semantic parser, raw entity extraction, draft gloss ordering, staged-motion retrieval, review/publication controls, station-specific display assignments.
- **Missing or not measurable:** integrated trusted timetable safety, populated approved semantic retrieval corpus, sentence-level motion corpus, transition-aware variant optimization, top-k preview ranking, exact separated stage timing and hardware GPU usage.
- **Failed / needs improvement:** typed intent abstentions, unsupported Emergency/Other classes, train-name extraction, wrong/ambiguous lexical place roles, partial semantic coverage and some unnecessary spelling. Exact cases are retained; no runtime patch hides them.
- **Voice:** inspect the separate synthetic-audio smoke evidence if present; a synthetic voice test is not natural speech recognition accuracy or station-noise robustness.
- **ISL expert evaluation:** Requires human participant evaluation.
- **Deaf-user comprehension, naturalness and satisfaction:** Requires human participant evaluation.

## Reproduction

See `scripts/run-evaluation.ps1`. Prerequisites: existing local services, local credentials kept outside the repository, registered GLBs, Playwright Chromium, and evaluation-only Python dependencies under `artifacts/research-python`. Scripts checkpoint raw attempts. Use a fresh evidence directory for an independent replication; do not silently append retries to passing samples. App requests create ordinary private preview records; no library version is modified, activated or deleted, and no announcement is broadcast.

Do not publish raw local credentials. All thirteen PNG charts (eleven requested plus two sensitivity/profile charts) have adjacent JSON data, and the chart generator is `scripts/graphs.py`. Tables and raw responses are the authoritative evidence; screenshots are illustrative.

## Final verification and retained failures

Station display counts are dynamic, not fixed at five. The isolated seven-display suite passed 98 tests with two opt-in tests skipped; a separate three-display run passed its control integration test with the browser test skipped. These tests cover independent targeting, broadcast, stopping a selected display, replay/idempotency, station/role scope and assignment. They are backend integration evidence, not a claim of simultaneous physical-screen testing. Frontend unit tests: 67 passed.

The first backend test run from the backend directory loaded its local .env and failed the unconfigured-ASR expectation (93 passed, one failed). The root-directory isolated rerun passed. Both XML results are retained. No application change was made to conceal that environment issue.

The distinct-motion browser series contains 120 successful trials and one retained failed harness attempt: movement was observed, but playback completed before the harness clicked Stop. The evaluation harness was corrected to tolerate an already-completed sequence and the trial was retried. The failed record and screenshot remain available.

Safety challenge-set QA replaced cancellation examples confounded by changed numeric fields with same-train positive/negative cancellation pairs, and added ten destination-only adversarial pairs. Those ten exposed the guard false negatives. Initial data are retained separately. This is exploratory authored testing, not a held-out representative safety benchmark.

### Synthetic voice smoke

Direct ASR HTTP 201; measured ASR duration 3378.14 ms; transcript: 'Train number 1201 is arriving at platform 2.'. Voice translation HTTP 200. This is one synthetic English recording, not human/noise accuracy evidence.
Browser smoke: PASSED. mandatory: 8 clips completed, rendered bone movement and advancing mixer verified; typo: 8 clips completed, rendered bone movement and advancing mixer verified; lexical: 2 clips completed, rendered bone movement and advancing mixer verified; voice: 8 clips completed, rendered bone movement and advancing mixer verified.
Browser-captured transcript: 'is arriving at platform 2. Train number 1201 is'. Chromium's fake audio stream loops independently of the Record button; this recording captured a rotated/partial utterance. Its successful lexical playback verifies microphone upload, ASR, retrieval and animation transport, not correct reconstruction of the complete announcement. The separate whole-WAV ASR result above preserves the complete sentence.
