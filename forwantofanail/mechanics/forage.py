from __future__ import annotations


def forage_depletion_level(value: int | None) -> int:
    return max(0, min(3, int(value or 0)))


def forage_condition_word(average_depletion: float) -> str:
    if average_depletion <= 0:
        return "untouched"
    if average_depletion < 1:
        return "plentiful"
    if average_depletion < 2:
        return "picked-over"
    return "exhausted"
