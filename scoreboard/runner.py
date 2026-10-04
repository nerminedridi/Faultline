"""One trial = quiet lab -> inject a fault -> let symptoms develop -> investigate -> revert -> score."""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from agent.investigator import investigate
from chaos import engine, lab
from chaos.faults import FAULTS

PROMETHEUS = "http://localhost:9090"
COMPLAINT = "Something may be wrong with the shop. Check whether customers are affected."
BASELINE_S = 60  # the investigation window opens this long before the injection
FAULT_TTL_S = 1800  # safety net: in-app faults and the table lock expire on their own


def _prom(query: str) -> list:
    url = f"{PROMETHEUS}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)["data"]["result"]


def lab_is_quiet() -> tuple[bool, str]:
    """No alerts, no server errors, every service answering: safe to start the next trial."""
    alerts = [a for a in _prom("ALERTS") if a["metric"].get("alertstate") in ("firing", "pending")]
    if alerts:
        return False, f"{len(alerts)} alert(s) still active"
    errors = _prom('sum(rate(http_requests_total{status=~"5.."}[1m]))')
    if errors and float(errors[0]["value"][1]) > 0:
        return False, "5xx errors in the last minute"
    if _prom('up{job="shop"} == 0'):
        return False, "a service is not answering scrapes"
    return True, "quiet"


def wait_until_quiet(min_wait: float, timeout: float = 600, echo=print) -> None:
    deadline = time.monotonic() + timeout
    time.sleep(min_wait)
    while True:
        quiet, why = lab_is_quiet()
        if quiet:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"lab did not recover within {timeout:.0f} s: {why}")
        echo(f"    waiting for the lab to recover ({why})")
        time.sleep(15)


def score(truth: dict, diagnosis: dict | None) -> dict:
    service_ok = bool(diagnosis) and diagnosis["service"] == truth["service"]
    kind_ok = bool(diagnosis) and diagnosis["kind"] == truth["kind"]
    return {"service_ok": service_ok, "kind_ok": kind_ok, "correct": service_ok and kind_ok}


def run_trial(fault_id: str, llm, warmup: float, max_steps: int, echo=print) -> dict:
    lab.clear_all()  # belt and braces: nothing left over from an interrupted trial
    run = engine.inject(fault_id, FAULT_TTL_S)
    injected = datetime.fromisoformat(run["injected_at"]).timestamp()
    report = None
    try:
        echo(f"    injected; letting symptoms develop for {warmup:.0f} s")
        time.sleep(warmup)
        report = investigate(llm, COMPLAINT, window_start=injected - BASELINE_S,
                             max_steps=max_steps, echo=lambda line: echo("  " + line))
    finally:
        engine.revert(run, "scoreboard")

    truth = FAULTS[fault_id].root_cause
    truth = {"service": truth.service, "kind": truth.kind}
    return {
        "fault": fault_id,
        "run_id": run["run_id"],
        "truth": truth,
        "diagnosis": report["diagnosis"],
        **score(truth, report["diagnosis"]),
        "tool_calls": report["tool_calls"],
        "usage": report["usage"],
        "duration_s": report["duration_s"],
        "transcript": report["transcript"],
    }
