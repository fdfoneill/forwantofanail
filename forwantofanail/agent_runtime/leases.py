from dataclasses import dataclass
from datetime import timezone

from forwantofanail.core.locking import begin_write, lock_clock, lock_commanders
from forwantofanail.core.models import AgentAssignment, AgentRun


@dataclass(frozen=True)
class RunLease:
    run_id: int
    owner: str
    generation: int


class LeaseLost(Exception):
    """The caller no longer has authority to write this heartbeat."""


def lock_agent_scope(session, commander_id):
    lock_clock(session)
    lock_commanders(session, [commander_id])
    if session.bind.dialect.name == "postgresql":
        session.query(AgentAssignment.commander_id).filter_by(commander_id=commander_id).with_for_update().all()
        session.query(AgentRun.run_id).filter_by(commander_id=commander_id).order_by(AgentRun.run_id).with_for_update().all()


def owned_run(session, lease: RunLease):
    from .service import utcnow
    if not isinstance(lease, RunLease):
        raise LeaseLost()
    begin_write(session)
    row = session.get(AgentRun, lease.run_id)
    if row is None:
        raise LeaseLost()
    lock_agent_scope(session, row.commander_id)
    session.refresh(row)
    assignment = session.get(AgentAssignment, row.commander_id)
    if assignment is None:
        raise LeaseLost()
    session.refresh(assignment)
    clock = lock_clock(session)
    expires = row.lease_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if (row.status != "running" or row.lease_owner != lease.owner
            or row.lease_generation != lease.generation or expires is None or expires <= utcnow()
            or not assignment.enabled or clock is None or row.world_tick != clock.world_tick):
        raise LeaseLost()
    return row
