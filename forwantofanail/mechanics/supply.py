from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwantofanail.core.models import Army


INFANTRY_CAPACITY = 15
NONCOMBATANT_CAPACITY = 15
CAVALRY_CAPACITY = 75
WAGON_CAPACITY = 1000

INFANTRY_DAILY_CONSUMPTION = 1
NONCOMBATANT_DAILY_CONSUMPTION = 1
CAVALRY_DAILY_CONSUMPTION = 10
WAGON_DAILY_CONSUMPTION = 10


@dataclass(frozen=True)
class SupplyStats:
    capacity: int
    daily_consumption: int
    days_estimate: float | None


def _infantry_count(army: Army) -> int:
    return sum(det.warrior_count for det in army.detachments if not det.is_cavalry)


def _cavalry_count(army: Army) -> int:
    return sum(det.warrior_count for det in army.detachments if det.is_cavalry)


def _wagon_count(army: Army) -> int:
    return sum(det.wagon_count for det in army.detachments)


def supply_capacity(army: Army) -> int:
    infantry = _infantry_count(army)
    cavalry = _cavalry_count(army)
    wagons = _wagon_count(army)
    noncombatants = army.noncombattant_count
    return (
        infantry * INFANTRY_CAPACITY
        + noncombatants * NONCOMBATANT_CAPACITY
        + cavalry * CAVALRY_CAPACITY
        + wagons * WAGON_CAPACITY
    )


def daily_supply_consumption(army: Army) -> int:
    infantry = _infantry_count(army)
    cavalry = _cavalry_count(army)
    wagons = _wagon_count(army)
    noncombatants = army.noncombattant_count
    return (
        infantry * INFANTRY_DAILY_CONSUMPTION
        + noncombatants * NONCOMBATANT_DAILY_CONSUMPTION
        + cavalry * CAVALRY_DAILY_CONSUMPTION
        + wagons * WAGON_DAILY_CONSUMPTION
    )


def supply_stats(army: Army) -> SupplyStats:
    capacity = supply_capacity(army)
    daily_consumption = daily_supply_consumption(army)
    days_estimate = None
    if daily_consumption > 0:
        days_estimate = round(army.army_supply / daily_consumption, 2)
    return SupplyStats(
        capacity=capacity,
        daily_consumption=daily_consumption,
        days_estimate=days_estimate,
    )


def consume_supply_for_all_armies(session: Session) -> dict[str, int]:
    armies = session.query(Army).all()
    total_consumed = 0
    exhausted_armies = 0

    for army in armies:
        daily = daily_supply_consumption(army)
        if daily <= 0:
            continue
        consumed = min(army.army_supply, daily)
        army.army_supply = max(0, army.army_supply - daily)
        total_consumed += consumed
        if army.army_supply == 0:
            exhausted_armies += 1

    return {
        "armies_processed": len(armies),
        "total_consumed": total_consumed,
        "armies_at_zero_supply": exhausted_armies,
    }
