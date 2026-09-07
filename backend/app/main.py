import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from . import db
from . import graph_actions
from . import recovery
from .logging_config import configure_logging
from .rate_limit import AuthFailureLockoutMiddleware
from .routes import admin, jobs
from .security_headers import SecurityHeadersMiddleware

configure_logging()
logger = logging.getLogger("app")

# How often the periodic sweep runs. A startup-only sweep would only ever
# catch problems left behind by THIS process's own restart - it would
# never notice a connector dying while the backend keeps running for
# days, which is the more likely real-world case. 5 minutes is frequent
# enough that nothing sits abandoned for long, without hammering the
# database.
SWEEP_INTERVAL_SECONDS = 300


def _run_sweep_once() -> None:
    with Session(db.engine) as session:
        reset_ids = recovery.reset_abandoned_claims(session)
        stuck_job_ids = graph_actions.find_interrupted_jobs(session)

    if reset_ids:
        logger.warning("Reset %d abandoned claim(s): %s", len(reset_ids), reset_ids)
    for job_id in stuck_job_ids:
        asyncio.create_task(run_in_threadpool(graph_actions.run_cloud_steps, job_id))
    if stuck_job_ids:
        logger.warning("Resumed %d job(s) left mid-flight: %s", len(stuck_job_ids), stuck_job_ids)


async def _periodic_sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await run_in_threadpool(_run_sweep_once)
        except Exception:
            # A failed sweep attempt should never crash the loop - it just
            # tries again next interval.
            logger.exception("Periodic sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # Run once immediately at startup (catches anything left behind by a
    # previous restart of this same process)...
    _run_sweep_once()
    # ...then keep running periodically for the life of the process (catches
    # a connector dying at any point while the backend itself stays up).
    sweep_task = asyncio.create_task(_periodic_sweep_loop())

    yield

    sweep_task.cancel()


app = FastAPI(title="Hybrid Joiner Automation Backend", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthFailureLockoutMiddleware)
app.include_router(admin.router)
app.include_router(jobs.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Logged with a full traceback so an operator can actually diagnose
    # it (Azure App Service's Log Stream captures this), while the caller
    # gets a generic message - never the exception's own text, which could
    # leak internal details (a stack trace, a query, a path).
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health():
    return {"status": "ok"}
