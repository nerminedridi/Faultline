"""Inject and revert faults while keeping the ground-truth records consistent."""

from chaos import lab, runs
from chaos.faults import FAULTS


class FaultActive(RuntimeError):
    pass


def inject(fault_id: str, duration: float) -> dict:
    """Inject a fault and open its run record. Raises FaultActive or lab.LabError."""
    if active := runs.open_runs():
        raise FaultActive(f"a fault is already active ({active[0]['run_id']})")
    fault = FAULTS[fault_id]
    run = runs.start(fault, duration)
    try:
        fault.inject(duration)
    except lab.LabError:
        lab.clear_all()
        runs.finish(run, "failed")
        raise
    return run


def revert(run: dict, ended_by: str) -> list[str]:
    actions = lab.clear_all()
    runs.finish(run, ended_by)
    return actions
