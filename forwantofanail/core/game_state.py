from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from forwantofanail.mechanics.movement import MoveResult, move_army
from forwantofanail.mechanics.time import GameTime, Watch


@dataclass
class GameState:
    session: Session
    game_time: GameTime

    @classmethod
    def starting_state(cls, session: Session, start_date: date | None = None, watch: Watch = Watch.MATIN):
        if start_date is None:
            start_date = date.today()
        return cls(session=session, game_time=GameTime(start_date, watch))

    def advance_time(self, steps: int = 1) -> GameTime:
        self.game_time = self.game_time.advance(steps=steps)
        return self.game_time

    def move_army(self, army_id: int, destination_id: str, allow_night: bool = False) -> MoveResult:
        result = move_army(self.session, army_id, destination_id, self.game_time, allow_night=allow_night)
        self.game_time = result.game_time
        return result
