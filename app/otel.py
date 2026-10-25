"""OpenTelemetry wiring: traces, metrics and logs all go out over OTLP
to the collector, which fans them out to Jaeger / Prometheus / Loki."""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "taskapi")
ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s - %(message)s"
)


def setup_telemetry() -> None:
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": os.getenv("SERVICE_VERSION", "1.0.0"),
            "deployment.environment": os.getenv("ENVIRONMENT", "dev"),
        }
    )

    # --- traces ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    # --- metrics ---
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=ENDPOINT, insecure=True),
        export_interval_millis=5000,
    )
    # the default buckets are tuned for milliseconds; our handler histogram is in
    # seconds, so without this view every observation lands in the first bucket
    seconds_buckets = View(
        instrument_name="task_operation_duration",
        aggregation=ExplicitBucketHistogramAggregation(
            [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
        ),
    )
    metrics.set_meter_provider(
        MeterProvider(
            resource=resource, metric_readers=[reader], views=[seconds_buckets]
        )
    )

    # --- logs ---
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=ENDPOINT, insecure=True))
    )
    set_logger_provider(logger_provider)

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # keep stdout logs for `docker compose logs`, and ship the same records to Loki
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
