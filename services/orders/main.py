"""Orders: reserves stock, charges payment and stores orders in PostgreSQL."""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import Gauge
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from common import chaos
from common.observability import call_downstream, setup

INVENTORY_URL = os.environ["INVENTORY_URL"]
PAYMENTS_URL = os.environ["PAYMENTS_URL"]

# A short checkout timeout means an exhausted pool shows up as errors quickly.
pool = AsyncConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=2,
    max_size=int(os.getenv("DB_POOL_SIZE", "10")),
    timeout=2.0,
    # Validate connections on checkout so the pool recovers by itself after a database restart.
    check=AsyncConnectionPool.check_connection,
    open=False,
    kwargs={"row_factory": dict_row},
)
client = httpx.AsyncClient(timeout=3.0)

POOL_STATS = Gauge("db_pool_connections", "Database pool connections, by state", ["state"])
POOL_STATS.labels("max").set_function(lambda: pool.max_size)
POOL_STATS.labels("in_use").set_function(
    lambda: (s := pool.get_stats())["pool_size"] - s["pool_available"]
)
POOL_STATS.labels("waiting").set_function(lambda: pool.get_stats()["requests_waiting"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    sku         TEXT           NOT NULL,
    qty         INT            NOT NULL,
    amount      NUMERIC(10, 2) NOT NULL,
    status      TEXT           NOT NULL,
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT now()
)
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    yield
    await pool.close()
    await client.aclose()


app = FastAPI(title="orders", lifespan=lifespan)
log = setup(app, "orders")


class NewOrder(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=10)


async def set_status(order_id: int, status: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))


async def release_stock(order: NewOrder) -> None:
    try:
        await call_downstream(
            client, log, "inventory", "POST", f"{INVENTORY_URL}/release", json=order.model_dump()
        )
    except HTTPException:
        log.error("failed to release stock", sku=order.sku, qty=order.qty)


# ---------- fault: a code path that checks out connections and never returns them ----------
leaked: list = []
leak_task: asyncio.Task | None = None


async def _leak(count: float) -> None:
    while len(leaked) < int(count):
        try:
            leaked.append(await pool.getconn())
        except PoolTimeout:
            pass  # competing with live traffic for the last connections; keep trying


async def start_leak(count: float) -> None:
    global leak_task
    leak_task = asyncio.create_task(_leak(count))


async def stop_leak() -> None:
    if leak_task is not None:
        leak_task.cancel()
    while leaked:
        await pool.putconn(leaked.pop())


chaos.register("leak_db_connections", start_leak, stop_leak)


@app.post("/orders")
async def create_order(order: NewOrder):
    reserved = await call_downstream(
        client, log, "inventory", "POST", f"{INVENTORY_URL}/reserve", json=order.model_dump()
    )
    if reserved.status_code in (404, 409):
        return JSONResponse(reserved.json(), status_code=reserved.status_code)
    if reserved.status_code != 200:
        return JSONResponse({"error": "inventory error"}, status_code=502)

    amount = round(reserved.json()["unit_price"] * order.qty, 2)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO orders (sku, qty, amount, status) VALUES (%s, %s, %s, 'pending') RETURNING id",
                (order.sku, order.qty, amount),
            )
            order_id = (await cur.fetchone())["id"]
    except Exception:
        await release_stock(order)  # the order was never stored: don't keep its stock reserved
        raise

    try:
        paid = await call_downstream(
            client, log, "payments", "POST", f"{PAYMENTS_URL}/charge",
            json={"order_id": order_id, "amount": amount},
        )
    except HTTPException:
        await set_status(order_id, "failed")
        await release_stock(order)
        raise

    if paid.status_code == 402:
        await set_status(order_id, "declined")
        await release_stock(order)
        return JSONResponse({"order_id": order_id, "status": "declined"}, status_code=402)
    if paid.status_code != 200:
        await set_status(order_id, "failed")
        await release_stock(order)
        return JSONResponse({"error": "payment error"}, status_code=502)

    await set_status(order_id, "paid")
    log.info("order paid", order_id=order_id, sku=order.sku, qty=order.qty, amount=amount)
    return {"order_id": order_id, "status": "paid", "amount": amount}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, sku, qty, amount::float AS amount, status, created_at FROM orders WHERE id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return JSONResponse({"error": "order not found"}, status_code=404)
    return row
