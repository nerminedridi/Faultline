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
1. The page comes with the current alerts and a log summary: start from those.
2. Work out which services are affected and how (errors, latency, which status codes).
3. Follow the dependency chain downstream to the deepest component that is itself misbehaving,
   rather than just reacting to a failing dependency.
4. Confirm the mechanism with direct evidence (specific log lines, metric values) before concluding.
5. Before submitting, name the other root causes that would produce the same symptoms, and check
   the evidence that rules each one out. If you have not checked it, check it first.
Confidence: "high" only when you have direct evidence of the fault in the originating component
itself (its own logs or metrics show the mechanism), and the lookalikes are ruled out by evidence.
"medium" when the conclusion is inferred from symptoms elsewhere. "low" when it is a best guess.
Your tools only return data from the incident window given in the page; earlier data is out of
scope and unavailable.
Every turn costs one request against a small daily quota: when several lookups don't depend on each
other, request them together in the same turn. Be purposeful; when the evidence supports a
conclusion, call submit_diagnosis.

Kinds of root cause (pick the kind of the originating fault, not of the symptoms it causes further
along: timeouts, errors and exhausted resources are often consequences of something else):
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
    if not [r for r in args.get("ruled_out") or [] if str(r).strip()]:
        return ("ruled_out is empty: name at least one other root cause that would produce the same "
                "symptoms and the evidence that rules it out (check it first if you have not)")
    if args.get("service") not in tools.SERVICES:
        return f"service must be one of {tools.SERVICES}"
    if args.get("kind") not in tools.KINDS:
        return f"kind must be one of {tools.KINDS}"
    if not args.get("summary"):
        return "summary is required"
    return None


def investigate(llm, complaint: str, window_start: float, max_steps: int = 15, echo=print) -> dict:
    """Investigate the incident that began around window_start (epoch seconds) and is ongoing."""
    started = datetime.now(timezone.utc)
    since = datetime.fromtimestamp(window_start, timezone.utc)
    tools.open_window(window_start)
    # Like a real page, it arrives with context: saves the model two requests fetching it.
    opening = (
        f"Page received at {started:%Y-%m-%d %H:%M:%S} UTC: {complaint}\n"
        f"Incident window: since {since:%H:%M:%S} UTC ({(started - since).total_seconds() / 60:.1f} min). "
        f"Find the root cause of what is happening now.\n\n"
        f"## Current alerts\n{tools.run('get_alerts', {})}\n\n"
        f"## {tools.run('log_summary', {})}"
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
        "window_start": since.isoformat(timespec="seconds"),
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
