"""Gateway: the public API. Routes customer requests to internal services."""

import os

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from common.observability import call_downstream, forward, setup

INVENTORY_URL = os.environ["INVENTORY_URL"]
ORDERS_URL = os.environ["ORDERS_URL"]

app = FastAPI(title="gateway")
log = setup(app, "gateway")
client = httpx.AsyncClient(timeout=5.0)


class Checkout(BaseModel):
    sku: str
    qty: int = Field(default=1, ge=1, le=10)


@app.get("/api/products")
async def products():
    return forward(await call_downstream(client, log, "inventory", "GET", f"{INVENTORY_URL}/products"))


@app.post("/api/checkout")
async def checkout(req: Checkout):
    return forward(
        await call_downstream(client, log, "orders", "POST", f"{ORDERS_URL}/orders", json=req.model_dump())
    )


@app.get("/api/orders/{order_id}")
async def order(order_id: int):
    return forward(await call_downstream(client, log, "orders", "GET", f"{ORDERS_URL}/orders/{order_id}"))
