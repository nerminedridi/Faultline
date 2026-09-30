# Faultline

**Break it on purpose. Let an AI find out why.**

An AI agent that investigates outages in a live microservice system and identifies the root cause, evaluated against faults injected on purpose, each with a known ground truth.

> **Status:** M1 done: the lab (shop + observability) runs locally. Chaos engine, agent and eval scoreboard are next.

## The lab

A small online shop that runs 24/7 under simulated customer traffic, monitored the way a production system would be.

```
loadgen ──► gateway ──► orders ──► inventory
 (5 req/s)   :8080        │
                          ├──► payments
                          └──► PostgreSQL

every service ──metrics──► Prometheus ──► alert rules
              ──logs────► Alloy ──► Loki
                                       └──► Grafana dashboards
```

| Service | Role |
|---|---|
| `gateway` | Public API: `GET /api/products`, `POST /api/checkout`, `GET /api/orders/{id}` |
| `orders` | Reserves stock → stores the order in Postgres → charges payment; compensates (releases stock) on failure |
| `inventory` | Product catalog and stock reservations |
| `payments` | Simulated card processor: 20–80 ms latency, ~3% declines |
| `loadgen` | Simulated customers: browse 50%, checkout 40%, track order 10% |

### Observability built in
- **Structured JSON logs** from every service, with a `request_id` propagated through the `X-Request-ID` header, so one customer request can be followed across all services in Loki.
- **Prometheus metrics**: `http_requests_total`, `http_request_duration_seconds` (by route/status), and `downstream_errors_total` (by target and kind: timeout / connection / 5xx).
- **Alert rules**: `ServiceDown`, `HighErrorRate` (>5% 5xx), `HighLatency` (p95 > 500 ms).

## Run it

Requires Docker Desktop (running).

```bash
docker compose up -d --build
```

| URL | What |
|---|---|
| http://localhost:3000 | Grafana: *Shop Overview* dashboard |
| http://localhost:9090 | Prometheus: metrics, alerts |
| http://localhost:8080/api/products | The shop's public API |

Try a checkout:

```bash
curl -X POST localhost:8080/api/checkout -H "Content-Type: application/json" -d '{"sku":"SKU-001","qty":1}'
```

Stop everything with `docker compose down` (add `-v` to also wipe the database).
