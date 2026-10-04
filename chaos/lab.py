"""Thin wrappers around `docker compose` for injecting and reverting faults."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_SERVICES = ("gateway", "orders", "inventory", "payments")
LOCK_APP_NAME = "nightly-report"  # what the rogue session looks like in pg_stat_activity

# Runs inside a service container; talks to that service's /_chaos endpoint.
_CHAOS_CLIENT = """
import sys, urllib.request, urllib.error
body = sys.stdin.buffer.read() or None
req = urllib.request.Request("http://localhost:8000/_chaos", data=body, method=sys.argv[1],
                             headers={"content-type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except urllib.error.HTTPError as exc:
    print(exc.read().decode()); sys.exit(1)
"""


class LabError(RuntimeError):
    pass


def compose(*args: str, input: str | None = None, check: bool = True) -> str:
    cmd = ["docker", "compose", "--project-directory", str(ROOT), *args]
    result = subprocess.run(cmd, input=input, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise LabError(f"`{' '.join(cmd[4:])}` failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _chaos_call(service: str, method: str, body: dict | None = None) -> dict:
    out = compose(
        "exec", "-T", service, "python", "-c", _CHAOS_CLIENT, method,
        input=json.dumps(body) if body is not None else "",
    )
    return json.loads(out)


def inject_in_app(service: str, spec: dict) -> None:
    _chaos_call(service, "POST", spec)


def stop(service: str) -> None:
    compose("stop", service)


def pause(service: str) -> None:
    compose("pause", service)


def _psql(sql: str, *, detach: bool = False, app_name: str = "chaos-admin") -> str:
    flags = ["-d"] if detach else ["-T"]
    return compose(
        "exec", *flags, "-e", f"PGAPPNAME={app_name}", "postgres",
        "psql", "-U", "shop", "-d", "shop", "-tAc", sql,
    )


def lock_table(table: str, duration: float) -> None:
    # Runs detached inside the postgres container, so the lock outlives this process
    # and releases itself when the sleep ends.
    # Ending this session with pg_terminate_backend would make Postgres log its full SQL,
    # an obviously artificial pg_sleep; the setting keeps the statement out of the logs.
    _psql(
        "SET log_min_error_statement = panic; "
        f"BEGIN; LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep({duration:.0f}); COMMIT;",
        detach=True,
        app_name=LOCK_APP_NAME,
    )


def container_states() -> dict[str, str]:
    """Service name -> container state (running, paused, exited, ...)."""
    out = compose("ps", "-a", "--format", "json")
    # Depending on the compose version this is one JSON array or one object per line.
    rows = json.loads(out) if out.lstrip().startswith("[") else [json.loads(l) for l in out.splitlines() if l.strip()]
    return {row["Service"]: row["State"] for row in rows}


def in_app_faults(states: dict[str, str] | None = None) -> dict[str, dict]:
    """Service -> active in-app fault, for running services that have one."""
    states = states or container_states()
    faults = {}
    for service in APP_SERVICES:
        if states.get(service) == "running":
            status = _chaos_call(service, "GET")
            if status["active"]:
                faults[service] = status
    return faults


def lock_sessions(states: dict[str, str] | None = None) -> int:
    if (states or container_states()).get("postgres") != "running":
        return 0
    return int(_psql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{LOCK_APP_NAME}'").strip())


def clear_all() -> list[str]:
    """Revert every fault the engine knows how to inject. Safe to run any time."""
    actions = []
    states = container_states()
    for service, state in states.items():
        if state == "paused":
            compose("unpause", service)
            actions.append(f"unpaused {service}")
    stopped = [s for s, state in states.items() if state in ("exited", "created")]
    if stopped:
        compose("start", *stopped)
        actions.append(f"started {', '.join(stopped)}")

    # Services restarted above carry no faults (and may still be booting): only check the others.
    if lock_sessions(states):
        _psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE application_name = '{LOCK_APP_NAME}'"
        )
        actions.append("terminated lock-holding database session")

    for service in in_app_faults(states):
        _chaos_call(service, "DELETE")
        actions.append(f"cleared in-app fault on {service}")
    return actions
