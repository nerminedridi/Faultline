"""Command line for the scoreboard.

    python -m scoreboard run                      every dev fault once, with the default model
    python -m scoreboard run --split holdout      the held-out faults (final evaluation only)
    python -m scoreboard run --faults db-lock --repeats 3 --model gemini-3.5-flash-lite
    python -m scoreboard report                   table of every saved run

Each run takes ~5 minutes per trial (symptoms build up, investigation, recovery).
Results are saved after every trial, so an interrupted run keeps what it finished.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from agent.__main__ import load_env
from chaos.faults import FAULTS

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _faults(args: argparse.Namespace) -> list[str]:
    if args.faults:
        unknown = [f for f in args.faults if f not in FAULTS]
        if unknown:
            sys.exit(f"unknown faults: {unknown}")
        return args.faults
    return [f.id for f in FAULTS.values() if args.split == "all" or f.holdout == (args.split == "holdout")]


def cmd_run(args: argparse.Namespace) -> int:
    from agent.llm import LLMError, from_env
    from scoreboard.runner import run_trial, wait_until_quiet

    llm = from_env(args.provider, args.model)
    provider = type(llm).__name__.lower()
    faults = _faults(args)
    started = datetime.now(timezone.utc)
    path = RESULTS_DIR / f"{started:%Y%m%dT%H%M%SZ}-{llm.model}.json"
    result = {
        "started_at": started.isoformat(timespec="seconds"),
        "provider": provider,
        "model": llm.model,
        "split": "custom" if args.faults else args.split,
        "settings": {"warmup_s": args.warmup, "cooldown_s": args.cooldown, "max_steps": args.max_steps},
        "trials": [],
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    trials = [f for f in faults for _ in range(args.repeats)]
    print(f"Scoreboard: {len(trials)} trial(s) with {provider} / {llm.model} -> {path.name}")

    for i, fault in enumerate(trials, 1):
        print(f"\n[{i}/{len(trials)}] {fault}")
        try:
            wait_until_quiet(min_wait=args.cooldown if i > 1 else 0)
            trial = run_trial(fault, llm, warmup=args.warmup, max_steps=args.max_steps)
        except LLMError as exc:
            print(f"  stopping: {exc}")
            break
        except KeyboardInterrupt:
            print("  interrupted")
            break
        d = trial["diagnosis"]
        verdict = "CORRECT" if trial["correct"] else ("service ok, kind wrong" if trial["service_ok"] else "WRONG")
        answer = f"{d['service']} / {d['kind']} ({d['confidence']})" if d else "no diagnosis"
        print(f"  -> {answer}: {verdict}")
        result["trials"].append(trial)
        path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")

    print()
    print_table([result])
    return 0


def print_table(results: list[dict]) -> None:
    """Per model: per fault, how often the service and the full root cause were right."""
    by_model = defaultdict(lambda: defaultdict(list))
    for result in results:
        for trial in result["trials"]:
            by_model[result["model"]][trial["fault"]].append(trial)

    for model, faults in by_model.items():
        print(f"## {model}\n")
        print("| Fault | Split | Trials | Service right | Fully right | Avg model calls |")
        print("|---|---|---|---|---|---|")
        totals = defaultdict(lambda: [0, 0, 0])
        for fault_id in [f for f in FAULTS if f in faults]:
            trials = faults[fault_id]
            n = len(trials)
            svc = sum(t["service_ok"] for t in trials)
            full = sum(t["correct"] for t in trials)
            calls = sum(t["usage"]["llm_calls"] for t in trials) / n
            split = "holdout" if FAULTS[fault_id].holdout else "dev"
            for key in (split, "all"):
                totals[key][0] += n
                totals[key][1] += svc
                totals[key][2] += full
            print(f"| `{fault_id}` | {split} | {n} | {svc}/{n} | {full}/{n} | {calls:.1f} |")
        for key in ("dev", "holdout", "all"):
            if key in totals:
                n, svc, full = totals[key]
                print(f"| **{key}** | | {n} | **{svc / n:.0%}** | **{full / n:.0%}** | |")
        print()


def cmd_report(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files] or sorted(RESULTS_DIR.glob("*.json"))
    if not paths:
        print("No results yet. Run `python -m scoreboard run` first.")
        return 1
    print_table([json.loads(p.read_text(encoding="utf-8")) for p in paths])
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m scoreboard", description="Faultline scoreboard")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent against faults and grade it")
    run.add_argument("--split", choices=["dev", "holdout", "all"], default="dev",
                     help="which faults (default dev; holdout is for the final evaluation)")
    run.add_argument("--faults", nargs="+", help="specific fault ids instead of a split")
    run.add_argument("--repeats", type=int, default=1, help="trials per fault (default 1)")
    run.add_argument("--warmup", type=float, default=90, help="seconds between injection and investigation")
    run.add_argument("--cooldown", type=float, default=150, help="minimum seconds between trials")
    run.add_argument("--max-steps", type=int, default=15, help="agent tool-call budget")
    run.add_argument("--provider", choices=["gemini", "ollama"])
    run.add_argument("--model")
    run.set_defaults(fn=cmd_run)

    report = sub.add_parser("report", help="summarise saved results")
    report.add_argument("files", nargs="*", help="result files (default: all)")
    report.set_defaults(fn=cmd_report)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
