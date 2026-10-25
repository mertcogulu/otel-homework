# OpenTelemetry Homework — Task API Observability Stack

A small FastAPI + PostgreSQL service instrumented with OpenTelemetry, and the full
observability stack around it. All three signals leave the application over a single
OTLP connection to an OpenTelemetry Collector, which fans them out to a dedicated
backend each:

```
                           ┌──────────────┐  traces   ┌──────────┐
                           │              ├──────────►│  Jaeger  │
  ┌──────────┐    OTLP     │  OpenTelemetry           └──────────┘
  │ taskapi  ├────────────►│  Collector   │  metrics  ┌────────────┐   ┌─────────┐
  │ (FastAPI)│   gRPC:4317 │              ├──────────►│ Prometheus ├──►│         │
  └────┬─────┘             │              │  (scrape) └────────────┘   │ Grafana │
       │                   │              │  logs     ┌──────────┐     │         │
  ┌────▼─────┐             │              ├──────────►│   Loki   ├────►│         │
  │ Postgres │             └──────────────┘           └──────────┘     └─────────┘
  └──────────┘
```

* **Metrics** → OTel Collector (Prometheus exporter) → **Prometheus** → **Grafana**
* **Logs** → OTel Collector (OTLP/HTTP) → **Loki** → **Grafana**
* **Traces** → OTel Collector → **Jaeger** (and Grafana's Jaeger datasource)

Everything is packaged with Docker Compose.

## Running it

```bash
docker compose up -d --build
```

Give it ~20 seconds, then generate some traffic so the dashboards are not empty:

```bash
./scripts/load.sh 120
```

| What | URL | Notes |
|---|---|---|
| Task API (Swagger UI) | http://localhost:8000/docs | |
| Grafana | http://localhost:3000 | `admin` / `admin`, anonymous access is on too |
| Prometheus | http://localhost:9090 | |
| Jaeger UI | http://localhost:16686 | pick service `taskapi` |
| Loki | http://localhost:3100 | queried through Grafana |
| Collector metrics endpoint | http://localhost:8889/metrics | what Prometheus scrapes |

The Grafana dashboard (**Task API — Observability**) and all three datasources are
provisioned automatically, so there is nothing to click through after startup.

Tear down with `docker compose down -v`.

## The application

A deliberately small task list API, so the telemetry stays the interesting part.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | liveness, excluded from tracing |
| `GET` | `/tasks` | list the 50 newest tasks |
| `POST` | `/tasks` | create a task |
| `POST` | `/tasks/{id}/complete` | mark a task done |
| `DELETE` | `/tasks/{id}` | delete a task |
| `GET` | `/work` | fake 3-step pipeline, produces nested spans |
| `GET` | `/boom` | always fails, for demoing errors across all three signals |

## What is instrumented

**Auto-instrumentation** gives the HTTP server spans and metrics (FastAPI) and a span
per SQL statement (SQLAlchemy), so a request trace already shows the database work
underneath it.

**Manual instrumentation** on top of that:

*Custom metrics (OpenTelemetry metrics API, `app/main.py`)*

| Metric | Type | Meaning |
|---|---|---|
| `tasks_created_total` | counter | tasks created |
| `tasks_completed_total` | counter | tasks marked as done |
| `tasks_open` | up/down counter | tasks still open (seeded from the DB at startup) |
| `task_operation_duration_seconds` | histogram | time spent in each handler, labelled by `operation` |
| `app_errors_total` | counter | handled errors, labelled by `reason` |

Instrument names are unit-less on purpose — the collector's Prometheus exporter appends
`_total` / `_seconds` itself, following the OpenTelemetry naming convention. The handler
histogram gets explicit second-scale bucket boundaries through a `View` in `app/otel.py`;
with the SDK defaults (tuned for milliseconds) every observation would land in the first
bucket and the quantiles would be meaningless.

*Custom spans* — `list-tasks`, `create-task` and a `pipeline` span with a child span per
step in `/work`, with attributes like `tasks.returned`, `task.id` and `step.sleep_seconds`.
`/boom` records the exception on the span and sets the span status to `ERROR`.

*Logs* — standard-library `logging` records are shipped over OTLP by the SDK's
`LoggingHandler`, while still going to stdout for `docker compose logs`. Thanks to the
logging instrumentation every record carries the active `trace_id` / `span_id`, which the
collector forwards to Loki as structured metadata.

## Correlating the three signals

This is the part worth showing live:

1. A log line in the Grafana **Logs** panel has a `TraceID` field → clicking it opens that
   exact trace in Jaeger.
2. From a span in Jaeger, **Logs for this span** goes back to the matching Loki lines
   (configured via `tracesToLogsV2`).
3. The same request shows up in the metric panels — e.g. hitting `/boom` moves the error
   ratio stat, the `app_errors_total{reason="deliberate"}` panel, and produces a red trace
   in Jaeger, all at once.

## Dashboard panels

1. Open tasks, requests/s, error ratio and overall p95 latency as single stats
2. Request rate by endpoint and method
3. p95 request latency by endpoint
4. Tasks created vs completed per minute
5. Application errors by reason
6. Task handler duration by operation (p50 and p95)
7. Live application logs from Loki with the trace link

## Layout

```
app/main.py                 API handlers, custom metrics and spans
app/otel.py                 OTLP exporters for traces/metrics/logs + histogram view
app/db.py                   SQLAlchemy model and session handling
otel/collector-config.yaml  receivers, processors and the three export pipelines
prometheus/prometheus.yml   scrape config (collector + collector self-metrics)
loki/loki-config.yaml       single-binary Loki with filesystem storage
grafana/provisioning/       datasources and the dashboard provider
grafana/dashboards/         the dashboard JSON
scripts/load.sh             traffic generator for the demo
docker-compose.yml          the whole stack
```

## A note on the security settings

This stack is built to run on a laptop and be demoed in class, and a few settings
are deliberately relaxed for that. They are fine on `localhost` and would all need
changing before anything like this went further:

* **Grafana** ships with the default `admin` / `admin` login and anonymous access
  enabled, so the demo needs no sign-in. The anonymous role is `Editor` rather than
  `Admin` — the lowest role that can still follow a log line's trace link into
  Explore. For any real deployment: anonymous off (or `Viewer`), and a real password
  from a secret rather than the compose file.
* **Loki** runs with `auth_enabled: false`, which is the normal single-tenant setup
  and the reason no `X-Scope-OrgID` header is needed anywhere. Port 3100 is published
  only so the API can be poked with `curl` during the demo; Grafana and the collector
  reach it over the internal Docker network and would not need it exposed.
* **Task titles are written into span attributes and log lines** (`task.title`). That
  is intentional here — it is what makes the spans worth looking at — but in a real
  service, free-text user input does not belong in telemetry that gets shipped to a
  logging backend and kept around. There you would log the id only, or redact the
  title first.
