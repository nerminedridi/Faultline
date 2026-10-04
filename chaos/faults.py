"""The fault catalog: every fault the engine can inject, with its ground truth.

The ground truth is what the agent is scored against: the service where the
fault actually lives (not where the alerts fire) and the kind of failure.
"""

from dataclasses import dataclass
from typing import Callable

from chaos import lab


@dataclass(frozen=True)
class RootCause:
    service: str
    kind: str  # latency | errors | crash | hang | resource-exhaustion | lock-contention | bad-config
    description: str


@dataclass(frozen=True)
class Fault:
    id: str
    summary: str
    root_cause: RootCause
    # What should show up in the telemetry; documents the scenario, not used for scoring.
    expected_signals: tuple[str, ...]
    inject: Callable[[float], None]  # takes the duration in seconds
    # Held out from agent tuning: only run in a final, honest scoreboard pass.
    holdout: bool = False


def _in_app(service: str, **spec) -> Callable[[float], None]:
    return lambda duration: lab.inject_in_app(service, {**spec, "duration_s": duration})


FAULTS: dict[str, Fault] = {
    f.id: f
    for f in [
        Fault(
            id="payments-latency",
            summary="Card charges take 1.5-2.5 s instead of ~50 ms",
            root_cause=RootCause(
                "payments", "latency", "The payments processor is slow to answer /charge."
            ),
            expected_signals=(
                "HighLatency on payments, orders and gateway",
                "no rise in 5xx: checkouts still succeed, slowly",
            ),
            inject=_in_app("payments", latency_ms=(1500, 2500), routes=["/charge"]),
        ),
        Fault(
            id="payments-errors",
            summary="Half of all card charges fail with 503",
            root_cause=RootCause(
                "payments", "errors", "The payments processor cannot reach its card acquirer."
            ),
            expected_signals=(
                "HighErrorRate on payments, orders and gateway",
                "payments logs 'acquirer connection failed'",
                "orders marks orders failed and releases stock",
            ),
            inject=_in_app(
                "payments",
                error_rate=0.5,
                error_status=503,
                error_log="acquirer connection failed: card network unreachable",
                routes=["/charge"],
            ),
        ),
        Fault(
            id="payments-declines",
            summary="The card decline rate jumps from 3% to 60%",
            root_cause=RootCause(
                "payments", "bad-config", "Payments declines most cards: its decline threshold is wrong."
            ),
            expected_signals=(
                "no alert fires: declines are 402s, not 5xx",
                "checkout 402 rate and 'card declined' logs spike",
            ),
            inject=lambda duration: lab.inject_in_app(
                "payments", {"hooks": {"decline_rate": 0.6}, "duration_s": duration}
            ),
        ),
        Fault(
            id="orders-db-pool-leak",
            summary="Orders leaks every database connection in its pool",
            root_cause=RootCause(
                "orders",
                "resource-exhaustion",
                "A code path in orders checks out database connections and never returns them.",
            ),
            expected_signals=(
                "HighErrorRate on orders and gateway",
                "orders logs PoolTimeout tracebacks after ~2 s",
                "db_pool_connections: in_use at max, requests waiting",
                "postgres is healthy: no connection errors, no lock waits",
            ),
            # Matches DB_POOL_SIZE in docker-compose.yml.
            inject=lambda duration: lab.inject_in_app(
                "orders", {"hooks": {"leak_db_connections": 10}, "duration_s": duration}
            ),
        ),
        Fault(
            id="inventory-down",
            summary="The inventory container is stopped",
            root_cause=RootCause("inventory", "crash", "The inventory service is down."),
            expected_signals=(
                "ServiceDown for inventory",
                "gateway and orders get connection errors to inventory (502)",
            ),
            inject=lambda duration: lab.stop("inventory"),
        ),
        Fault(
            id="inventory-hang",
            summary="The inventory process is frozen: connections open, nothing answers",
            root_cause=RootCause(
                "inventory", "hang", "The inventory process is hung and accepts no work."
            ),
            expected_signals=(
                "ServiceDown for inventory (scrapes time out)",
                "timeouts, not connection errors: orders 504 after 3 s, gateway after 5 s",
            ),
            inject=lambda duration: lab.pause("inventory"),
        ),
        Fault(
            id="postgres-down",
            summary="The PostgreSQL container is stopped",
            root_cause=RootCause("postgres", "crash", "The orders database is down."),
            expected_signals=(
                "no ServiceDown: postgres is not scraped by Prometheus",
                "HighErrorRate on orders and gateway",
                "orders logs \"error connecting in 'pool-1'\" warnings",
                "browsing products still works",
            ),
            inject=lambda duration: lab.stop("postgres"),
        ),
        Fault(
            id="db-lock",
            summary="A reporting job holds an exclusive lock on the orders table",
            root_cause=RootCause(
                "postgres",
                "lock-contention",
                "A long-running transaction (application 'nightly-report') "
                "holds an ACCESS EXCLUSIVE lock on the orders table.",
            ),
            expected_signals=(
                "checkout and order tracking hang, then time out (gateway 504)",
                "HighLatency and HighErrorRate on orders and gateway; PoolTimeout in orders",
                "postgres logs 'still waiting for RowExclusiveLock', naming the lock holder",
                "every service, and postgres, is up",
            ),
            inject=lambda duration: lab.lock_table("orders", duration),
        ),
        # ---------- held out: never used while tuning the agent ----------
        Fault(
            id="inventory-latency",
            summary="Inventory takes 1.2-2 s to answer catalog and reservation calls",
            root_cause=RootCause("inventory", "latency", "The inventory service is slow to respond."),
            expected_signals=(
                "HighLatency on inventory, orders and gateway",
                "browsing and checkout both slow; no rise in 5xx",
            ),
            inject=_in_app("inventory", latency_ms=(1200, 2000), routes=["/products", "/reserve"]),
            holdout=True,
        ),
        Fault(
            id="payments-down",
            summary="The payments container is stopped",
            root_cause=RootCause("payments", "crash", "The payments service is down."),
            expected_signals=(
                "ServiceDown for payments",
                "orders times out calling payments, fails checkouts and releases stock",
                "browsing and order tracking still work",
            ),
            inject=lambda duration: lab.stop("payments"),
            holdout=True,
        ),
        Fault(
            id="postgres-hang",
            summary="The PostgreSQL process is frozen: connections open, no queries answered",
            root_cause=RootCause("postgres", "hang", "The orders database is hung and answers nothing."),
            expected_signals=(
                "no ServiceDown: postgres is not scraped",
                "orders requests hang, then the pool runs dry (PoolTimeout); gateway 504s",
                "postgres logs nothing at all: no shutdown, no lock waits",
            ),
            inject=lambda duration: lab.pause("postgres"),
            holdout=True,
        ),
    ]
}
