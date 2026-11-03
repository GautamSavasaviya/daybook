"""Autocomplete matching for free-text fields ("Requested by", tags)."""

from collections.abc import Iterable

MAX_SUGGESTIONS = 6


def matching(values: Iterable[str], typed: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Values with a word starting with what was typed, in their given (most-used) order.

    Case-insensitive. An exact match is left out: there is nothing left to complete.
    """
    needle = typed.strip().casefold()
    if not needle:
        return []
    result: list[str] = []
    for value in values:
        folded = value.casefold()
        if folded == needle:
            continue
        if folded.startswith(needle) or any(word.startswith(needle) for word in folded.split()[1:]):
            result.append(value)
            if len(result) == limit:
                break
    return result
