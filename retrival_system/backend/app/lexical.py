"""Conservative spelling recovery shared by parsing and catalog lookup."""

import re

DOMAIN_WORDS = frozenset(
    "train platform arrive arrives arriving arrived arrival depart departs departing departed "
    "departure delay delayed cancel cancelled canceled number station express passenger "
    "minute minutes hour hours expected accident emergency danger".split()
)
CANONICAL = {"canceled": "cancelled", "arrival": "arrive", "departure": "depart"}
TOKEN_RE = re.compile(r"\d{1,2}:\d{2}(?:\s?[ap]m)?|[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def edit_distance(left, right):
    """Optimal string alignment distance, including adjacent transposition."""
    grid = [list(range(len(right) + 1))]
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            cost = a != b
            row.append(min(row[-1] + 1, grid[-1][j] + 1, grid[-1][j - 1] + cost))
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                row[j] = min(row[j], grid[i - 2][j - 2] + 1)
        grid.append(row)
    return grid[-1][-1]


def fuzzy_word(token, vocabulary):
    # Never change identifiers, short words, clock values, or exact catalog labels.
    if token in vocabulary or len(token) < 4 or not token.isalpha():
        return None
    matches = [
        word
        for word in vocabulary
        if word.isalpha()
        and len(word) >= 4
        and abs(len(word) - len(token)) <= 1
        and word[0] == token[0]
        and edit_distance(token, word) == 1
    ]
    return matches[0] if len(matches) == 1 else None


def normalize_domain(normalized, offsets):
    """Correct whole words and carry their original offsets through length changes."""
    parts, mapped, corrections, cursor = [], [], [], 0
    for match in TOKEN_RE.finditer(normalized):
        original = match.group()
        replacement = CANONICAL.get(original) or fuzzy_word(original, DOMAIN_WORDS)
        if not replacement:
            continue
        parts.extend((normalized[cursor : match.start()], replacement))
        mapped.extend(offsets[cursor : match.start()])
        source = offsets[match.start() : match.end()]
        mapped.extend(
            source[i * (len(source) - 1) // max(1, len(replacement) - 1)]
            for i in range(len(replacement))
        )
        corrections.append(
            {
                "original": original,
                "normalized": replacement,
                "reason": "CANONICAL_FORM" if original in CANONICAL else "DOMAIN_FUZZY_MATCH",
            }
        )
        cursor = match.end()
    parts.append(normalized[cursor:])
    mapped.extend(offsets[cursor:])
    return "".join(parts), mapped, corrections
