import logging
import os
import secrets
import time

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

access_log = logging.getLogger("scanbrain.access")

SERVICE_KEY = os.getenv("SERVICE_SECRET_KEY", "")
RATE_LIMIT = os.getenv("RATE_LIMIT", "30/minute")

limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT])


def verify_service_key(request: Request) -> None:
    if not SERVICE_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SERVICE_SECRET_KEY is not configured on the server.",
        )
    incoming = request.headers.get("X-Service-Key", "")
    if not secrets.compare_digest(incoming.encode(), SERVICE_KEY.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service key.",
        )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            access_log.exception(
                "request_failed method=%s path=%s elapsed_ms=%.1f ip=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                get_remote_address(request),
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        access_log.info(
            "request method=%s path=%s status=%d elapsed_ms=%.1f ip=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            get_remote_address(request),
        )
        return response
