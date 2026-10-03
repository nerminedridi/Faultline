"""In-process fault injection, driven by the chaos engine (see /chaos at the repo root).

Each service exposes POST/GET/DELETE /_chaos. The endpoint is deliberately
invisible to the telemetry the agent investigates: it is excluded from metrics
and request logs, and injected faults only ever log what the real failure would
log. Nothing here mentions chaos in a log line, metric or traceback.

A fault always expires on its own (duration_s) so a crashed controller can't
leave the lab broken.
"""

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

CHAOS_PATH = "/_chaos"


class FaultSpec(BaseModel):
    # Extra latency, uniformly drawn from [min, max] milliseconds.
    latency_ms: tuple[int, int] | None = None
    # Fraction of requests answered with error_status instead of being handled.
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    error_status: int = 500
    # What the service logs for each injected error: it should read like the real failure.
    error_log: str = "internal error"
    # Only affect these route templates (e.g. ["/charge"]); empty means every route.
    routes: list[str] = []
    # Service-specific faults registered with register(): {"name": value}.
    hooks: dict[str, float] = {}
    duration_s: float = Field(default=300, gt=0, le=3600)


Hook = tuple[Callable[[float], Awaitable[None]], Callable[[], Awaitable[None]]]


@dataclass
class _State:
    spec: FaultSpec | None = None
    expires_at: float = 0.0
    expiry_task: asyncio.Task | None = None
    hooks: dict[str, Hook] = field(default_factory=dict)
    params: dict[str, float] = field(default_factory=dict)


_state = _State()


def register(name: str, start: Callable[[float], Awaitable[None]], stop: Callable[[], Awaitable[None]]) -> None:
    """Register a service-specific fault, e.g. leaking database connections."""
    _state.hooks[name] = (start, stop)


def param(name: str, default: float) -> float:
    """Read a tunable the active fault may override (e.g. a decline rate)."""
    return _state.params.get(name, default)


def active() -> FaultSpec | None:
    if _state.spec is not None and time.monotonic() >= _state.expires_at:
        return None
    return _state.spec


async def apply(route: str) -> tuple[int, str] | None:
    """Run the active fault for one request.

    Sleeps for any injected latency and returns (status, log message) when the
    request should fail, or None to handle it normally.
    """
    spec = active()
    if spec is None or (spec.routes and route not in spec.routes):
        return None
    if spec.latency_ms:
        await asyncio.sleep(random.uniform(*spec.latency_ms) / 1000)
    if spec.error_rate and random.random() < spec.error_rate:
        return spec.error_status, spec.error_log
    return None


async def _stop_hooks() -> None:
    if _state.spec is not None:
        for name in _state.spec.hooks:
            await _state.hooks[name][1]()
    _state.spec = None


async def clear() -> None:
    if _state.expiry_task is not None and _state.expiry_task is not asyncio.current_task():
        _state.expiry_task.cancel()
    _state.expiry_task = None
    await _stop_hooks()


async def _expire(after: float) -> None:
    await asyncio.sleep(after)
    await clear()


async def inject(spec: FaultSpec) -> None:
    await clear()
    unknown = [name for name in spec.hooks if name not in _state.hooks]
    if unknown:
        raise ValueError(f"unknown hooks: {unknown}; available: {sorted(_state.hooks)}")
    _state.spec = spec
    _state.expires_at = time.monotonic() + spec.duration_s
    for name, value in spec.hooks.items():
        await _state.hooks[name][0](value)
    _state.expiry_task = asyncio.create_task(_expire(spec.duration_s))


def tunable(name: str) -> None:
    """Declare a param() the chaos engine is allowed to override."""

    async def start(value: float) -> None:
        _state.params[name] = value

    async def stop() -> None:
        _state.params.pop(name, None)

    register(name, start, stop)


def mount(app: FastAPI) -> None:
    @app.post(CHAOS_PATH, include_in_schema=False)
    async def chaos_inject(spec: FaultSpec):
        try:
            await inject(spec)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _describe()

    @app.get(CHAOS_PATH, include_in_schema=False)
    async def chaos_status():
        return _describe()

    @app.delete(CHAOS_PATH, include_in_schema=False)
    async def chaos_clear():
        await clear()
        return _describe()


def _describe() -> dict:
    spec = active()
    return {
        "active": spec is not None,
        "fault": spec.model_dump() if spec else None,
        "remaining_s": round(_state.expires_at - time.monotonic(), 1) if spec else 0,
        "available_hooks": sorted(_state.hooks),
    }
