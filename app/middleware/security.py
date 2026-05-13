import logging
import os
import time
from hashlib import sha256
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger("scanbrain.security")
access_log = logging.getLogger("scanbrain.access")

JWT_SECRET = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
RATE_LIMIT = os.getenv("RATE_LIMIT", "30/minute")
API_KEY_HEADER = "X-API-Key"

bearer_scheme = HTTPBearer(auto_error=False)
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


class AuthenticatedUser:
    def __init__(self, id: str, email: Optional[str] = None, auth_type: str = "jwt") -> None:
        self.id = id
        self.email = email
        self.auth_type = auth_type

    def __repr__(self) -> str:
        return f"AuthenticatedUser(id={self.id!r}, auth_type={self.auth_type!r})"


def _decode_jwt(token: str) -> dict:
    if not JWT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET_KEY is not configured.",
        )
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _verify_api_key(api_key: str) -> AuthenticatedUser:
    # Lazy import to avoid a circular dependency (auth.py imports this module).
    from app.routes.auth import _get_supabase

    key_hash = sha256(api_key.encode()).hexdigest()
    db = _get_supabase()
    try:
        res = await run_in_threadpool(
            lambda: db.table("api_keys")
            .select("user_id, users(id, email)")
            .eq("key_hash", key_hash)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("API key lookup failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database error: {exc}",
        )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    row = res.data[0]
    user_block = row.get("users") or {}
    return AuthenticatedUser(
        id=str(row["user_id"]),
        email=user_block.get("email"),
        auth_type="api_key",
    )


async def get_current_user(
    bearer: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    api_key: Annotated[Optional[str], Depends(api_key_scheme)],
) -> AuthenticatedUser:
    if bearer is not None:
        payload = _decode_jwt(bearer.credentials)
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token is missing a subject.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AuthenticatedUser(
            id=str(sub),
            email=payload.get("email"),
            auth_type="jwt",
        )

    if api_key:
        return await _verify_api_key(api_key)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide a Bearer JWT or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and JWT_SECRET:
        token = auth.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except JWTError:
            pass

    api_key = request.headers.get(API_KEY_HEADER)
    if api_key:
        return f"apikey:{sha256(api_key.encode()).hexdigest()[:16]}"

    return get_remote_address(request)


limiter = Limiter(key_func=_principal_from_request, default_limits=[RATE_LIMIT])


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        principal = _principal_from_request(request)
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            access_log.exception(
                "request_failed method=%s path=%s elapsed_ms=%.1f principal=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                principal,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        access_log.info(
            "request method=%s path=%s status=%d elapsed_ms=%.1f principal=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            principal,
        )
        return response
