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


class AnnotationCreateItem(BaseModel):
    page_number: int = Field(ge=1)
    kind: str = Field(pattern="^(highlight|drawing|text)$")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    color: str = Field(default="#fde047", max_length=20)
    text: str = Field(default="", max_length=2000)
    # JSON-serialized polyline array (normalized) for DRAWING annotations.
    paths: str = Field(default="", max_length=200000)


class AnnotationCreateRequest(BaseModel):
    annotations: list[AnnotationCreateItem] = Field(min_length=1)


class AnnotationResponse(BaseModel):
    id: UUID
    page_number: int
    kind: str
    x: float
    y: float
    width: float
    height: float
    color: str
    text: str
    paths: str
    created_by: UUID
    created_at: datetime


class DocumentResponse(BaseModel):
    id: UUID
    title: str
    uploaded_by: UUID
    status: DocumentStatus
    total_pages: int
    final_hash: str | None
    created_at: datetime
    regions: list[RegionResponse]
    annotations: list[AnnotationResponse] = Field(default_factory=list)


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


class SignAllRequest(BaseModel):
    """One signature to apply to every region assigned to the current signer."""

    method: SignatureMethod
    drawn_signature_base64: str | None = None
    typed_name: str | None = None
    typed_font: str | None = None
    uploaded_signature_base64: str | None = None
    remember_signature: bool = Field(
        default=False, description="Persist this signature for reuse on future documents"
    )


# ── Integration schemas ───────────────────────────────────────────────────────


class LaunchRequest(BaseModel):
    """EsignGuid + role posted by the external software."""

    token: str = Field(min_length=10, description="EsignRequestGuid from ESignRequests table")
    role: str = Field(default="", description="Role sent by CpaDesk e.g. CpaAdmin, CpaClient")
    login_detail_id: int | None = Field(default=None, description="LoginDetailID for signer (3-tier flow, CpaClient role)")


class LaunchResponse(BaseModel):
    """Returned after a successful launch-token exchange."""

    access_token: str
    token_type: str = "bearer"
    role: str
    # Frontend uses next_route to navigate immediately to the correct page.
    next_route: str
    document_id: UUID | None = None
    user: UserResponse


class MappedSignerResponse(BaseModel):
    """Signer entry returned from the mapping-constrained signer list."""

    id: UUID
    name: str
    email: str


class SubmitDocumentResponse(BaseModel):
    """Result of a SIGNER submitting a fully-signed document."""

    document_id: UUID
    status: str
    signed_regions: int
    total_regions: int
    callback_triggered: bool


class SigningProgressResponse(BaseModel):
    """Signing progress counter for the signer UI."""

    document_id: UUID
    assigned_total: int
    assigned_signed: int
    can_submit: bool
