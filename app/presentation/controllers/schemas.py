from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.entities.enums import DocumentStatus, SignatureMethod, UserRole


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: EmailStr
    role: UserRole
    created_at: datetime


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class DocumentUploadResponse(BaseModel):
    id: UUID
    title: str
    status: DocumentStatus
    total_pages: int
    created_at: datetime


class RegionCreateItem(BaseModel):
    page_number: int = Field(ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    assigned_to: UUID


class RegionCreateRequest(BaseModel):
    regions: list[RegionCreateItem] = Field(min_length=1)


class RegionResponse(BaseModel):
    id: UUID
    page_number: int
    x: float
    y: float
    width: float
    height: float
    assigned_to: UUID
    signed: bool
    signed_at: datetime | None


class DocumentResponse(BaseModel):
    id: UUID
    title: str
    uploaded_by: UUID
    status: DocumentStatus
    total_pages: int
    final_hash: str | None
    created_at: datetime
    regions: list[RegionResponse]


class SignDocumentRequest(BaseModel):
    region_id: UUID
    method: SignatureMethod
    page_number: int
    x: float
    y: float
    width: float
    height: float
    drawn_signature_base64: str | None = None
    typed_name: str | None = None
    typed_font: str | None = None
    uploaded_signature_base64: str | None = None
