"""FastAPI application entry point.

Run with: uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config
from app.database import init_db
from app.logging_config import configure_logging
from app.middleware import MaxBodySizeMiddleware, RequestContextMiddleware
from app.routes import agent, integrations, tasks

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="FastAPI AI Task Agent", lifespan=lifespan)

# add_middleware() prepends, so the stack ends up with the LAST one
# added as the OUTERMOST layer - RequestContextMiddleware must be added
# last so it wraps everything, including a 413 from
# MaxBodySizeMiddleware, with a request id and one access-log line. See
# app/middleware.py's own docstring.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.MAX_REQUEST_BODY_BYTES)
app.add_middleware(RequestContextMiddleware)

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
