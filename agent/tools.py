"""The agent's view of the system: alerts, metrics and logs, nothing else.

Everything is read-only and goes through Prometheus and Loki, the same way an
on-call engineer would look through Grafana. Results are compact text: tool
output is the bulk of every prompt, and free-tier token budgets are small.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI = os.getenv("LOKI_URL", "http://localhost:3100")

SERVICES = ["gateway", "orders", "inventory", "payments", "postgres"]
# Shop containers only: the observability stack's own logs are noise for the agent.
SHOP = '{service=~"gateway|orders|inventory|payments|postgres|loadgen"}'
MAX_OUTPUT = 6000  # characters per tool result
KINDS = ["latency", "errors", "crash", "hang", "resource-exhaustion", "lock-contention", "bad-config"]


class ToolError(Exception):
    pass


def _get(base: str, path: str, **params) -> dict:
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"{exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"cannot reach {base}: {exc.reason}") from exc


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n... [truncated, {len(text)} chars total]"


def _hms(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%H:%M:%S")


def _logql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selector(service: str | None) -> str:
    if service is None:
        return SHOP
    if service not in SERVICES + ["loadgen"]:
        raise ToolError(f"unknown service {service!r}; choose from {SERVICES + ['loadgen']}")
    return f'{{service="{service}"}}'


def _labels(metric: dict) -> str:
    shown = {k: v for k, v in metric.items() if k not in ("instance", "job")}
    name = shown.pop("__name__", "")
    return name + "{" + ",".join(f'{k}="{v}"' for k, v in sorted(shown.items())) + "}"


def _num(value: str) -> str:
    v = float(value)
    return value if v != v else f"{v:.3g}"  # NaN stays NaN


# ---------- tools ----------

def get_alerts() -> str:
    alerts = _get(PROMETHEUS, "/api/v1/alerts")["data"]["alerts"]
    if not alerts:
        return "No alerts are firing or pending."
    lines = []
    for a in sorted(alerts, key=lambda a: (a["state"], a["labels"]["alertname"])):
        since = a.get("activeAt", "")[11:19]
        lines.append(
            f"[{a['state']}] {a['labels']['alertname']} service={a['labels'].get('service', '-')} "
            f"since {since}Z: {a['annotations'].get('summary', '')}"
        )
    return "\n".join(lines)


def query_metrics(promql: str, range_minutes: int = 0) -> str:
    if range_minutes <= 0:
        result = _get(PROMETHEUS, "/api/v1/query", query=promql)["data"]["result"]
        if not result:
            return "No data (empty result)."
        if isinstance(result, list) and result and "value" in result[0]:
            return _clip("\n".join(f"{_labels(r['metric'])} = {_num(r['value'][1])}" for r in result[:60]))
        return _clip(json.dumps(result))

    end = time.time()
    step = max(15, range_minutes * 60 // 12)
    data = _get(PROMETHEUS, "/api/v1/query_range", query=promql,
                start=end - range_minutes * 60, end=end, step=step)["data"]["result"]
    if not data:
        return "No data (empty result)."
    lines = [f"{len(data)} series, one value every {step} s, oldest -> newest (ends {_hms(end)}Z):"]
    for series in data[:40]:
        lines.append(f"{_labels(series['metric'])}: " + " ".join(_num(v) for _, v in series["values"]))
    return _clip("\n".join(lines))


def log_summary(minutes: int = 10, service: str | None = None) -> str:
    end = time.time()
    # Structured (JSON) logs: counted by Loki itself, so this covers every line in the window.
    fields = 'level="level", msg="msg", target="target", status="status"'
    counts = _get(LOKI, "/loki/api/v1/query", time=int(end * 1e9), query=(
        f"sum by (service, level, msg, target, status) "
        f"(count_over_time({_selector(service)} | json {fields} | __error__=\"\" [{minutes}m]))"
    ))["data"]["result"]
    rows = sorted(counts, key=lambda r: -float(r["value"][1]))
    lines = [f"Log lines in the last {minutes} min, grouped (count  service  level  message  [fields]):"]
    for r in rows[:40]:
        m = r["metric"]
        extra = " ".join(f"{k}={m[k]}" for k in ("target", "status") if m.get(k))
        lines.append(f"{int(float(r['value'][1])):>6}  {m.get('service')}  {m.get('level')}  {m.get('msg')!r}  {extra}")

    # Plain-text logs (postgres): sampled and grouped here, with numbers masked.
    raw = _get(LOKI, "/loki/api/v1/query_range", query=f'{_selector(service)} !~ "^\\\\{{"',
               start=int((end - minutes * 60) * 1e9), end=int(end * 1e9), limit=2000)["data"]["result"]
    plain = Counter()
    for stream in raw:
        for _, line in stream["values"]:
            text = re.sub(r"^\S+ \S+ \S+ \[\d+\] ", "", line)  # drop postgres' timestamp and pid prefix
            plain[(stream["stream"].get("service"), re.sub(r"\d+(\.\d+)?", "N", text)[:120])] += 1
    if plain:
        lines.append("Plain-text log lines (numbers masked as N):")
        lines += [f"{n:>6}  {svc}  {text}" for (svc, text), n in plain.most_common(15)]
    return _clip("\n".join(lines))


def _format_line(service: str, ts_ns: str, line: str) -> str:
    try:
        entry = json.loads(line)
    except ValueError:
        return f"{_hms(int(ts_ns) / 1e9)} {service} | {line[:300]}"
    exc = entry.pop("exception", None)
    head = f"{entry.pop('ts', '')[11:23]} {entry.pop('service', service)} {entry.pop('level', '')} {entry.pop('msg', '')!r}"
    rest = " ".join(f"{k}={v}" for k, v in entry.items())
    out = f"{head} {rest}"
    if exc:
        out += "\n    exception: " + " | ".join(exc.strip().splitlines()[-2:])
    return out[:600]


def search_logs(service: str | None = None, contains: str | None = None, level: str | None = None,
                minutes: int = 10, limit: int = 30) -> str:
    query = _selector(service)
    if contains:
        query += f" |= {_logql_string(contains)}"
    if level:
        query += " |= " + _logql_string('"level": "%s"' % level)
    end = time.time()
    streams = _get(LOKI, "/loki/api/v1/query_range", query=query, direction="backward",
                   start=int((end - minutes * 60) * 1e9), end=int(end * 1e9), limit=min(limit, 100))["data"]["result"]
    entries = sorted(
        (ts, s["stream"].get("service", "?"), line) for s in streams for ts, line in s["values"]
    )
    if not entries:
        return "No matching log lines."
    return _clip(f"{len(entries)} newest matching lines, oldest first:\n" +
                 "\n".join(_format_line(svc, ts, line) for ts, svc, line in entries))


def trace_request(request_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{4,64}", request_id):
        raise ToolError("request_id should look like 'a1b2c3d4e5f6'")
    return search_logs(contains=request_id, minutes=60, limit=100)


# ---------- declarations, in the JSON-schema subset both Gemini and Ollama accept ----------

_minutes = {"type": "integer", "description": "How far back to look, in minutes (default 10)."}
_service = {"type": "string", "enum": SERVICES + ["loadgen"], "description": "Limit to one service (default: all)."}

DECLARATIONS = [
    {
        "name": "get_alerts",
        "description": "Prometheus alerts that are firing or pending right now.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "log_summary",
        "description": "Counts of log lines grouped by service, level and message over a time window. "
                       "The fastest way to see what is being logged, and where.",
        "parameters": {"type": "object", "properties": {"minutes": _minutes, "service": _service}},
    },
    {
        "name": "search_logs",
        "description": "The newest log lines matching the filters, with all their fields (and exception tails).",
        "parameters": {
            "type": "object",
            "properties": {
                "service": _service,
                "contains": {"type": "string", "description": "Case-sensitive substring the line must contain."},
                "level": {"type": "string", "enum": ["info", "warning", "error"],
                          "description": "Only structured lines at this level (plain-text lines are excluded)."},
                "minutes": _minutes,
                "limit": {"type": "integer", "description": "Max lines, up to 100 (default 30)."},
            },
        },
    },
    {
        "name": "trace_request",
        "description": "Every log line, across all services, for one request_id: follows a single request end to end.",
        "parameters": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    },
    {
        "name": "query_metrics",
        "description": "Run a PromQL query. With range_minutes=0 returns current values; otherwise a "
                       "time series of about 12 points over that many minutes.",
        "parameters": {
            "type": "object",
            "properties": {
                "promql": {"type": "string"},
                "range_minutes": {"type": "integer", "description": "0 for an instant query (default)."},
            },
            "required": ["promql"],
        },
    },
    {
        "name": "submit_diagnosis",
        "description": "Submit the root cause. Call this once, when the evidence supports a conclusion.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": SERVICES,
                            "description": "The component where the fault originates, not where symptoms show."},
                "kind": {"type": "string", "enum": KINDS},
                "summary": {"type": "string", "description": "One or two sentences: what is wrong and why."},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "The specific observations that support it."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["service", "kind", "summary", "evidence", "confidence"],
        },
    },
]

RUNNERS = {
    "get_alerts": get_alerts,
    "log_summary": log_summary,
    "search_logs": search_logs,
    "trace_request": trace_request,
    "query_metrics": query_metrics,
}


def run(name: str, args: dict) -> str:
    if name not in RUNNERS:
        return f"error: unknown tool {name!r}"
    try:
        # Models sometimes send integers as floats (10.0); the tools expect ints.
        args = {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in args.items()}
        return RUNNERS[name](**args)
    except ToolError as exc:
        return f"error: {exc}"
    except TypeError as exc:
        return f"error: bad arguments: {exc}"
