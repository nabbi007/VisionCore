import base64
import binascii
import json
import logging
import os
from typing import Optional

from anthropic import APIError, AsyncAnthropic
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "512"))

_client: Optional[AsyncAnthropic] = None


class ItemDetails(BaseModel):
    item_name: str = Field(..., description="Recognized product name.")
    brand: Optional[str] = Field(None, description="Brand on the packaging, if visible.")
    category: Optional[str] = Field(None, description="Product category, e.g. beverage, snack.")
    size: Optional[str] = Field(None, description="Package size, e.g. 500ml, 12oz, 250g.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model confidence, 0.0 to 1.0.")


PROMPT = (
    "You are a point-of-sale item recognition assistant. Analyze the product image "
    "and reply with ONLY a JSON object — no prose, no markdown fences — using exactly "
    "these keys:\n\n"
    "{\n"
    '  "item_name": "<string>",\n'
    '  "brand": "<string or null>",\n'
    '  "category": "<string or null>",\n'
    '  "size": "<string or null>",\n'
    '  "confidence": <float between 0 and 1>\n'
    "}\n\n"
    'If you cannot identify the item, set "item_name" to "unknown" and "confidence" to 0.0.'
)


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ANTHROPIC_API_KEY is not configured on the server.",
            )
        _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _detect_media_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported image format. Use PNG, JPEG, GIF, or WebP.",
    )


def _decode_base64(image_b64: str) -> bytes:
    if image_b64.startswith("data:"):
        try:
            image_b64 = image_b64.split(",", 1)[1]
        except IndexError:
            raise HTTPException(status_code=400, detail="Malformed data URL.")
    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid base64 image data.")
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image payload.")
    return image_bytes


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    # Strip ```json ... ``` fences if the model added them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        text = text.rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("Vision model returned non-JSON output: %s", raw[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Vision model returned malformed JSON.",
        )


async def recognize_item(image_b64: str) -> ItemDetails:
    """Identify a POS item in a base64-encoded image via Claude Vision."""
    image_bytes = _decode_base64(image_b64)
    media_type = _detect_media_type(image_bytes)
    # Re-encode to a clean base64 string (strips any data-URL prefix or whitespace).
    clean_b64 = base64.b64encode(image_bytes).decode("ascii")

    client = _get_client()

    try:
        response = await client.messages.create(
            model=VISION_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": clean_b64,
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
    except APIError as exc:
        logger.exception("Claude Vision API error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Vision API request failed: {exc}",
        )

    if not response.content or not hasattr(response.content[0], "text"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Empty response from vision model.",
        )

    data = _extract_json(response.content[0].text)

    try:
        return ItemDetails(**data)
    except ValidationError as exc:
        logger.exception("Vision response failed schema validation: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Vision response failed schema validation.",
        )
