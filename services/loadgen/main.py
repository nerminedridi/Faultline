"""Load generator: simulated customers browsing, checking out and tracking orders."""

import asyncio
import os
import random
import time
from collections import Counter, deque

import httpx

from common.observability import configure_logging

GATEWAY_URL = os.environ["GATEWAY_URL"]
RPS = float(os.getenv("RPS", "5"))
REPORT_EVERY = 30  # seconds

log = configure_logging("loadgen")
SKUS = [f"SKU-00{i}" for i in range(1, 7)]
recent_orders: deque[int] = deque(maxlen=200)
results: Counter[str] = Counter()


async def one_request(client: httpx.AsyncClient) -> None:
    roll = random.random()
    try:
        if roll < 0.5:
            action = "browse"
            resp = await client.get(f"{GATEWAY_URL}/api/products")
        elif roll < 0.9 or not recent_orders:
            action = "checkout"
            resp = await client.post(
                f"{GATEWAY_URL}/api/checkout",
                json={"sku": random.choice(SKUS), "qty": random.randint(1, 3)},
            )
            if resp.status_code == 200:
                recent_orders.append(resp.json()["order_id"])
        else:
            action = "track"
            resp = await client.get(f"{GATEWAY_URL}/api/orders/{random.choice(recent_orders)}")
        results[f"{action}:{resp.status_code}"] += 1
    except httpx.HTTPError as exc:
        results[f"error:{type(exc).__name__}"] += 1


async def main() -> None:
    log.info("load generator starting", target=GATEWAY_URL, rps=RPS)
    pending: set[asyncio.Task] = set()
    last_report = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            # Fire-and-forget so one slow request doesn't slow the whole load.
            task = asyncio.create_task(one_request(client))
            pending.add(task)
            task.add_done_callback(pending.discard)

            if time.monotonic() - last_report >= REPORT_EVERY:
                log.info("traffic summary", window_s=REPORT_EVERY, in_flight=len(pending), **dict(results))
                results.clear()
                last_report = time.monotonic()
            await asyncio.sleep(random.expovariate(RPS))


if __name__ == "__main__":
    asyncio.run(main())
