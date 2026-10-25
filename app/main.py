import logging
import random
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import SessionLocal, Task, engine, get_session, init_db
from app.otel import setup_telemetry

setup_telemetry()
LoggingInstrumentor().instrument(set_logging_format=False)

log = logging.getLogger("taskapi")
tracer = trace.get_tracer("taskapi")
meter = metrics.get_meter("taskapi")

# --- custom application metrics ---------------------------------------------
tasks_created = meter.create_counter(
    "tasks_created", unit="1", description="Tasks created"
)
tasks_completed = meter.create_counter(
    "tasks_completed", unit="1", description="Tasks marked as done"
)
tasks_open = meter.create_up_down_counter(
    "tasks_open", unit="1", description="Tasks that are not done yet"
)
op_duration = meter.create_histogram(
    "task_operation_duration",
    unit="s",
    description="Time spent in task handlers",
)
errors = meter.create_counter(
    "app_errors", unit="1", description="Handled application errors"
)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class TaskOut(BaseModel):
    id: int
    title: str
    done: bool

    class Config:
        from_attributes = True


def wait_for_db(attempts: int = 30) -> None:
    for i in range(attempts):
        try:
            with engine.connect():
                return
        except OperationalError:
            log.warning("database not ready yet (attempt %s/%s)", i + 1, attempts)
            time.sleep(1)
    raise RuntimeError("database never became reachable")


def count_open_tasks() -> int:
    session = SessionLocal()
    try:
        return session.query(Task).filter(Task.done.is_(False)).count()
    finally:
        session.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_db()
    init_db()
    # seed the gauge so it survives a restart with rows already in the table
    open_tasks = count_open_tasks()
    tasks_open.add(open_tasks)
    log.info("taskapi started, %s open tasks in the backlog", open_tasks)
    yield
    log.info("taskapi shutting down")


app = FastAPI(title="Task API", version="1.0.0", lifespan=lifespan)

SQLAlchemyInstrumentor().instrument(engine=engine)
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)):
    started = time.perf_counter()
    with tracer.start_as_current_span("list-tasks") as span:
        rows = session.query(Task).order_by(Task.id.desc()).limit(50).all()
        span.set_attribute("tasks.returned", len(rows))
    op_duration.record(time.perf_counter() - started, {"operation": "list"})
    log.info("listed %s tasks", len(rows))
    return rows


@app.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(payload: TaskIn, session: Session = Depends(get_session)):
    started = time.perf_counter()
    with tracer.start_as_current_span("create-task") as span:
        span.set_attribute("task.title", payload.title)
        task = Task(title=payload.title)
        session.add(task)
        session.commit()
        session.refresh(task)
        span.set_attribute("task.id", task.id)

    tasks_created.add(1)
    tasks_open.add(1)
    op_duration.record(time.perf_counter() - started, {"operation": "create"})
    log.info("created task id=%s title=%r", task.id, task.title)
    return task


@app.post("/tasks/{task_id}/complete", response_model=TaskOut)
def complete_task(task_id: int, session: Session = Depends(get_session)):
    started = time.perf_counter()
    task = session.get(Task, task_id)
    if task is None:
        errors.add(1, {"reason": "task_not_found"})
        log.warning("cannot complete task id=%s: not found", task_id)
        raise HTTPException(status_code=404, detail="task not found")

    if not task.done:
        task.done = True
        session.commit()
        session.refresh(task)
        tasks_completed.add(1)
        tasks_open.add(-1)
        log.info("completed task id=%s", task_id)

    op_duration.record(time.perf_counter() - started, {"operation": "complete"})
    return task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, session: Session = Depends(get_session)):
    task = session.get(Task, task_id)
    if task is None:
        errors.add(1, {"reason": "task_not_found"})
        raise HTTPException(status_code=404, detail="task not found")
    if not task.done:
        tasks_open.add(-1)
    session.delete(task)
    session.commit()
    log.info("deleted task id=%s", task_id)


@app.get("/work")
def do_work():
    """Fake multi-step job, so the traces in Jaeger have some depth."""
    started = time.perf_counter()
    with tracer.start_as_current_span("pipeline") as parent:
        parent.set_attribute("pipeline.steps", 3)
        for step in ("fetch", "transform", "store"):
            with tracer.start_as_current_span(f"step-{step}") as span:
                delay = random.uniform(0.01, 0.12)
                span.set_attribute("step.name", step)
                span.set_attribute("step.sleep_seconds", round(delay, 3))
                time.sleep(delay)
    took = time.perf_counter() - started
    op_duration.record(took, {"operation": "work"})
    log.info("pipeline finished in %.3fs", took)
    return {"took_seconds": round(took, 3)}


@app.get("/boom")
def boom():
    """Deliberate failure, used in the demo to show errors in all three signals."""
    errors.add(1, {"reason": "deliberate"})
    log.error("boom endpoint called, raising on purpose")
    span = trace.get_current_span()
    exc = RuntimeError("deliberate failure for the observability demo")
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
    raise HTTPException(status_code=500, detail=str(exc))
