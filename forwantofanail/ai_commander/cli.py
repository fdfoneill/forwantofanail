from __future__ import annotations

import argparse
import json

from forwantofanail.ai_commander.runtime import (
    CommanderHeartbeatScheduler,
    CommanderWorker,
    RuntimeConfig,
    get_runtime_detail,
    list_runtime_rows,
    list_runs,
    mark_manual_attention,
    set_controller_type,
)
from forwantofanail.core.database import create_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forwantofanail.ai_commander.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scheduler = subparsers.add_parser("scheduler", help="Run the AI commander heartbeat scheduler loop.")
    scheduler.add_argument("--base-url", default="http://127.0.0.1:8000")
    scheduler.add_argument("--log-dir", default="logs")
    scheduler.add_argument("--poll-interval", type=float, default=1.0)
    scheduler.add_argument("--lease-seconds", type=int, default=30)
    scheduler.add_argument("--once", action="store_true")

    worker = subparsers.add_parser("worker", help="Execute a single queued commander run.")
    worker.add_argument("--commander-id", type=int, required=True)
    worker.add_argument("--run-id", type=int, required=True)
    worker.add_argument("--lease-token", required=True)
    worker.add_argument("--base-url", default="http://127.0.0.1:8000")
    worker.add_argument("--log-dir", default="logs")

    control = subparsers.add_parser("set-controller", help="Set commander controller type.")
    control.add_argument("--commander-id", type=int, required=True)
    control.add_argument("--controller-type", required=True)

    nudge = subparsers.add_parser("nudge", help="Mark a commander as needing AI attention.")
    nudge.add_argument("--commander-id", type=int, required=True)
    nudge.add_argument("--reason", default="manual_nudge")

    inspect = subparsers.add_parser("inspect", help="Inspect commander runtime state.")
    inspect.add_argument("--commander-id", type=int)
    inspect.add_argument("--run-limit", type=int, default=10)
    inspect.add_argument("--status", action="append", dest="statuses")
    inspect.add_argument("--limit", type=int, default=50)
    return parser


def command_scheduler(args: argparse.Namespace) -> int:
    config = RuntimeConfig(
        base_url=args.base_url,
        log_dir=args.log_dir,
        poll_interval_seconds=args.poll_interval,
        lease_duration_seconds=args.lease_seconds,
    )
    scheduler = CommanderHeartbeatScheduler(config=config)
    if args.once:
        print(json.dumps({"launched_run_ids": scheduler.run_once()}, indent=2))
        return 0
    scheduler.run_forever()
    return 0


def command_worker(args: argparse.Namespace) -> int:
    config = RuntimeConfig(base_url=args.base_url, log_dir=args.log_dir)
    worker = CommanderWorker(config=config)
    payload = worker.run(
        commander_id=args.commander_id,
        run_id=args.run_id,
        lease_token=args.lease_token,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_set_controller(args: argparse.Namespace) -> int:
    session = create_session()
    try:
        runtime = set_controller_type(session, args.commander_id, args.controller_type)
        session.commit()
        print(json.dumps({"runtime": {"commander_id": runtime.commander_id, "controller_type": runtime.controller_type}}, indent=2))
        return 0
    finally:
        session.close()


def command_nudge(args: argparse.Namespace) -> int:
    session = create_session()
    try:
        runtime = mark_manual_attention(session, args.commander_id, args.reason)
        session.commit()
        print(json.dumps({"runtime": {"commander_id": runtime.commander_id, "attention_needed": runtime.attention_needed}}, indent=2))
        return 0
    finally:
        session.close()


def command_inspect(args: argparse.Namespace) -> int:
    session = create_session()
    try:
        if args.commander_id is not None:
            payload = get_runtime_detail(session, args.commander_id, run_limit=args.run_limit)
        elif args.statuses:
            payload = {"runs": list_runs(session, statuses=args.statuses, limit=args.limit)}
        else:
            payload = {"runtimes": list_runtime_rows(session)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        session.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "scheduler":
        return command_scheduler(args)
    if args.command == "worker":
        return command_worker(args)
    if args.command == "set-controller":
        return command_set_controller(args)
    if args.command == "nudge":
        return command_nudge(args)
    if args.command == "inspect":
        return command_inspect(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
