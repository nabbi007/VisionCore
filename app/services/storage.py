import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

S3_BUCKET = os.getenv("S3_BUCKET_NAME", "")
S3_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or None
S3_PREFIX = os.getenv("S3_PREFIX", "scans")
S3_ACL = os.getenv("S3_ACL") or None
PUBLIC_URL_BASE = os.getenv("S3_PUBLIC_URL_BASE") or None

METADATA_TABLE = os.getenv("SUPABASE_IMAGE_TABLE", "image_metadata")

_s3_client = None


class StoredImage(BaseModel):
    image_id: str
    url: str
    timestamp: datetime
    store_id: Optional[str] = None
    llm_response: Optional[dict] = None
    confirmed_by_user: bool = False
    used_for_training: bool = False


def _get_s3():
    global _s3_client
    if _s3_client is None:
        if not S3_BUCKET:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="S3_BUCKET_NAME is not configured.",
            )
        _s3_client = boto3.client(
            "s3",
            region_name=S3_REGION,
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
        )
    return _s3_client


def _detect_content_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _extension_for(content_type: str) -> str:
    return mimetypes.guess_extension(content_type) or ".bin"


def _build_public_url(s3_key: str) -> str:
    if PUBLIC_URL_BASE:
        return f"{PUBLIC_URL_BASE.rstrip('/')}/{s3_key}"
    if S3_ENDPOINT_URL:
        return f"{S3_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET}/{s3_key}"
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"


def _parse_timestamp(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def _upload_to_s3(image_bytes: bytes, s3_key: str, content_type: str) -> str:
    s3 = _get_s3()
    put_kwargs = {
        "Bucket": S3_BUCKET,
        "Key": s3_key,
        "Body": image_bytes,
        "ContentType": content_type,
    }
    if S3_ACL:
        put_kwargs["ACL"] = S3_ACL
    try:
        await run_in_threadpool(lambda: s3.put_object(**put_kwargs))
    except (BotoCoreError, ClientError) as exc:
        logger.exception("S3 upload failed key=%s", s3_key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"S3 upload failed: {exc}",
        )
    return _build_public_url(s3_key)


async def _save_metadata(record: dict) -> dict:
    # Lazy import to avoid circular dependency (auth.py imports from this package).
    from app.routes.auth import _get_supabase

    db = _get_supabase()
    try:
        res = await run_in_threadpool(
            lambda: db.table(METADATA_TABLE).insert(record).execute()
        )
    except Exception as exc:
        logger.exception("Metadata insert failed image_id=%s", record.get("image_id"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to save image metadata: {exc}",
        )
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Metadata insert returned no row.",
        )
    return res.data[0]


async def store_image(
    image_bytes: bytes,
    *,
    image_id: Optional[str] = None,
    store_id: Optional[str] = None,
    llm_response: Optional[dict] = None,
    confirmed_by_user: bool = False,
    used_for_training: bool = False,
    content_type: Optional[str] = None,
) -> StoredImage:
    """Upload image to S3 and persist metadata to Supabase. Returns the stored record."""
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty image payload.",
        )

    image_id = image_id or str(uuid.uuid4())
    final_type = content_type or _detect_content_type(image_bytes)
    s3_key = f"{S3_PREFIX.rstrip('/')}/{image_id}{_extension_for(final_type)}"

    url = await _upload_to_s3(image_bytes, s3_key, final_type)

    record = {
        "image_id": image_id,
        "store_id": store_id,
        "llm_response": llm_response,
        "confirmed_by_user": confirmed_by_user,
        "used_for_training": used_for_training,
        "s3_url": url,
        "s3_key": s3_key,
    }
    saved = await _save_metadata(record)

    return StoredImage(
        image_id=image_id,
        url=url,
        timestamp=_parse_timestamp(saved.get("timestamp")) or datetime.now(timezone.utc),
        store_id=saved.get("store_id"),
        llm_response=saved.get("llm_response"),
        confirmed_by_user=saved.get("confirmed_by_user", False),
        used_for_training=saved.get("used_for_training", False),
    )
