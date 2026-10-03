"""The investigation loop: the model calls tools until it submits a diagnosis."""

import json
import time
from datetime import datetime, timezone

from agent import tools
from agent.llm import Reply

SYSTEM_PROMPT = """\
You are an on-call SRE investigating a live incident in an online shop. Your job is to find the
root cause: the single component where the fault originates. That is often not where the alerts
fire, because failures propagate up the call chain.

## Architecture
loadgen -> gateway -> orders -> inventory
                         |---> payments
                         '---> postgres
- gateway: public API. GET /api/products calls inventory; POST /api/checkout and GET /api/orders/{id}
  call orders. HTTP client timeout 5 s.
- orders: a checkout reserves stock (inventory /reserve), inserts the order into postgres, charges the
  card (payments /charge), then updates the order. On failure it releases the stock (/release).
  HTTP client timeout 3 s. Talks to postgres through a connection pool of 10 connections; waiting for
  a free connection times out after 2 s.
- inventory: in-memory product catalog and stock reservations.
- payments: card processor. Normally ~3% of cards are declined (HTTP 402), which is expected.
- postgres: the orders database. Not scraped by Prometheus, but its logs are in Loki (plain text).
- loadgen: simulated customers (~5 requests/s). It generates traffic; it is never the root cause.

## Telemetry
Prometheus metrics (every series has a `service` label):
- http_requests_total{method, route, status}            requests handled by each service
- http_request_duration_seconds_bucket{method, route, le} latency histogram
- downstream_errors_total{target, kind}                   failed calls to another service; kind is
                                                          timeout, connection or 5xx
- db_pool_connections{state}                              orders only: in_use, waiting, max
- up{job="shop"}                                          1 if the service answered its last scrape
Logs (Loki): one JSON object per line with service, level, msg, request_id and extra fields;
the request_id is shared by every service that handled the same customer request.
Alert rules: ServiceDown, HighErrorRate (>5% 5xx), HighLatency (p95 > 500 ms). An incident can
exist with no alert firing.

## Method
1. Get the big picture: alerts, and a log summary of the recent window.
2. Work out which services are affected and how (errors, latency, which status codes).
3. Follow the dependency chain downstream to the deepest component that is itself misbehaving,
   rather than just reacting to a failing dependency.
4. Confirm the mechanism with direct evidence (specific log lines, metric values) before concluding.
   Rule out the alternatives that would produce the same symptoms.
You have a limited number of tool calls, so be purposeful. When the evidence supports a conclusion,
call submit_diagnosis.

Kinds of root cause:
- latency: the component works but responds slowly.
- errors: the component is running but returns errors.
- crash: the component is not running at all.
- hang: the component is running but unresponsive (requests to it time out).
- resource-exhaustion: the component ran out of a finite resource (connections, memory, threads).
- lock-contention: work is blocked waiting on locks held by another session or process.
- bad-config: the component runs without errors but makes wrong decisions or returns wrong results.
"""

FINAL_NUDGE = "You are out of tool calls. Call submit_diagnosis now with your best conclusion."
NO_TOOL_NUDGE = "Continue the investigation with the tools, or call submit_diagnosis if you are done."


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _check_diagnosis(args: dict) -> str | None:
    if args.get("service") not in tools.SERVICES:
        return f"service must be one of {tools.SERVICES}"
    if args.get("kind") not in tools.KINDS:
        return f"kind must be one of {tools.KINDS}"
    if not args.get("summary"):
        return "summary is required"
    return None


def investigate(llm, complaint: str, minutes: int = 10, max_steps: int = 15, echo=print) -> dict:
    started = datetime.now(timezone.utc)
    opening = (
        f"Page received at {started:%Y-%m-%d %H:%M:%S} UTC: {complaint}\n"
        f"Investigate what is happening now (look at roughly the last {minutes} minutes) and find the root cause."
    )
    history: list[dict] = [{"role": "user", "text": opening}]
    usage = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}
    diagnosis = None
    t0 = time.monotonic()

    def ask(only: str | None = None) -> Reply:
        reply = llm.chat(SYSTEM_PROMPT, history, tools.DECLARATIONS, only=only)
        history.append({"role": "assistant", "reply": reply})
        usage["llm_calls"] += 1
        for k in ("input_tokens", "output_tokens"):
            usage[k] += reply.usage.get(k, 0)
        if reply.text:
            echo(f"  model: {reply.text[:300]}")
        return reply

    tool_calls = 0
    nudges = 0
    while diagnosis is None:
        out_of_steps = tool_calls >= max_steps
        if out_of_steps:
            history.append({"role": "user", "text": FINAL_NUDGE})
        reply = ask(only="submit_diagnosis" if out_of_steps else None)

        if not reply.tool_calls:
            if out_of_steps or nudges >= 2:
                break
            nudges += 1
            history.append({"role": "user", "text": NO_TOOL_NUDGE})
            continue

        results = []
        for call in reply.tool_calls:
            if call.name == "submit_diagnosis":
                problem = _check_diagnosis(call.args)
                if problem is None:
                    diagnosis = call.args
                    result = "Diagnosis recorded."
                else:
                    result = f"error: {problem}"
            else:
                tool_calls += 1
                echo(f"  [{tool_calls}] {call.name}({_brief(call.args)})")
                result = tools.run(call.name, call.args)
            results.append((call, result))
        history.append({"role": "tool", "results": results})
        if out_of_steps and diagnosis is None:
            break

    return {
        "started_at": started.isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - t0, 1),
        "complaint": complaint,
        "window_minutes": minutes,
        "tool_calls": tool_calls,
        "usage": usage,
        "diagnosis": diagnosis,
        "transcript": [_plain(turn) for turn in history],
    }


def _plain(turn: dict) -> dict:
    """A JSON-friendly copy of one history turn for the report."""
    if turn["role"] == "assistant":
        reply = turn["reply"]
        return {"role": "assistant", "text": reply.text,
                "tool_calls": [{"name": c.name, "args": c.args} for c in reply.tool_calls]}
    if turn["role"] == "tool":
        return {"role": "tool", "results": [{"name": c.name, "result": r} for c, r in turn["results"]]}
    return turn


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)
