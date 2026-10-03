"""Command line for the chaos engine.

    python -m chaos list                       show the fault catalog
    python -m chaos inject <fault> [-d SECS]   inject, wait, then revert
    python -m chaos inject <fault> --detach    inject and return; revert with `clear`
    python -m chaos status                     what is broken right now
    python -m chaos clear                      revert everything
    python -m chaos runs                       past runs and their ground truth
"""

import argparse
import sys
import time

from chaos import lab, runs
from chaos.faults import FAULTS


def cmd_list(_: argparse.Namespace) -> int:
    width = max(map(len, FAULTS))
    for fault in FAULTS.values():
        cause = fault.root_cause
        print(f"{fault.id:<{width}}  {fault.summary}")
        print(f"{'':<{width}}  root cause: {cause.service} / {cause.kind}")
    return 0


def _wait(seconds: float) -> str:
    """Sleep, printing a countdown; returns how the wait ended."""
    deadline = time.monotonic() + seconds
    try:
        while (left := deadline - time.monotonic()) > 0:
            print(f"\r  fault active, {left:4.0f} s left (Ctrl-C to end early) ", end="", flush=True)
            time.sleep(min(1.0, left))
    except KeyboardInterrupt:
        print()
        return "interrupted"
    print()
    return "duration"


def cmd_inject(args: argparse.Namespace) -> int:
    fault = FAULTS[args.fault]
    if active := runs.open_runs():
        print(f"A fault is already active ({active[0]['run_id']}). Run `python -m chaos clear` first.")
        return 1

    run = runs.start(fault, args.duration)
    try:
        fault.inject(args.duration)
    except lab.LabError as exc:
        print(f"Injection failed: {exc}")
        lab.clear_all()
        runs.finish(run, "failed")
        return 1
    print(f"Injected {fault.id} (run {run['run_id']})")

    if args.detach:
        print("Detached. Revert with `python -m chaos clear`.")
        return 0

    ended_by = _wait(args.duration)
    for action in lab.clear_all():
        print(f"  {action}")
    runs.finish(run, ended_by)
    print(f"Reverted. Ground truth: {runs.RUNS_DIR.name}/{run['run_id']}.json")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    for run in runs.open_runs():
        print(f"open run: {run['run_id']} (injected {run['injected_at']})")
    states = lab.container_states()
    unhealthy = {s: state for s, state in states.items() if state != "running"}
    for service, state in unhealthy.items():
        print(f"container {service}: {state}")
    in_app = lab.in_app_faults(states)
    for service, status in in_app.items():
        print(f"in-app fault on {service}: {status['remaining_s']} s left")
    if locks := lab.lock_sessions(states):
        print(f"lock-holding database sessions: {locks}")
    if not (unhealthy or in_app or locks or runs.open_runs()):
        print("No faults active.")
    return 0


def cmd_clear(_: argparse.Namespace) -> int:
    actions = lab.clear_all()
    for action in actions:
        print(action)
    for run in runs.open_runs():
        runs.finish(run, "clear")
        print(f"closed run {run['run_id']}")
    if not actions:
        print("Nothing to revert.")
    return 0


def cmd_runs(_: argparse.Namespace) -> int:
    for run in runs.all_runs():
        cause = run["root_cause"]
        ended = run["ended_at"] or "still active"
        print(f"{run['run_id']}  {cause['service']}/{cause['kind']}  {run['injected_at']} -> {ended}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m chaos", description="Faultline chaos engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show the fault catalog").set_defaults(fn=cmd_list)
    inject = sub.add_parser("inject", help="inject a fault")
    inject.add_argument("fault", choices=sorted(FAULTS))
    inject.add_argument("-d", "--duration", type=float, default=180, help="seconds (default 180)")
    inject.add_argument("--detach", action="store_true", help="return immediately; revert with `clear`")
    inject.set_defaults(fn=cmd_inject)
    sub.add_parser("status", help="what is broken right now").set_defaults(fn=cmd_status)
    sub.add_parser("clear", help="revert every fault").set_defaults(fn=cmd_clear)
    sub.add_parser("runs", help="list past runs").set_defaults(fn=cmd_runs)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except lab.LabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
