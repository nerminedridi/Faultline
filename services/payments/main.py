"""Payments: simulated card processor with realistic latency and declines."""

import asyncio
import os
import random
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import chaos
from common.observability import setup

app = FastAPI(title="payments")
log = setup(app, "payments")

DECLINE_RATE = float(os.getenv("DECLINE_RATE", "0.03"))
chaos.tunable("decline_rate")


class Charge(BaseModel):
    order_id: int
    amount: float = Field(gt=0)


@app.post("/charge")
async def charge(req: Charge):
    await asyncio.sleep(random.uniform(0.02, 0.08))  # talking to the "bank"
    if random.random() < chaos.param("decline_rate", DECLINE_RATE):
        log.info("card declined", order_id=req.order_id, amount=req.amount)
        return JSONResponse({"status": "declined"}, status_code=402)
    return {"status": "approved", "transaction_id": uuid.uuid4().hex, "amount": req.amount}
