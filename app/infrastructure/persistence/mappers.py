from app.domain.entities.audit_log import AuditLogEntity
from app.domain.entities.document import DocumentEntity
from app.domain.entities.signature_region import SignatureRegionEntity
from app.domain.entities.user import UserEntity
from app.domain.value_objects.signature_box import SignatureBox
from app.infrastructure.persistence.models import AuditLogModel, DocumentModel, SignatureRegionModel, UserModel


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
