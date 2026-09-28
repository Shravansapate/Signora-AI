# Retrieval and manifest contract

Read frontend-playback.md for the player and realtime-delivery.md for publication/recovery. This reference owns semantic selection and immutable playback plans.

## Protect meaning before matching

Normalize conservatively and extract typed intent/slots, polarity, temporal meaning, and entity roles before searching. Never select a nearest sentence GLB and substitute its literal train/platform/time values through metadata. Exact text, alias, fuzzy, and vector paths all require context and production eligibility.

Preserve the hierarchy: approved template; exact sentence/phrase/concept; reviewed alias; pg_trgm fuzzy candidate; pgvector semantic candidate; approved phrase decomposition; approved word decomposition; typed number/name realization; permitted verified ISL fingerspelling.

A recognized template can resolve known concept IDs directly. Component resolution after decomposition may reuse earlier lookup stages. The late number/name stage is realization fallback, not delayed slot protection.

For unattended operational publication, require approved templates or exact approved sentences with validated critical values. Unresolved critical meaning from fuzzy/semantic retrieval remains a review suggestion. Protect ARRIVE/DEPART, arriving/arrived, cancelled/not-cancelled/running, open/closed, entity roles, old/new platforms, identifiers, times, and units. Do not assume a negated cancellation establishes all aspects of normal service.

## Fuzzy and vector search

Use reviewed aliases scoped by source text language, domain, and sense/context. Treat pg_trgm as candidate spelling tolerance; its score/default threshold is not a confidence percentage.

At the initial library scale, prefer exact pgvector cosine scans over eligible candidates. Evaluate filtered recall and latency before HNSW or extra infrastructure. Select thresholds and ambiguity handling on development data and verify on held-out hard negatives; do not blindly combine heterogeneous raw scores or accept top-1 unconditionally.

If choosing a new encoder, multilingual-e5-small with 384-dimensional vectors is an initial evaluation baseline, not a mandatory replacement for a working model or an ISL translator. Follow its query/passage-prefix and normalization contract; pin model revision and input-text hash. Never mix incompatible revisions because dimensions happen to match.

Load/warm the encoder once, run bounded heavy inference outside the realtime event loop, and track readiness/timeouts separately. Deterministic paths continue if the encoder fails. Keep semantic publication disabled if evaluation shows no safe benefit. Check the actual model card when changing encoding behavior.

## Composition and names/numbers

Compose over a reviewed ISL construction rather than English whitespace order. Prefer approved complete phrases when they preserve meaning and boundaries. A complete constrained covering may use dynamic programming to reduce boundaries/fallbacks, but coverage and semantic validity are hard requirements.

Keep identifiers as exact strings. Use separately reviewed realization policies for train identifiers, quantities, platforms, clock times, and durations. Validate suffixes and station-specific formats. Preserve repeated digits.

Use reviewed lexical signs for names when available; otherwise apply a permitted ISL fingerspelling convention. Review transliteration before applying Latin alphabet assets to other scripts. Fingerspelling does not authorize unknown critical verbs/instructions or unsupported grammar. Keep each identifier/name as an atomic semantic group.

Batch unique concept/version lookup, then restore exact order and repeated occurrences. Missing required meaning returns a structured reason; do not skip a failed item and mark the partial sentence READY.

## Manifest contract

Persist a versioned immutable plan including:

- Schema, manifest ID/revision/hash, message ID/revision, source revision and station/display scope.
- Approved template version and canonical avatar profile/version.
- Intent, exact typed slots, priority, issued/valid-until times, supersession, and authorized caption text.
- Ordered items with concept ID, exact motion-version ID, immutable key/checksum, selected animation clip, actual duration, approved trim/rate, and transition-policy identity.
- Atomic semantic groups and approved interruption/recovery boundaries; complete-message readiness for operational messages.

Pin versions consistently in a database transaction/snapshot. Revalidate mutable eligibility/freshness before publication. Rebuild an unpublished preview whose catalog selections changed. Already published valid snapshots may use retained archived versions after routine replacement; check concept/template enabled state, exact-version approval, compatibility, freshness, and nonrevocation. Never silently upgrade a pinned clip mid-message.

Separate renewable transport URLs from semantic plan identity. Renew an expired signed URL only for the same key/hash/version; omit auth strings from semantic hashes. Ensure all required items are reachable and that the client validates decoded readiness separately from backend READY.

Do not send GLB binaries over the manifest API, merge GLBs at request time, infer a universal blend duration, or label an illustrative gloss sequence linguistically verified. Estimate duration from actual clips, approved rates/holds, and real permitted overlap.
