import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Any, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from supabase import Client, create_client

from app.middleware.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
API_KEY_PREFIX = "sb_"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_supabase: Optional[Client] = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Supabase credentials are not configured.",
            )
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    created_at: datetime


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Friendly label for this key.")


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key: str
    created_at: datetime


def _hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def _make_access_token(user_id: str, email: str) -> Tuple[str, int]:
    if not JWT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET_KEY is not configured.",
        )
    expires_in = JWT_EXPIRE_MINUTES * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "type": "access",
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expires_in


def _generate_api_key() -> Tuple[str, str]:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, sha256(raw.encode()).hexdigest()


async def _fetch_user_by_email(email: str) -> Optional[dict]:
    db = _get_supabase()
    try:
        res = await run_in_threadpool(
            lambda: db.table("users").select("*").eq("email", email).limit(1).execute()
        )
    except Exception as exc:
        logger.exception("Supabase user fetch failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database error: {exc}",
        )
    return res.data[0] if res.data else None


async def _insert_user(email: str, password_hash: str) -> dict:
    db = _get_supabase()
    try:
        res = await run_in_threadpool(
            lambda: db.table("users")
            .insert({"email": email, "password_hash": password_hash})
            .execute()
        )
    except Exception as exc:
        logger.exception("Supabase user insert failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database error: {exc}",
        )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user.",
        )
    return res.data[0]


async def _insert_api_key(user_id: str, name: str, key_hash: str) -> dict:
    db = _get_supabase()
    try:
        res = await run_in_threadpool(
            lambda: db.table("api_keys")
            .insert({"user_id": user_id, "name": name, "key_hash": key_hash})
            .execute()
        )
    except Exception as exc:
        logger.exception("Supabase api_key insert failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database error: {exc}",
        )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create API key.",
        )
    return res.data[0]


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user account.",
)
async def register(payload: RegisterRequest) -> UserResponse:
    if await _fetch_user_by_email(payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )
    user = await _insert_user(payload.email, _hash_password(payload.password))
    return UserResponse(
        id=str(user["id"]),
        email=user["email"],
        created_at=user["created_at"],
    )


@router.post(
    "/login",
    summary="Exchange email + password for a JWT access token.",
)
async def login(payload: LoginRequest) -> TokenResponse:
    user = await _fetch_user_by_email(payload.email)
    if not user or not _verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = _make_access_token(str(user["id"]), user["email"])
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/api-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new API key for the authenticated user. Plaintext is shown only once.",
)
async def create_api_key(
    payload: ApiKeyRequest,
    user: Annotated[Any, Depends(get_current_user)],
) -> ApiKeyResponse:
    user_id = getattr(user, "id", None)
    if user_id is None and isinstance(user, dict):
        user_id = user.get("id") or user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not resolve user id from token.",
        )

    plain_key, key_hash = _generate_api_key()
    record = await _insert_api_key(str(user_id), payload.name, key_hash)
    return ApiKeyResponse(
        id=str(record["id"]),
        name=record["name"],
        key=plain_key,
        created_at=record["created_at"],
    )
