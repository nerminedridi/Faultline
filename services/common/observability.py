"""Shared logging, metrics and downstream-call helpers for every service.

Every service logs one JSON object per line to stdout (collected into Loki)
and exposes Prometheus metrics on /metrics. A request id is propagated via the
X-Request-ID header so a single request can be followed across services.
"""

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.routing import Match

from common import chaos

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# The `service` label is added by Prometheus at scrape time, not here.
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by route and status code",
    ["method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
DOWNSTREAM_ERRORS = Counter(
    "downstream_errors_total",
    "Failed calls to other services (connection errors, timeouts, 5xx)",
    ["target", "kind"],
)

UNTRACKED_PATHS = {"/metrics", "/health", chaos.CHAOS_PATH}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class Log:
    """Thin wrapper so call sites can pass structured fields as kwargs."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _emit(self, level: int, msg: str, exc_info: bool = False, **fields):
        self._logger.log(level, msg, exc_info=exc_info, extra={"fields": fields})

    def info(self, msg: str, **fields):
        self._emit(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields):
        self._emit(logging.WARNING, msg, **fields)

    def error(self, msg: str, **fields):
        self._emit(logging.ERROR, msg, **fields)

    def exception(self, msg: str, **fields):
        self._emit(logging.ERROR, msg, exc_info=True, **fields)


def configure_logging(service: str) -> Log:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    logger = logging.getLogger(service)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Library warnings (e.g. the DB pool failing to reconnect) get the same JSON format.
    logging.root.handlers = [handler]
    logging.root.setLevel(logging.WARNING)
    return Log(logger)


def _route_template(app: FastAPI, scope) -> str:
    # Use the route pattern (/orders/{order_id}) rather than the raw path so
    # metrics don't get one time series per order id.
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.path
    return "unmatched"


def setup(app: FastAPI, service: str) -> Log:
    """Attach request logging, metrics, /metrics, /health and fault injection to an app."""
    log = configure_logging(service)
    chaos.mount(app)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        if request.url.path in UNTRACKED_PATHS:
            return await call_next(request)

        token = request_id_var.set(request.headers.get("x-request-id") or uuid.uuid4().hex[:12])
        route = _route_template(app, request.scope)
        start = time.perf_counter()
        try:
            injected = await chaos.apply(route)
            if injected:
                status, message = injected
                log.error(message, method=request.method, path=request.url.path)
                response = JSONResponse({"error": message}, status_code=status)
            else:
                response = await call_next(request)
        except Exception:
            log.exception("unhandled error", method=request.method, path=request.url.path)
            response = JSONResponse({"error": "internal server error"}, status_code=500)

        duration = time.perf_counter() - start
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(request.method, route).observe(duration)
        log_fn = log.error if response.status_code >= 500 else log.info
        log_fn(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration * 1000, 1),
        )
        response.headers["x-request-id"] = request_id_var.get()
        request_id_var.reset(token)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "service": service}

    return log


async def call_downstream(
    client: httpx.AsyncClient, log: Log, target: str, method: str, url: str, **kwargs
) -> httpx.Response:
    """Call another service, propagating the request id.

    Connection errors and timeouts become a 502 for our caller; the downstream
    response is returned as-is otherwise (callers decide what a 4xx means).
    """
    try:
        response = await client.request(
            method, url, headers={"x-request-id": request_id_var.get()}, **kwargs
        )
    except httpx.TimeoutException as exc:
        DOWNSTREAM_ERRORS.labels(target, "timeout").inc()
        log.error("downstream timeout", target=target, url=url, error=repr(exc))
        raise HTTPException(status_code=504, detail=f"{target} timed out") from exc
    except httpx.HTTPError as exc:
        DOWNSTREAM_ERRORS.labels(target, "connection").inc()
        log.error("downstream unreachable", target=target, url=url, error=repr(exc))
        raise HTTPException(status_code=502, detail=f"{target} unavailable") from exc

    if response.status_code >= 500:
        DOWNSTREAM_ERRORS.labels(target, "5xx").inc()
        log.error("downstream error", target=target, url=url, status=response.status_code)
    return response


def forward(response: httpx.Response) -> JSONResponse:
    """Relay a downstream response; a downstream 5xx becomes our 502."""
    if response.status_code >= 500:
        return JSONResponse({"error": "upstream error"}, status_code=502)
    return JSONResponse(response.json(), status_code=response.status_code)
