from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.entities.audit_log import AuditLogEntity
from app.domain.entities.document import DocumentEntity
from app.domain.entities.enums import DocumentStatus, UserRole
from app.domain.entities.signature_region import SignatureRegionEntity
from app.domain.entities.user import UserEntity
from app.domain.repositories.audit_repository import AuditRepository
from app.domain.repositories.document_repository import DocumentRepository
from app.domain.repositories.user_repository import UserRepository
from app.domain.value_objects.signature_box import SignatureBox
from app.infrastructure.persistence.mappers import map_audit_log, map_document, map_region, map_user
from app.infrastructure.persistence.models import AuditLogModel, DocumentModel, SignatureRegionModel, UserModel


class SqlAlchemyUserRepository(UserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: UUID) -> UserEntity | None:
        result = await self.session.execute(select(UserModel).where(UserModel.id == user_id))
        row = result.scalar_one_or_none()
        return map_user(row) if row else None

    async def get_by_email(self, email: str) -> UserEntity | None:
        result = await self.session.execute(select(UserModel).where(UserModel.email == email.lower().strip()))
        row = result.scalar_one_or_none()
        return map_user(row) if row else None

    async def create(self, name: str, email: str, password_hash: str, role: UserRole) -> UserEntity:
        model = UserModel(name=name, email=email.lower().strip(), password_hash=password_hash, role=role)
        self.session.add(model)
        await self.session.flush()
        return map_user(model)

    async def list_signers(self) -> list[UserEntity]:
        result = await self.session.execute(select(UserModel).where(UserModel.role == UserRole.SIGNER).order_by(UserModel.created_at))
        return [map_user(row) for row in result.scalars().all()]


class SqlAlchemyDocumentRepository(DocumentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_document(self, title: str, uploaded_by: UUID, original_path: str, total_pages: int) -> DocumentEntity:
        model = DocumentModel(
            title=title.strip(),
            uploaded_by=uploaded_by,
            original_path=original_path,
            total_pages=total_pages,
            status=DocumentStatus.DRAFT,
        )
        self.session.add(model)
        await self.session.flush()
        await self.session.refresh(model)
        # Freshly created documents have no regions yet; avoid async lazy-load on relationship access.
        return map_document(model, include_regions=False)

    async def get_document_by_id(self, document_id: UUID) -> DocumentEntity | None:
        result = await self.session.execute(
            select(DocumentModel)
            .where(DocumentModel.id == document_id)
            .options(selectinload(DocumentModel.signature_regions))
        )
        row = result.scalar_one_or_none()
        return map_document(row) if row else None

    async def list_documents_uploaded_by(self, admin_id: UUID) -> list[DocumentEntity]:
        result = await self.session.execute(
            select(DocumentModel)
            .where(DocumentModel.uploaded_by == admin_id)
            .order_by(DocumentModel.created_at.desc())
            .options(selectinload(DocumentModel.signature_regions))
        )
        return [map_document(row) for row in result.scalars().all()]

    async def list_pending_documents_for_signer(self, signer_id: UUID) -> list[DocumentEntity]:
        result = await self.session.execute(
            select(DocumentModel)
            .join(SignatureRegionModel, SignatureRegionModel.document_id == DocumentModel.id)
            .where(
                and_(
                    SignatureRegionModel.assigned_to == signer_id,
                    SignatureRegionModel.signed.is_(False),
                    DocumentModel.status.in_(
                        [DocumentStatus.PENDING, DocumentStatus.PARTIALLY_SIGNED],
                    ),
                )
            )
            .order_by(DocumentModel.created_at.desc())
            .options(selectinload(DocumentModel.signature_regions))
            .distinct()
        )
        return [map_document(row) for row in result.scalars().all()]

    async def create_signature_regions(
        self, document_id: UUID, regions: list[tuple[SignatureBox, UUID]]
    ) -> list[SignatureRegionEntity]:
        created: list[SignatureRegionModel] = []
        for box, signer_id in regions:
            model = SignatureRegionModel(
                document_id=document_id,
                page_number=box.page_number,
                x=box.x,
                y=box.y,
                width=box.width,
                height=box.height,
                assigned_to=signer_id,
                signed=False,
            )
            self.session.add(model)
            created.append(model)

        document_result = await self.session.execute(select(DocumentModel).where(DocumentModel.id == document_id))
        document = document_result.scalar_one()
        document.status = DocumentStatus.PENDING

        await self.session.flush()
        return [map_region(model) for model in created]

    async def get_region_by_id(self, region_id: UUID) -> SignatureRegionEntity | None:
        result = await self.session.execute(select(SignatureRegionModel).where(SignatureRegionModel.id == region_id))
        row = result.scalar_one_or_none()
        return map_region(row) if row else None

    async def mark_region_signed(
        self,
        region_id: UUID,
        signature_image_path: str,
        signed_at: datetime,
    ) -> SignatureRegionEntity:
        result = await self.session.execute(select(SignatureRegionModel).where(SignatureRegionModel.id == region_id))
        region = result.scalar_one()
        region.signed = True
        region.signed_at = signed_at
        region.signature_image_path = signature_image_path
        await self.session.flush()
        return map_region(region)

    async def update_document_after_sign(
        self,
        document_id: UUID,
        final_path: str,
        final_hash: str,
    ) -> DocumentEntity:
        result = await self.session.execute(
            select(DocumentModel).where(DocumentModel.id == document_id).options(selectinload(DocumentModel.signature_regions))
        )
        document = result.scalar_one()
        document.final_path = final_path
        document.final_hash = final_hash

        total_regions = len(document.signature_regions)
        signed_regions = len([region for region in document.signature_regions if region.signed])
        if total_regions == 0:
            document.status = DocumentStatus.DRAFT
        elif signed_regions == 0:
            document.status = DocumentStatus.PENDING
        elif signed_regions < total_regions:
            document.status = DocumentStatus.PARTIALLY_SIGNED
        else:
            document.status = DocumentStatus.COMPLETED

        await self.session.flush()
        return map_document(document)

    def serialize_document_for_cache(self, document: DocumentEntity) -> dict:
        return {
            "id": str(document.id),
            "title": document.title,
            "uploaded_by": str(document.uploaded_by),
            "original_path": document.original_path,
            "final_path": document.final_path,
            "final_hash": document.final_hash,
            "total_pages": document.total_pages,
            "status": document.status.value,
            "created_at": document.created_at.isoformat(),
            "regions": [
                {
                    "id": str(region.id),
                    "document_id": str(region.document_id),
                    "page_number": region.box.page_number,
                    "x": region.box.x,
                    "y": region.box.y,
                    "width": region.box.width,
                    "height": region.box.height,
                    "assigned_to": str(region.assigned_to),
                    "signed": region.signed,
                    "signed_at": region.signed_at.isoformat() if region.signed_at else None,
                    "signature_image_path": region.signature_image_path,
                }
                for region in document.regions
            ],
        }

    def deserialize_document_from_cache(self, payload: dict) -> DocumentEntity:
        regions = [
            SignatureRegionEntity(
                id=UUID(item["id"]),
                document_id=UUID(item["document_id"]),
                box=SignatureBox(
                    page_number=item["page_number"],
                    x=item["x"],
                    y=item["y"],
                    width=item["width"],
                    height=item["height"],
                ),
                assigned_to=UUID(item["assigned_to"]),
                signed=item["signed"],
                signed_at=datetime.fromisoformat(item["signed_at"]) if item["signed_at"] else None,
                signature_image_path=item["signature_image_path"],
            )
            for item in payload["regions"]
        ]
        return DocumentEntity(
            id=UUID(payload["id"]),
            title=payload["title"],
            uploaded_by=UUID(payload["uploaded_by"]),
            original_path=payload["original_path"],
            final_path=payload["final_path"],
            final_hash=payload["final_hash"],
            total_pages=payload["total_pages"],
            status=DocumentStatus(payload["status"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            regions=regions,
        )


class SqlAlchemyAuditLogRepository(AuditRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_log(
        self,
        document_id: UUID,
        user_id: UUID,
        action: str,
        ip_address: str,
        user_agent: str,
        document_hash: str | None = None,
    ) -> AuditLogEntity:
        model = AuditLogModel(
            document_id=document_id,
            user_id=user_id,
            action=action,
            ip_address=ip_address,
            user_agent=user_agent,
            document_hash=document_hash,
        )
        self.session.add(model)
        await self.session.flush()
        return map_audit_log(model)
