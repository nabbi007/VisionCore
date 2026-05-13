import asyncio
import base64
import hashlib
import logging
import os
from collections import OrderedDict
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.middleware.security import get_current_user
from app.services.storage import store_image
from app.services.vision import ItemDetails, recognize_item

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
CACHE_MAX_SIZE = int(os.getenv("SCAN_CACHE_MAX_SIZE", "1000"))


class CachedScan(BaseModel):
    details: ItemDetails
    image_url: str
    image_id: str


class ScanResponse(BaseModel):
    details: ItemDetails
    image_id: str
    image_url: str
    cached: bool


_cache: "OrderedDict[str, CachedScan]" = OrderedDict()
_cache_lock = asyncio.Lock()


async def _cache_get(key: str) -> Optional[CachedScan]:
    async with _cache_lock:
        value = _cache.get(key)
        if value is None:
            return None
        _cache.move_to_end(key)
        return value


async def _cache_set(key: str, value: CachedScan) -> None:
    async with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_SIZE:
            _cache.popitem(last=False)


@router.post(
    "/",
    status_code=status.HTTP_200_OK,
    summary="Recognize a POS item from an uploaded image.",
)
async def scan_image(
    user: Annotated[Any, Depends(get_current_user)],
    image: Annotated[UploadFile, File(description="Product image (PNG, JPEG, GIF, or WebP).")],
    x_store_id: Annotated[Optional[str], Header(description="Identifier of the POS terminal/store.")] = None,
) -> ScanResponse:
    if image.content_type and not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not an image.",
        )

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty image upload.",
        )
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds the {MAX_IMAGE_BYTES} byte limit.",
        )

    cache_key = hashlib.sha256(image_bytes).hexdigest()
    user_id = getattr(user, "id", None) or getattr(user, "username", "?")

    cached = await _cache_get(cache_key)
    if cached is not None:
        logger.info("scan cache hit user=%s key=%s", user_id, cache_key[:12])
        return ScanResponse(
            details=cached.details,
            image_id=cached.image_id,
            image_url=cached.image_url,
            cached=True,
        )

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    details = await recognize_item(image_b64)

    stored = await store_image(
        image_bytes,
        store_id=x_store_id,
        llm_response=details.model_dump(),
        content_type=image.content_type,
    )

    await _cache_set(
        cache_key,
        CachedScan(details=details, image_url=stored.url, image_id=stored.image_id),
    )
    logger.info("scan cache miss user=%s key=%s image_id=%s", user_id, cache_key[:12], stored.image_id)
    return ScanResponse(
        details=details,
        image_id=stored.image_id,
        image_url=stored.url,
        cached=False,
    )
