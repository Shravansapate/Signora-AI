# GLB lifecycle and ingestion

## Version acceptance

Inspect existing storage, models, role checks, and review evidence. Protect all mutations with authentication and appropriate authorization. Accept the requested operation without adding redundant permission prompts when it is already authorized; retain the product's deliberate deletion policy.

Keep staging, validation, review, and activation distinct. Validate binary structure, selected animation, required tracks, rest/rig mapping, root behavior, and supported resources before activation. Approval of the exact rendered version is separate from structural QC and composition checks.

Generate safe storage keys. Enforce upload/resource limits and path traversal protection. Require self-contained GLBs for the initial asset contract or explicitly validated resource packaging; do not fetch arbitrary external URIs from an uploaded asset. Preserve source provenance and unknown/pending review information.

## Add and replace

For a new concept, resolve its semantic identity and metadata deliberately; do not infer a new concept solely from a different filename. For a replacement, preserve concept ID/gloss, aliases, templates, and embeddings unless a separately requested semantic edit justifies changing them.

Use this publication sequence:

1. Stage bytes, inspect/checksum them, and obtain required exact-version and composition approval.
2. Write a new immutable object and verify durable readability before the database activation commit.
3. Begin a short transaction, lock the concept, compare expected current revision, and recheck approval/compatibility/revocation.
4. Register/promote the candidate, archive the previous version logically, update the pointer, and write audit plus registry-change/outbox records in the same transaction.
5. Commit, invalidate logical caches, and verify the new catalog result and asset serving.

A precommit failure leaves the old active version usable. Database and object storage are not one shared transaction: retain/reconcile orphaned candidate objects after a failed commit instead of creating an active pointer to missing bytes. Do not hold the concept lock during upload or inference.

Keep archived bytes at their immutable keys for retained manifests. A quality replacement may require affected composition tests because boundaries/duration can change despite unchanged meaning.

## Retirement versus revocation

| Action | Required behavior |
|---|---|
| Routine replacement | New plans use the new version; already published valid snapshots may finish on retained old versions |
| Deactivate concept | Exclude it from new catalog retrieval at commit, preserve history, and invalidate affected queued plans through current-state/revocation delivery |
| Revoke defective version | Block the exact version in new and published-plan authorization; emit a durable invalidation event |
| Reactivate | Recheck content review, storage presence, compatible avatar, and dependencies before restoring eligibility |
| Rollback | Verify retained object/hash, approval, current avatar compatibility and nonrevocation; lock/compare state and switch pointer/status with audit atomically |
| Delete | Check actual authorization, retention, template/manifest references, and object sharing; preserve required audit metadata; schedule eligible physical cleanup after commit |

Do not imply that deactivation instantly erases cached bytes or already performed signs. Connected clients apply invalidation at approved semantic boundaries; disconnected clients follow freshness leases. Keep normal archival distinct from revocation.

Use stable IDs in new APIs where practical, while preserving compatible existing routes. Expose version upload, preview, approval/activation, deactivate/reactivate, rollback, version history, and eligible deletion; do not require direct SQL or manual file copying by operators.

## Initial bulk ingestion

Discover metadata-driven assets, validate identity and bytes, and store per-item state. Use semantic key + checksum + metadata hash to distinguish unchanged input, candidate replacement, and metadata edit. A rerun must not create duplicate concepts or versions. Different bytes do not grant automatic replacement permission.

Persist job/item stages and failures. Bound concurrency and retries, use independent sessions/transactions, continue valid items after a bad one, and resume incomplete work. Keep failed/pending/unverified items unavailable instead of autoapproving them. An embedding failure can leave semantic readiness pending without corrupting registration or other supported deterministic lookup.

Return discovered/unchanged/staged/eligible/pending/rejected/failed counts and actionable errors. Do not claim all 150+ inputs are production-eligible merely because registration finished.
