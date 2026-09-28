"""Gloss retrieval preserves typed roles; English grammar cannot reach spelling."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.demo import Candidate, _select, _terms
from app.gloss import GlossUnit as G


def candidate(key, *terms, aliases=()):
    return Candidate(uuid4(), uuid4(), key, frozenset(terms), frozenset(aliases))


def test_gloss_phrase_priority_aliases_and_digit_occurrences():
    phrase = candidate("PHRASE", "express train")
    train = candidate("TRAIN", "train")
    arrive = candidate("ARRIVE", "arrive", aliases=("arrival",))
    zero, one = candidate("ZERO", "zero"), candidate("ONE", "1")
    trace = []
    motions, groups, boundaries, missing = _select(
        [train, phrase, arrive, zero, one],
        [G("EXPRESS"), G("TRAIN"), G("101", "IDENTIFIER", "entity"), G("ARRIVAL")],
        trace,
    )
    assert motions == [c.motion_id for c in (phrase, one, zero, one, arrive)]
    assert groups[1:4] == ["gloss-entity"] * 3
    assert boundaries == [0, 3, 4] and not missing
    assert [step["method"] for step in trace] == ["PHRASE", "DIGIT", "DIGIT", "DIGIT", "ALIAS"]


def test_missing_meaningful_gloss_uses_available_letters():
    train, a, t = [candidate(value, value) for value in ("train", "a", "t")]
    trace = []
    motions, _, _, missing = _select([train, a, t], [G("TRAIN"), G("FUTURE"), G("DEPART")], trace)
    assert motions[0] == train.motion_id and len(motions) == 4
    assert missing
    assert any(step["method"] == "ALPHABET" for step in trace)
    with pytest.raises(TypeError, match="typed gloss"):
        _select([a, t], "at")


def test_name_phrase_word_then_alphabet_without_inflecting_proper_names():
    known = candidate("KNOWN", "new delhi")
    a = candidate("A", "a")
    trace = []
    motions, _, _, missing = _select([known, a], [G("New Delhi Ava", "NAME", "destination")], trace)
    assert motions == [known.motion_id, a.motion_id, a.motion_id]
    assert [step["method"] for step in trace] == [
        "PHRASE",
        "ALPHABET",
        "MISSING_LETTER",
        "ALPHABET",
    ]
    assert len(missing) == 1


def test_partial_limit_and_no_unrelated_sample_fallback():
    train = candidate("TRAIN", "train")
    motions, _, _, missing = _select([train], [G("TRAIN")] * 80)
    assert len(motions) == 64 and any("64-clip" in item for item in missing)
    trace = []
    motions, _, _, _ = _select([train], [], trace)
    assert motions == [] and trace == []


def test_ids_and_phrase_fragments_never_become_digit_or_word_matches():
    concept = SimpleNamespace(
        canonical_text="express train", gloss="EXPRESS_TRAIN", semantic_key="ISL_EXPRESS_TRAIN_01"
    )
    assert _terms(concept) == {"express train"}
    concept.canonical_text, concept.gloss = "2 two", "2_TWO"
    assert _terms(concept) == {"2 two", "2", "two"}
