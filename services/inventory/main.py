"""Inventory: product catalog and stock reservations (in memory)."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.observability import setup

app = FastAPI(title="inventory")
log = setup(app, "inventory")

RESTOCK_THRESHOLD = 10
RESTOCK_LEVEL = 100

PRODUCTS = {
    "SKU-001": {"name": "Mechanical Keyboard", "price": 89.00, "stock": RESTOCK_LEVEL},
    "SKU-002": {"name": "Wireless Mouse", "price": 29.50, "stock": RESTOCK_LEVEL},
    "SKU-003": {"name": "27in Monitor", "price": 249.99, "stock": RESTOCK_LEVEL},
    "SKU-004": {"name": "USB-C Hub", "price": 39.00, "stock": RESTOCK_LEVEL},
    "SKU-005": {"name": "Laptop Stand", "price": 45.00, "stock": RESTOCK_LEVEL},
    "SKU-006": {"name": "Noise-Cancelling Headphones", "price": 199.00, "stock": RESTOCK_LEVEL},
}


class StockChange(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=10)


@app.get("/products")
async def list_products():
    return [{"sku": sku, **product} for sku, product in PRODUCTS.items()]


@app.post("/reserve")
async def reserve(change: StockChange):
    product = PRODUCTS.get(change.sku)
    if product is None:
        return JSONResponse({"error": "unknown sku"}, status_code=404)
    if product["stock"] < change.qty:
        log.warning("insufficient stock", sku=change.sku, requested=change.qty, available=product["stock"])
        return JSONResponse({"error": "out of stock"}, status_code=409)

    product["stock"] -= change.qty
    if product["stock"] < RESTOCK_THRESHOLD:
        # Simulated supplier delivery so the lab never runs dry.
        product["stock"] = RESTOCK_LEVEL
        log.info("restocked", sku=change.sku, stock=RESTOCK_LEVEL)
    return {"sku": change.sku, "qty": change.qty, "unit_price": product["price"]}


@app.post("/release")
async def release(change: StockChange):
    product = PRODUCTS.get(change.sku)
    if product is None:
        return JSONResponse({"error": "unknown sku"}, status_code=404)
    product["stock"] += change.qty
    return {"sku": change.sku, "stock": product["stock"]}
