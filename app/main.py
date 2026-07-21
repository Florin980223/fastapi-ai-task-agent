"""FastAPI application entry point.

Run with: uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config, database
from app.database import init_db
from app.logging_config import configure_logging
from app.middleware import MaxBodySizeMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from app.routes import agent, integrations, tasks
from app.services import readiness

configure_logging()
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify-only - see app.database.init_db() and
    # app.services.schema_migration.ensure_schema_is_current(). Never
    # wrapped in a try/except here: an invalid schema must still raise
    # uncaught and prevent the ASGI app from ever starting to serve
    # requests at all - there is no partially-running process where
    # /health would answer 200 in that case. See docs/DEPLOYMENT.md's
    # "Health vs. readiness" section for the precise startup-time-failure
    # vs. post-startup-failure distinction.
    init_db()
    logger.info("startup complete - schema verified, ready to accept traffic")
    yield
    # database.engine (not a name imported at module load time) so this
    # picks up whatever engine is actually live at shutdown time, same
    # reasoning as app.services.readiness's own lookup - see that
    # module's docstring.
    database.engine.dispose()
    readiness.shutdown()
    logger.info("shutdown complete")


app = FastAPI(title="FastAPI AI Task Agent", lifespan=lifespan)

# add_middleware() prepends, so the stack ends up with the LAST one
# added as the OUTERMOST layer - SecurityHeadersMiddleware must be added
# last so it wraps everything, including a 413 from MaxBodySizeMiddleware
# and a 500 from RequestContextMiddleware, with the same security
# headers on every response. See app/middleware.py's own docstring.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.MAX_REQUEST_BODY_BYTES)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(tasks.router)
app.include_router(integrations.router)
app.include_router(agent.router)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net - NOT the primary handler for an unhandled
    exception raised while executing a request. That's
    RequestContextMiddleware.dispatch (app/middleware.py), which has to
    handle it itself: Starlette treats a handler registered for the
    bare Exception class specially, wiring it into ServerErrorMiddleware,
    the true outermost layer that sits OUTSIDE every add_middleware()
    layer above - by the time an exception reaches this function,
    RequestContextMiddleware's own request_id_var has already been
    reset and its response already returned, so this handler could
    neither tag its log line with the right request_id nor attach an
    X-Request-ID header to the client-facing response. This only ever
    fires for a bug in RequestContextMiddleware's own code outside its
    try block - it should never normally be reached.

    Same contract regardless: never exposes the exception text, a
    traceback, or any DB/internal detail to the client - only ever this
    fixed, generic body. The real exception is logged server-side only.
    """
    logger.exception("Unhandled exception while processing %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/ready")
def readiness_check(response: Response):
    """Liveness (/health above) only proves the process is alive and the
    event loop is responsive - it never touches the database. This is
    the separate readiness signal: verifies the database is reachable,
    the schema is at the expected Alembic revision, and required
    configuration is available, all within a small bounded timeout, and
    never modifies the database. No auth (matches /health's precedent -
    an orchestrator/load balancer probe typically can't supply an
    API key) and not rate-limited.

    Only ever returns a fixed, safe "reason" string on failure - never a
    database URL, a credential, or raw exception text. See
    app/services/readiness.py and docs/DEPLOYMENT.md's "Health vs.
    readiness" section for the exact semantics, including how this
    differs from a startup-time schema failure (which prevents the app
    from starting at all, rather than answering here).
    """
    result = readiness.check_ready()
    if result.ready:
        return {"status": "ready"}
    response.status_code = 503
    return {"status": "not_ready", "reason": result.reason}


# Mounted last, deliberately: every route above (this file's own /health,
# every router's endpoints, and FastAPI's own built-in /docs, /redoc,
# /openapi.json registered inside FastAPI(...) before any of this module's
# code even runs) is matched first by Starlette's in-order routing - this
# catch-all only ever sees a path nothing else claimed. html=True serves
# app/static/index.html for "/"; it does NOT invent an index.html fallback
# for arbitrary unmatched deep paths (e.g. a typo'd API path still 404s -
# see tests/test_web_ui.py).
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
