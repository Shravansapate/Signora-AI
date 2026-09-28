"""Parser-independent retrieval and conservative spelling regression coverage."""

import pytest
from test_demo_mode import candidate

from app.announcement_schema import StationDefinition
from app.demo import _select, retrieval_trace
from app.gloss import construct_gloss, recover_lexical, translation_trace
from app.lexical import fuzzy_word, normalize_domain
from app.meaning import normalize, parse_meaning

STATION = StationDefinition(name="Nagpur", platforms=["1", "2"])


def pipeline(source):
    normalized, meaning, issues = parse_meaning(source, "NAGPUR", STATION, development=True)
    units = (
        construct_gloss(meaning, STATION.development_gloss)
        if meaning
        else recover_lexical(normalized)[0]
    )
    trace = translation_trace(source, normalized, meaning, issues, units, STATION.development_gloss)
    return units, trace, meaning


@pytest.mark.parametrize(
    "source",
    [
        "Train 1201 Arrives at platform 2",
        "Train 1201 Arrives at platfrm 2",
        "Train 1201 arrives at platfrom 2",
        "Train 1201 arrive at platform 2",
        "train 1201 arives at platform 2",
        "trian 1201 arrives at platform 2",
        "Train no 1201 arrives platform 2",
        "1201 arrives at platform 2",
    ],
)
def test_equivalent_arrival_forms_and_original_identifier_spans(source):
    units, trace, meaning = pipeline(source)
    assert [u.token for u in units] == ["TRAIN", "1201", "PLATFORM", "2", "ARRIVE"]
    assert trace["semantic_status"] == "COMPLETE"
    for slot in meaning.slots.values():
        assert source[slot.source_start : slot.source_end] == slot.raw


@pytest.mark.parametrize("word", ["platfrm", "platfrom", "pltform", "platrorm"])
def test_reusable_domain_spelling(word):
    normalized, _, changes = normalize_domain(*normalize(f"trrain 001201AB {word} 02 at 06:30 pm"))
    assert normalized == "train 001201ab platform 02 at 06:30 pm"
    assert changes[-1] == {
        "original": word,
        "normalized": "platform",
        "reason": "DOMAIN_FUZZY_MATCH",
    }
    assert fuzzy_word("cane", {"came", "care"}) is None
    assert fuzzy_word("00120", {"00121"}) is None


def test_correction_offsets_cover_the_complete_original_word():
    source = "arrival trrain 00120"
    value, offsets, _ = normalize_domain(*normalize(source))
    assert value == "arrive train 00120"
    for corrected, original in [("arrive", "arrival"), ("train", "trrain"), ("00120", "00120")]:
        start = value.index(corrected)
        assert source[offsets[start] : offsets[start + len(corrected) - 1] + 1] == original


@pytest.mark.parametrize(
    "source",
    ["Train ACCIDENT", "train accident", "TRAIN ACCIDENT", "Train Accident", "train aCcIdEnT"],
)
def test_parser_failure_does_not_block_known_words(source):
    units, translation, meaning = pipeline(source)
    assert meaning is None
    train, accident = candidate("TRAIN", "train"), candidate("ACCIDENT", "accident")
    trace = []
    motions, _, _, missing = _select([train, accident], units, trace)
    assert motions == [train.motion_id, accident.motion_id]
    result = retrieval_trace(translation, trace, missing, [], len(motions))
    assert result["translation_status"] == "LEXICAL_RECOVERY"
    assert result["semantic_status"] == "UNSUPPORTED"
    assert (
        result["retrieval_status"] == "COMPLETE" and result["semantic_coverage"] == "LEXICAL_ONLY"
    )
    assert not result["last_resort"]


@pytest.mark.parametrize("suffix", ["danger", "xyzabc", "unknownword", "emergency"])
def test_unknown_word_never_destroys_known_tokens(suffix):
    units, _, _ = pipeline(f"Train accident {suffix}")
    library = [
        candidate(w.upper(), w)
        for w in ["train", "accident", "danger", "emergency", *"abcdefghijklmnopqrstuvwxyz"]
    ]
    trace = []
    motions, _, _, missing = _select(library, units, trace)
    assert motions[:2] == [c.motion_id for c in library[:2]]
    assert len(motions) == (3 if suffix in {"danger", "emergency"} else 2 + len(suffix))
    assert not missing and all(t["method"] != "LAST_RESORT" for t in trace)


def test_partial_slots_keep_numbers_without_inventing_arrival():
    units, trace, meaning = pipeline("Train 1201 platform 2")
    assert meaning is None and trace["semantic_status"] == "PARTIAL"
    assert trace["partial_semantics"]["slots"] == {
        "train_identifier": ["1201"],
        "platform_identifier": ["2"],
    }
    assert [u.token for u in units] == ["TRAIN", "1201", "PLATFORM", "2"]


def test_grammar_is_skipped_but_relationships_survive_and_phrases_win():
    units, _, _ = pipeline(
        "the accident at a platform before danger not after emergency between from to"
    )
    trace = []
    _select(
        [
            candidate(w, w)
            for w in (
                "accident platform before danger not after emergency between from to a t"
            ).split()
        ],
        units,
        trace,
    )
    assert {t["input"] for t in trace if t["method"] == "SKIPPED_GRAMMAR"} == {"the", "at", "a"}
    assert {"before", "after", "between", "not", "from", "to"} <= {
        t["input"] for t in trace if t.get("matched")
    }
    phrase = candidate("PHRASE", "the accident")
    assert _select([phrase], units)[0] == [phrase.motion_id]


def test_fuzzy_catalog_and_aliases_before_spelling_and_truthful_empty():
    units, _, _ = pipeline("assistance accidnt")
    help_sign = candidate("HELP", "help", aliases=("assistance",))
    accident = candidate("ACCIDENT", "accident")
    assert _select([help_sign, accident], units)[0] == [help_sign.motion_id, accident.motion_id]
    unknown, _, _ = pipeline("xyzabc")
    assert _select([accident], unknown)[0] == []


@pytest.mark.parametrize(
    "source,event,time",
    [
        ("Train 1201 arriving at platform 2", "ARRIVE", "NOW RIGHT NOW"),
        ("Train number 1201 is arriving at platform number 2", "ARRIVE", "NOW RIGHT NOW"),
        ("Train 1201 departs from platform 2", "DEPART", None),
        ("Train 1201 departing from platfrm 2", "DEPART", "NOW RIGHT NOW"),
        ("Train 1201 is delayed by 20 minutes", "DELAY", None),
        ("Train 1201 has been canceled", "CANCEL", None),
    ],
)
def test_non_arrival_and_temporal_semantics(source, event, time):
    units, _, meaning = pipeline(source)
    tokens = [u.token for u in units]
    assert meaning is not None and event in tokens
    if time:
        assert time in tokens
