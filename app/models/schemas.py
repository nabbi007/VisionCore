from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class ItemDetails(BaseModel):
    item_name: str = Field(..., description="Recognized product name.")
    brand: Optional[str] = Field(None, description="Brand on the packaging, if visible.")
    category: Optional[str] = Field(None, description="Product category, e.g. beverage, snack.")
    size: Optional[str] = Field(None, description="Package size, e.g. 500ml, 12oz, 250g.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model confidence between 0 and 1.")


class ScanRequest(BaseModel):
    image: str = Field(..., description="Base64-encoded product image (optionally a data URL).")
    store_id: Optional[str] = Field(None, description="Identifier of the POS terminal or store.")


class ScanResponse(BaseModel):
    details: ItemDetails
    image_id: str = Field(..., description="UUID assigned to the stored image.")
    image_url: str = Field(..., description="Public URL of the uploaded image.")
    cached: bool = Field(..., description="True if served from the recognition cache.")


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds.")


class ImageMetadata(BaseModel):
    image_id: str
    timestamp: datetime
    store_id: Optional[str] = None
    llm_response: Optional[dict] = None
    confirmed_by_user: bool = False
    used_for_training: bool = False
