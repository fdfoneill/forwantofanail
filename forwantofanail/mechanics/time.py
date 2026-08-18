from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum


class Watch(IntEnum):
    NIGHT = 0
    MATIN = 1
    PRIME = 2
    NOON = 3
    VESPER = 4


WATCHES_PER_DAY = 5
WATCH_CHRONOLOGICAL_ORDER = {
    Watch.MATIN: 0,
    Watch.PRIME: 1,
    Watch.NOON: 2,
    Watch.VESPER: 3,
    Watch.NIGHT: 4,
}
WATCH_BY_CHRONOLOGICAL_ORDER = {rank: watch for watch, rank in WATCH_CHRONOLOGICAL_ORDER.items()}


def to_world_tick(day: int, watch: int | Watch) -> int:
    if int(day) < 1:
        raise ValueError("day must be >= 1")
    normalized = _normalize_watch(watch)
    return (int(day) - 1) * WATCHES_PER_DAY + WATCH_CHRONOLOGICAL_ORDER[normalized]


def from_world_tick(world_tick: int) -> tuple[int, Watch]:
    tick = int(world_tick)
    if tick < 0:
        raise ValueError("world_tick must be non-negative")
    day, rank = divmod(tick, WATCHES_PER_DAY)
    return day + 1, WATCH_BY_CHRONOLOGICAL_ORDER[rank]


def _normalize_watch(value: int | Watch) -> Watch:
    if isinstance(value, Watch):
        return value
    return Watch(int(value))


def advance_time(current_date: date, current_watch: int | Watch, steps: int = 1) -> tuple[date, Watch]:
    if steps < 0:
        raise ValueError("steps must be non-negative")

    watch = _normalize_watch(current_watch)
    day = current_date
    for _ in range(steps):
        next_index = (int(watch) + 1) % WATCHES_PER_DAY
        if watch == Watch.NIGHT and next_index == Watch.MATIN:
            day = day + timedelta(days=1)
        watch = Watch(next_index)
    return day, watch


@dataclass(frozen=True)
class GameTime:
    date: date
    watch: Watch

    def advance(self, steps: int = 1) -> "GameTime":
        next_date, next_watch = advance_time(self.date, self.watch, steps=steps)
        return GameTime(date=next_date, watch=next_watch)
