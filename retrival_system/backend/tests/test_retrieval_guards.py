"""Held-out constraint cases: scores cannot reverse critical meaning."""

import pytest

from app.retrieval.search import compatible_text


@pytest.mark.parametrize(
    "query,candidate",
    [
        ("Train 00881 is arriving on platform 7", "Train 00881 is departing on platform 7"),
        ("Train 00881 has arrived on platform 7", "Train 00881 is arriving on platform 7"),
        ("Train 00881 is not cancelled", "Train 00881 is cancelled"),
        ("Door open", "Door closed"),
        (
            "Train 00881 from Delhi to Mumbai is arriving",
            "Train 00881 from Mumbai to Delhi is arriving",
        ),
        (
            "Platform for train 00881 has changed from 7 to 8",
            "Platform for train 00881 has changed from 8 to 7",
        ),
        ("Train 00881", "Train 881"),
        ("Train 00881A", "Train 00881B"),
        ("Delayed 18 minutes", "Delayed 18 hours"),
        ("Departure 6:30 am", "Departure 6:30 pm"),
        ("Train is not cancelled", "Train is running normally"),
        ("Platform 7 for train 881", "Train 7 on platform 881"),
    ],
)
def test_critical_conflicts_are_rejected(query, candidate):
    assert not compatible_text(query, candidate)


@pytest.mark.parametrize(
    "query,candidate",
    [
        ("Train no. 00881 is arriving", "Train number 00881 is arriving"),
        ("TRAIN ００８８１ is arriving", "Train 00881 is arriving"),
        ("train", "train"),
    ],
)
def test_equivalent_protected_values_remain_available(query, candidate):
    assert compatible_text(query, candidate)
