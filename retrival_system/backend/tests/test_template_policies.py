"""Policy mechanics use synthetic concept IDs, not fabricated linguistic approvals."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.announcement_schema import StationDefinition, StructuredMeaning, TemplateDefinition
from app.lifecycle import LifecycleError
from app.meaning import parse_meaning
from app.templates import expand_recipe


def definition():
    return {
        "intent": "TRAIN_ARRIVAL",
        "temporal_state": "ARRIVING_NOW",
        "polarity": "POSITIVE",
        "avatar_profile_id": str(uuid4()),
        "slot_types": {
            "train_identifier": "TRAIN_IDENTIFIER",
            "platform_identifier": "PLATFORM_IDENTIFIER",
        },
        "recipe": [
            {
                "kind": "CONCEPT",
                "concept_id": str(uuid4()),
                "covers": ["intent", "temporal_state", "polarity"],
                "group": "event",
            },
            {
                "kind": "SLOT",
                "slot": "train_identifier",
                "group": "identifier",
                "policy": {
                    "mode": "IDENTIFIER_CHARACTERS",
                    "units": {n: [str(uuid4())] for n in "0123456789A"},
                },
            },
            {
                "kind": "SLOT",
                "slot": "platform_identifier",
                "group": "platform",
                "policy": {"mode": "EXACT_VALUES", "units": {"3": [str(uuid4())]}},
            },
        ],
        "safe_after_groups": ["event", "identifier", "platform"],
    }


def meaning(**slots):
    _, result, issues = parse_meaning(
        "",
        "TEST",
        StationDefinition(
            name="Isolated",
            platforms=["3"],
            train_suffixes=["A", "B"],
            places=[{"id": "D", "name": "Delhi"}, {"id": "M", "name": "Mumbai"}],
        ),
        structured=StructuredMeaning(
            intent="TRAIN_ARRIVAL",
            temporal_state="ARRIVING_NOW",
            slots={"train_identifier": "00110A", "platform_identifier": "3", **slots},
        ),
    )
    assert not issues
    return result


def test_identifiers_keep_leading_zeros_repetition_suffix_and_atomic_boundaries():
    recipe = TemplateDefinition.model_validate(definition())
    ids, groups, boundaries = expand_recipe(recipe, meaning())
    units = recipe.recipe[1].policy.units
    assert ids[1:-1] == [units[n][0] for n in "00110A"]
    assert groups[1:-1] == ["identifier"] * 6
    assert boundaries == [0, 6, 7]
    with pytest.raises(LifecycleError, match="Incomplete units"):
        expand_recipe(recipe, meaning(train_identifier="00110B"))


@pytest.mark.parametrize(
    "damage",
    ["missing", "duplicate", "baked", "noncontiguous", "missing_digit", "duration_as_identifier"],
)
def test_incomplete_or_mistyped_constructions_are_rejected(damage):
    recipe = definition()
    if damage == "missing":
        recipe["recipe"][0]["covers"].remove("polarity")
    elif damage == "duplicate":
        recipe["recipe"][0]["covers"].append("intent")
    elif damage == "baked":
        recipe["recipe"][0]["covers"].append("train_identifier")
        recipe["recipe"].pop(1)
    elif damage == "noncontiguous":
        recipe["recipe"][-1]["group"] = "event"
    elif damage == "missing_digit":
        del recipe["recipe"][1]["policy"]["units"]["0"]
    else:
        recipe["slot_types"]["train_identifier"] = "DURATION"
    with pytest.raises(ValidationError):
        TemplateDefinition.model_validate(recipe)


def test_baked_literals_are_bound_and_cannot_be_replaced_by_a_slot():
    recipe = definition()
    recipe["fixed_slots"] = {"platform_identifier": "3"}
    with pytest.raises(ValidationError, match="Fixed values"):
        TemplateDefinition.model_validate(recipe)
    recipe["recipe"][-1] = {
        "kind": "CONCEPT",
        "concept_id": str(uuid4()),
        "covers": ["platform_identifier"],
        "group": "platform",
    }
    assert TemplateDefinition.model_validate(recipe).fixed_slots == {"platform_identifier": "3"}


def test_names_require_reviewed_entity_spellings_and_never_guess_transliteration():
    recipe = definition()
    alphabet = {n: [str(uuid4())] for n in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
    for slot, entity, spelling in [("source", "D", "DELHI"), ("destination", "M", "MUMBAI")]:
        recipe["slot_types"][slot] = "NAME"
        recipe["recipe"].append(
            {
                "kind": "SLOT",
                "slot": slot,
                "group": slot,
                "policy": {
                    "mode": "ISL_FINGERSPELL",
                    "units": alphabet,
                    "spellings": {entity: spelling},
                },
            }
        )
        recipe["safe_after_groups"].append(slot)
    construction = TemplateDefinition.model_validate(recipe)
    ids, groups, boundaries = expand_recipe(
        construction, meaning(source="Delhi", destination="Mumbai")
    )
    assert [str(cid) for cid in ids[-11:]] == [alphabet[n][0] for n in "DELHIMUMBAI"]
    assert groups[-11:] == ["source"] * 5 + ["destination"] * 6
    assert boundaries[-2:] == [12, 18]
    missing = deepcopy(recipe)
    missing["recipe"][-1]["policy"]["spellings"] = {"OTHER": "OTHER"}
    with pytest.raises(LifecycleError, match="no reviewed spelling"):
        expand_recipe(
            TemplateDefinition.model_validate(missing),
            meaning(source="Delhi", destination="Mumbai"),
        )


def test_oversized_complete_realization_is_withheld_instead_of_truncated():
    recipe = definition()
    recipe["recipe"][1]["policy"]["units"]["0"] = [str(uuid4()) for _ in range(16)]
    with pytest.raises(LifecycleError, match="exceeds 64"):
        expand_recipe(TemplateDefinition.model_validate(recipe), meaning(train_identifier="00000"))
