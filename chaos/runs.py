"""Ground-truth records: one JSON file per injected fault, for the scoreboard to grade against."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from chaos.faults import Fault

RUNS_DIR = Path(__file__).resolve().parent / "runs"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def start(fault: Fault, duration: float) -> dict:
    started = now()
    run = {
        "run_id": f"{started:%Y%m%dT%H%M%SZ}-{fault.id}",
        "fault": fault.id,
        "summary": fault.summary,
        "root_cause": asdict(fault.root_cause),
        "expected_signals": list(fault.expected_signals),
        "duration_s": duration,
        "injected_at": _iso(started),
        "ended_at": None,
        "ended_by": None,
    }
    save(run)
    return run


def save(run: dict) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    _path(run["run_id"]).write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


def finish(run: dict, ended_by: str) -> None:
    run["ended_at"] = _iso(now())
    run["ended_by"] = ended_by
    save(run)


def all_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RUNS_DIR.glob("*.json"))]


def open_runs() -> list[dict]:
    return [r for r in all_runs() if r["ended_at"] is None]
