"""Development motion retrieval from typed semantic gloss, with partial output."""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import defer

from app.gloss import GlossUnit
from app.lexical import fuzzy_word
from app.models import AvatarProfile, LibrarySelection, MotionVersion, SignAlias, SignConcept
from app.playback import PlaybackUnavailable, _check_bytes, _check_content, _persist
from app.storage import StorageError

DIGIT_WORDS = dict(enumerate("zero one two three four five six seven eight nine".split()))
WORD_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def _normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)
    return " ".join(WORD_RE.findall(value))


@dataclass(frozen=True)
class Candidate:
    motion_id: UUID
    concept_id: UUID
    semantic_key: str
    terms: frozenset[str]
    aliases: frozenset[str] = field(default_factory=frozenset)
    duration: float = 1


def _terms(concept):
    # IDs contain version numbers: ISL_TRAIN_01 must never match digit 1.
    # A phrase's component words do not individually identify that phrase.
    terms = {_normalized(concept.canonical_text), _normalized(concept.gloss)}
    for term in tuple(terms):
        match = re.fullmatch(r"(\d+) ([a-z]+)", term)
        if match:
            terms.update(match.groups())  # Supplied labels such as `2_TWO`.
    return frozenset(filter(None, terms))


def _variants(token):
    values = [token]
    if token.endswith("ies") and len(token) > 3:
        values.append(token[:-3] + "y")
    elif token.endswith("ing") and len(token) > 4:
        values.extend((token[:-3] + "e", token[:-3]))
    elif token.endswith("ed") and len(token) > 3:
        values.extend((token[:-1], token[:-2]))
    elif token.endswith("s") and len(token) > 3:
        values.append(token[:-1])
        if token.endswith("es"):
            values.append(token[:-2])
    return list(dict.fromkeys(values))


def _pick(candidates, value, *, aliases=False):
    return next((c for c in candidates if value in (c.aliases if aliases else c.terms)), None)


def _select(candidates, units, trace=None):
    """Retrieve typed sign units. Raw English strings are not an accepted input."""
    if isinstance(units, str) or any(not isinstance(unit, GlossUnit) for unit in units):
        raise TypeError("Motion retrieval requires typed gloss units")
    trace = [] if trace is None else trace
    motions, groups, missing = [], [], []
    duration = 0

    def append(candidate, group, source, method, unit):
        nonlocal duration
        if len(motions) >= 64 or duration + candidate.duration > 600:
            missing.append("Sequence limit reached; remaining motions skipped")
            return False
        motions.append(candidate.motion_id)
        groups.append(group)
        duration += candidate.duration
        trace.append(
            {
                "input": source,
                "method": method,
                "matched": candidate.semantic_key,
                "motion_version_id": str(candidate.motion_id),
                "gloss_token": unit.token,
                "role": unit.role,
            }
        )
        return True

    def lookup(token, unit):
        item = _pick(candidates, token)
        if item:
            return item, "PHRASE" if " " in token else "WORD"
        if unit.kind in {"LEXEME", "LEXICAL"}:
            for variant in _variants(token)[1:]:
                if item := _pick(candidates, variant):
                    return item, "WORD_FORM"
        item = _pick(candidates, token, aliases=True)
        if item:
            return item, "ALIAS"
        if unit.kind == "LEXICAL":
            vocabulary = {term for c in candidates for term in c.terms | c.aliases}
            corrected = fuzzy_word(token, vocabulary)
            if corrected:
                return _pick(candidates, corrected) or _pick(
                    candidates, corrected, aliases=True
                ), "FUZZY_WORD"
        return None, "ALIAS"

    def unavailable(token, unit, method="MISSING_GLOSS"):
        missing.append(f"Unavailable {unit.kind.lower()} {token} ({unit.role})")
        trace.append(
            {"input": token, "method": method, "gloss_token": unit.token, "role": unit.role}
        )

    def spell(token, unit, group):
        for char in token:
            if char.isspace():
                continue
            numeric = char.isascii() and char.isdigit()
            item = _pick(candidates, char) or _pick(candidates, char, aliases=True)
            if numeric and not item:
                word = DIGIT_WORDS[int(char)]
                item = _pick(candidates, word) or _pick(candidates, word, aliases=True)
            if item:
                append(item, group, char, "DIGIT" if numeric else "ALPHABET", unit)
            else:
                unavailable(char, unit, "MISSING_DIGIT" if numeric else "MISSING_LETTER")

    index = 0
    while index < len(units) and len(motions) < 64:
        unit = units[index]
        token, group = _normalized(unit.token), f"gloss-{unit.role}"
        # Longest sign phrase, never merge across an identifier or name role.
        if unit.kind in {"LEXEME", "LEXICAL", "FUNCTION"}:
            end = index + 1
            while (
                end < len(units)
                and units[end].kind in {"LEXEME", "LEXICAL", "FUNCTION"}
                and units[end].role == unit.role
            ):
                end += 1
            matched_end = None
            for stop in range(end, index + 1, -1):
                if unit.kind == "FUNCTION" and stop == index + 1:
                    continue
                phrase = " ".join(_normalized(u.token) for u in units[index:stop])
                item, method = lookup(phrase, unit)
                if item:
                    append(item, group, phrase, "PHRASE" if stop - index > 1 else method, unit)
                    matched_end = stop
                    break
            if matched_end:
                index = matched_end
                continue
        if unit.kind == "FUNCTION":
            trace.append(
                {
                    "input": token,
                    "method": "SKIPPED_GRAMMAR",
                    "gloss_token": unit.token,
                    "role": unit.role,
                }
            )
            index += 1
            continue
        if unit.kind == "IDENTIFIER":
            spell(token, unit, group)  # Preserve every digit, including repeats and zeros.
        elif unit.kind == "NAME":
            words, offset = token.split(), 0
            while offset < len(words):
                for stop in range(len(words), offset, -1):
                    phrase = " ".join(words[offset:stop])
                    item, method = lookup(phrase, unit)
                    if item:
                        append(item, group, phrase, method, unit)
                        offset = stop
                        break
                else:
                    spell(words[offset], unit, group)
                    offset += 1
        else:
            item, method = lookup(token, unit)
            if item:
                append(item, group, token, method, unit)
            elif (unit.kind == "NUMBER" and token.replace(" ", "").isdigit()) or (
                unit.kind == "PLATFORM_IDENTIFIER" and token.replace(" ", "").isalnum()
            ):
                spell(token, unit, group)
            elif unit.kind in {"LEXEME", "LEXICAL"}:
                spell(token, unit, group)
            else:
                unavailable(unit.token, unit)
        index += 1
    if index < len(units):
        missing.append("Demo sequence reached the 64-clip playback limit")
    boundaries = [
        i for i in range(len(motions)) if i == len(motions) - 1 or groups[i] != groups[i + 1]
    ]
    return motions, groups, boundaries, missing


def prepare_demo(session, store, caption, owner, valid_until, *, units, translation):
    rows = session.execute(
        select(MotionVersion, SignConcept, AvatarProfile)
        .options(defer(MotionVersion.source_metadata), defer(MotionVersion.technical_report))
        .join(SignConcept, MotionVersion.concept_id == SignConcept.id)
        .join(AvatarProfile, MotionVersion.avatar_profile_id == AvatarProfile.id)
        .outerjoin(LibrarySelection, LibrarySelection.concept_id == SignConcept.id)
        .where(
            MotionVersion.technical_qc_status == "PASSED",
            MotionVersion.lifecycle_status.in_({"STAGING", "ACTIVE"}),
            or_(
                LibrarySelection.concept_id.is_(None),
                and_(
                    LibrarySelection.enabled.is_(True),
                    LibrarySelection.motion_version_id == MotionVersion.id,
                ),
            ),
            or_(
                LibrarySelection.concept_id.is_not(None),
                SignConcept.active_motion_version_id.is_(None),
                SignConcept.enabled.is_(True),
            ),
            or_(
                LibrarySelection.concept_id.is_not(None),
                SignConcept.active_motion_version_id.is_(None),
                SignConcept.active_motion_version_id == MotionVersion.id,
            ),
            MotionVersion.deleted_at.is_(None),
            MotionVersion.revoked_at.is_(None),
            SignConcept.language_code == "ISL",
            MotionVersion.duration_seconds > 0,
            MotionVersion.duration_seconds <= 600,
        )
        .order_by(SignConcept.semantic_key, MotionVersion.version_no, MotionVersion.id)
        .with_for_update(read=True, of=(MotionVersion, SignConcept, AvatarProfile))
    ).all()
    aliases = defaultdict(list)
    for cid, value in session.execute(select(SignAlias.concept_id, SignAlias.alias)):
        aliases[cid].append(_normalized(value))
    profiles = defaultdict(list)
    for row in rows:
        profiles[row[0].avatar_profile_id].append(row)
    unavailable = []
    trace, missing = [], []
    verified = set()
    for profile_id, available in sorted(profiles.items(), key=lambda p: (-len(p[1]), str(p[0]))):
        while available:
            versions = {row[0].id: row for row in available}
            avatar_id = next(
                (m.id for m, _, p in available if m.sha256 == p.source_sha256), available[0][0].id
            )
            candidates, seen = [], set()
            for motion, concept, _ in available:
                if concept.id in seen:
                    continue
                seen.add(concept.id)
                candidates.append(
                    Candidate(
                        motion.id,
                        concept.id,
                        concept.semantic_key,
                        _terms(concept),
                        frozenset(aliases[concept.id]),
                        motion.duration_seconds,
                    )
                )
            trace = []
            motions, groups, boundaries, missing = _select(candidates, units, trace)
            if not motions:
                break
            selected = {mid: versions[mid] for mid in {avatar_id, *motions}}
            failed = set()
            for mid, row in selected.items():
                if mid in verified:
                    continue
                try:
                    _check_content({mid: row}, profile_id, development=True)
                    _check_bytes({mid: row}, store)
                    verified.add(mid)
                except (PlaybackUnavailable, StorageError, OSError):
                    failed.add(mid)
                    unavailable.append(str(mid))
            if failed:
                available = [row for row in available if row[0].id not in failed]
                continue  # Retry smaller matches, another version, or another compatible rig.
            result = _persist(
                session,
                selected,
                motions,
                avatar_id,
                owner,
                "CONTENT_REVIEW",
                caption,
                groups=groups,
                boundaries=boundaries,
                valid_until=valid_until,
            )
            return result.manifest, retrieval_trace(
                translation, trace, missing, unavailable, len(motions)
            )
    return None, retrieval_trace(translation, trace, missing, unavailable, 0)


def retrieval_trace(translation, trace, missing, unavailable, count):
    return {
        **translation,
        "matches": trace,
        "fingerspelled": list(
            dict.fromkeys(step["gloss_token"] for step in trace if step["method"] == "ALPHABET")
        ),
        "missing_gloss": list(
            dict.fromkeys(
                step["gloss_token"] for step in trace if step["method"].startswith("MISSING_")
            )
        ),
        "semantic_coverage": "LEXICAL_ONLY"
        if translation.get("translation_status") == "LEXICAL_RECOVERY"
        else "PARTIAL"
        if missing
        else "COMPLETE_DRAFT",
        "skipped": missing,
        "unavailable_motion_ids": unavailable,
        "last_resort": False,
        "retrieval_status": "EMPTY"
        if not count
        else "PARTIAL"
        if missing
        else "FINGERSPELLED"
        if any(step["method"] == "ALPHABET" for step in trace)
        else "COMPLETE",
        "clip_count": count,
    }
