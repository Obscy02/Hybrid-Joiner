"""
Failed-auth lockout: after too many 401s from one source IP in a short
window, that IP gets 429'd for a cool-down period regardless of what
credential it tries next. Closes the "nothing stops repeated guesses
against the dashboard token or an API key" gap.

In-memory, per-process - consistent with the single-instance assumption
documented everywhere else in this project (find_interrupted_jobs,
DEPLOY_AZURE.md's B1 App Service plan). A scaled-out deployment would need
a shared store (e.g. Redis) for this to actually work across instances;
here it would just mean each instance tracks failures independently,
which is a weaker guarantee, not a broken one.

State lives at module level, not on the middleware instance: Starlette
builds and caches the middleware stack lazily on the app object, so tests
sharing one `app` singleton across many TestClient instances would
otherwise all share one un-resettable instance's state - module globals
let tests reset between runs (see tests/conftest.py's autouse fixture).
"""
import logging
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("app.security")

MAX_FAILURES = 10
WINDOW_SECONDS = 300
LOCKOUT_SECONDS = 900

_failures: dict = defaultdict(list)
_locked_until: dict = {}


def reset_state() -> None:
    """Test-only hook - see the autouse fixture in tests/conftest.py."""
    _failures.clear()
    _locked_until.clear()


def _client_key(request: Request) -> str:
    # Azure App Service (and most reverse proxies) put the real origin IP
    # in X-Forwarded-For, not the TCP-level peer address - that's the
    # platform's own front-end, the same for every request. Trusting this
    # header is reasonable specifically because App Service sets/appends
    # to it itself; a deployment sitting directly on the internet with no
    # proxy in front would need to ignore client-supplied values instead.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthFailureLockoutMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = _client_key(request)
        now = time.monotonic()

        locked_until = _locked_until.get(key)
        if locked_until and now < locked_until:
            logger.warning("Rejected request from locked-out source %s (%s %s)", key, request.method, request.url.path)
            return JSONResponse(
                {"detail": "Too many failed auth attempts - try again later."}, status_code=429
            )

        response = await call_next(request)

        if response.status_code == 401:
            recent = [t for t in _failures[key] if now - t < WINDOW_SECONDS]
            recent.append(now)
            _failures[key] = recent
            if len(recent) >= MAX_FAILURES:
                _locked_until[key] = now + LOCKOUT_SECONDS
                _failures[key] = []
                logger.warning(
                    "Locking out %s for %ds after %d failed auth attempts in %ds",
                    key, LOCKOUT_SECONDS, MAX_FAILURES, WINDOW_SECONDS,
                )
        elif response.status_code < 400:
            # A successful request clears the count - only sustained
            # failures should ever trigger a lockout.
            _failures.pop(key, None)

        return response
