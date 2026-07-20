"""Request-correlation, access logging, request-body-size limiting, and
response security headers.

Three independent middlewares, added in app/main.py as:

    app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.MAX_REQUEST_BODY_BYTES)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

Starlette's add_middleware() prepends, and the stack is built so the
LAST one added ends up OUTERMOST (runs first on the way in, last on the
way out) - so SecurityHeadersMiddleware wraps everything (including a
413 from MaxBodySizeMiddleware and a 500 from RequestContextMiddleware),
guaranteeing every response - JSON API or static UI file - carries the
same security headers. RequestContextMiddleware sits just inside it,
tagging every response with a request id and one access-log line.
MaxBodySizeMiddleware sits innermost, rejecting oversized bodies before
auth or routing ever run.

RequestContextMiddleware also handles a genuinely unhandled exception
itself, rather than only relying on app.main's
@app.exception_handler(Exception). This isn't redundant: Starlette
treats a handler registered for the bare Exception class (or status
code 500) specially - it becomes the handler for ServerErrorMiddleware,
which is the true outermost layer, sitting OUTSIDE this entire
add_middleware() stack (see Starlette's Router.build_middleware_stack).
An exception with no more specific handler propagates all the way up
through call_next() before ever reaching it - past this point,
request_id_var has already been reset back to its default in the
finally block below, and the response ServerErrorMiddleware builds
never passes back through this middleware's own header-setting code,
so it would carry neither the right request_id in its log line nor an
X-Request-ID response header. Handling it here instead - inside the
same try that still has the real request_id in scope - is what makes
both guarantees hold for a 500 exactly like they do for every other
status code. app.main's handler stays registered too, as a last-resort
safety net for a bug in this middleware's own code that isn't inside
this try block - it should never normally be reached.
"""

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logging_config import request_id_var

logger = logging.getLogger(__name__)

# Deliberately conservative: opaque correlation ids only (uuids,
# ulids, short hex/alnum tokens) - never newlines, quotes, or other
# characters that could inject a fake extra "line" into a key=value log
# or otherwise be used to smuggle data through a header.
_SAFE_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _resolve_request_id(client_supplied: str | None) -> str:
    """A client-supplied X-Request-ID is only ever reused if it fullmatches
    the safe pattern above - fullmatch (not match/search), so a value
    like "safe-id\\nInjected: header" can never sneak through just
    because it starts with something that looks safe. Anything absent
    or unsafe gets a freshly generated id instead; an unsafe value is
    never echoed back and never reaches request_id_var (so it can never
    end up in a log line either).
    """
    if client_supplied is not None and _SAFE_REQUEST_ID_PATTERN.fullmatch(client_supplied):
        return client_supplied
    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns/accepts a request id, times the request, tags every log
    line emitted during it (via the request_id_var contextvar - see
    app/logging_config.py), and emits one access-log line per request.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = _resolve_request_id(request.headers.get("X-Request-ID"))
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # See this module's docstring: this is the ONLY place
                # that reliably has both the real request_id and a
                # chance to attach the X-Request-ID header for a
                # genuinely unhandled exception. Never includes the
                # exception text, a traceback, or any request/response
                # detail in the client-facing body - only ever this
                # fixed, generic one. The real exception (with
                # traceback) is logged server-side only.
                logger.exception(
                    "Unhandled exception while processing %s %s", request.method, request.url.path
                )
                response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

            duration_ms = int((time.monotonic() - started) * 1000)
            response.headers["X-Request-ID"] = request_id
            # user_id is only ever set on request.state by
            # get_current_user (app/services/auth.py), and only after a
            # successful authentication - an unauthenticated or
            # invalid-key request logs user_id=- here, never the
            # attempted key (that value never reaches request.state or
            # any log call anywhere).
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "user_id": getattr(request.state, "user_id", "-"),
                },
            )
            return response
        finally:
            # Always runs - on the normal return above, or if something
            # truly unexpected propagates past even the except block
            # above (a bug in this middleware's own code, not the
            # request it's handling) - this must never leak
            # request_id_var into whatever request this worker
            # thread/task handles next regardless.
            request_id_var.reset(token)


class MaxBodySizeMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware): rejects an oversized request
    body with a 413 before routing/auth/parsing ever run, and - the
    reason this is hand-rolled ASGI instead of the simpler
    `len(await request.body()) > max_bytes` - without ever buffering an
    arbitrarily large body first.

    Content-Length is checked up front when present: a well-behaved
    oversized client is rejected immediately, with zero bytes of body
    ever read. For the missing/understated-Content-Length case
    (chunked transfer, or a lying header), receive() is wrapped to
    count bytes incrementally as they arrive; as soon as the running
    total crosses max_bytes, a 413 is sent and no further body is read
    - the total held in memory at that point is bounded by max_bytes
    plus at most one chunk, never by however much more the client
    intended to send.

    A body that stays within the limit is buffered in full (bounded by
    max_bytes, by definition) and replayed to the downstream app via a
    receive() that yields the exact same sequence of messages the
    original receive() would have - the route sees an unmodified body.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length_raw = headers.get(b"content-length")
        if content_length_raw is not None:
            try:
                declared_length = int(content_length_raw)
            except ValueError:
                # Malformed Content-Length: fail closed with the same
                # generic 413 rather than trusting/forwarding a header
                # value we can't parse, and never echo it back.
                await self._reject(send)
                return
            if declared_length > self.max_bytes:
                await self._reject(send)
                return

        buffered: list[dict] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(send)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break
            else:
                buffered.append(message)
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send) -> None:
        body = b'{"detail":"Request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Swagger UI and ReDoc's default HTML (FastAPI's own /docs and /redoc, plus
# the /openapi.json they both fetch) load their JS/CSS from a CDN - existing,
# pre-existing behavior this project doesn't control and must keep working.
# These three paths are the ONLY exemption from the strict CSP below - not a
# general relaxation, just an acknowledgment that this specific vendored page
# already depends on an external source.
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

# Strict by construction: no 'unsafe-inline', no 'unsafe-eval', no CDN or any
# other external origin anywhere. The UI this protects (app/static/) never
# uses an inline <script>/<style> or an onclick=... attribute, so it never
# needs an exception either.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds a fixed set of response security headers to every response -
    JSON API responses and the static UI alike. Purely additive: never
    changes a status code or response body, so it can't weaken any
    existing API behavior.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path not in _CSP_EXEMPT_PATHS:
            response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        return response
