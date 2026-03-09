from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.domain.entities.enums import DocumentStatus, UserRole


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    uploaded_documents: Mapped[list["DocumentModel"]] = relationship(back_populates="uploader")


class DocumentModel(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    final_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Populated when the document was bootstrapped from an external system launch.
    external_document_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # PhysicalRelativePath from DocumentMaster – where the signed PDF is written back on submit.
    external_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        default=DocumentStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    uploader: Mapped["UserModel"] = relationship(back_populates="uploaded_documents")
    signature_regions: Mapped[list["SignatureRegionModel"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLogModel"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class SignatureRegionModel(Base):
    __tablename__ = "signature_regions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    assigned_to: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signature_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped["DocumentModel"] = relationship(back_populates="signature_regions")


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(128), nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, nullable=False)
    document_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    document: Mapped["DocumentModel"] = relationship(back_populates="audit_logs")


# ── Integration-specific tables ──────────────────────────────────────────────


class IntegrationAuditLogModel(Base):
    """Tracks all integration lifecycle events (launch, link, mapping, submit, callback)."""

    __tablename__ = "integration_audit_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    event: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # Optional FK to a local document – nullable because some events precede doc creation.
    document_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    external_document_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class CallbackAuditLogModel(Base):
    """Tracks idempotent outbound callbacks to the external system per document/event."""

    __tablename__ = "callback_audit_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    # SHA-256 of (external_document_id + external_user_id + status) for dedup.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    external_document_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    external_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
