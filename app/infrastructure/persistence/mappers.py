from datetime import UTC, datetime
from uuid import UUID

from app.domain.entities.audit_log import AuditLogEntity
from app.domain.entities.document import DocumentEntity
from app.domain.entities.integration import CallbackRecord, IntegrationAuditEntry
from app.domain.entities.signature_region import SignatureRegionEntity
from app.domain.entities.user import UserEntity
from app.domain.value_objects.signature_box import SignatureBox
from app.infrastructure.persistence.models import (
    AuditLogModel,
    CallbackAuditLogModel,
    DocumentModel,
    IntegrationAuditLogModel,
    SignatureRegionModel,
    UserModel,
)


def map_user(model: UserModel) -> UserEntity:
    return UserEntity(
        id=model.id,
        name=model.name,
        email=model.email,
        password_hash=model.password_hash,
        role=model.role,
        created_at=model.created_at,
    )


def map_region(model: SignatureRegionModel) -> SignatureRegionEntity:
    return SignatureRegionEntity(
        id=model.id,
        document_id=model.document_id,
        box=SignatureBox(
            page_number=model.page_number,
            x=model.x,
            y=model.y,
            width=model.width,
            height=model.height,
        ),
        assigned_to=model.assigned_to,
        signed=model.signed,
        signed_at=model.signed_at,
        signature_image_path=model.signature_image_path,
    )


def map_document(model: DocumentModel, include_regions: bool = True) -> DocumentEntity:
    regions = [map_region(region) for region in model.signature_regions] if include_regions else []
    return DocumentEntity(
        id=model.id,
        title=model.title,
        uploaded_by=model.uploaded_by,
        original_path=model.original_path,
        final_path=model.final_path,
        final_hash=model.final_hash,
        total_pages=model.total_pages,
        status=model.status,
        created_at=model.created_at,
        regions=regions,
        external_document_id=getattr(model, "external_document_id", None),
        external_path=getattr(model, "external_path", None),
    )


def map_audit_log(model: AuditLogModel) -> AuditLogEntity:
    return AuditLogEntity(
        id=model.id,
        document_id=model.document_id,
        user_id=model.user_id,
        action=model.action,
        ip_address=model.ip_address,
        user_agent=model.user_agent,
        document_hash=model.document_hash,
        timestamp=model.timestamp,
    )


def map_integration_audit(model: IntegrationAuditLogModel) -> IntegrationAuditEntry:
    return IntegrationAuditEntry(
        id=model.id,
        event=model.event,
        correlation_id=model.correlation_id,
        external_user_id=model.external_user_id,
        document_id=UUID(str(model.document_id)) if model.document_id else None,
        external_document_id=model.external_document_id,
        details=model.details,
        success=model.success,
        timestamp=model.timestamp,
    )


def map_callback_record(model: CallbackAuditLogModel) -> CallbackRecord:
    return CallbackRecord(
        id=model.id,
        idempotency_key=model.idempotency_key,
        external_document_id=model.external_document_id,
        external_user_id=model.external_user_id,
        status=model.status,
        attempts=model.attempts,
        last_attempt_at=model.last_attempt_at,
        succeeded=model.succeeded,
        last_error=model.last_error,
        created_at=model.created_at,
    )
