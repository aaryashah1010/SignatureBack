import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.entities.document import DocumentEntity
from app.domain.entities.enums import DocumentStatus, SignatureMethod, UserRole
from app.domain.entities.signature_region import SignatureRegionEntity
from app.domain.repositories.audit_repository import AuditRepository
from app.domain.repositories.document_repository import DocumentRepository
from app.domain.repositories.user_repository import UserRepository
from app.domain.value_objects.signature_box import SignatureBox
from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService
from app.infrastructure.redis.event_bus import RedisEventBus


class DocumentWorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        user_repository: UserRepository,
        document_repository: DocumentRepository,
        audit_repository: AuditRepository,
        pdf_service: SignaturePdfService,
        event_bus: RedisEventBus,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.user_repository = user_repository
        self.document_repository = document_repository
        self.audit_repository = audit_repository
        self.pdf_service = pdf_service
        self.event_bus = event_bus

    async def upload_document(self, admin_id: UUID, title: str, pdf_bytes: bytes, filename: str) -> DocumentEntity:
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")

        safe_name = f"{uuid4()}.pdf"
        output_path = self.settings.original_storage_dir / safe_name
        output_path.write_bytes(pdf_bytes)
        total_pages = self.pdf_service.get_page_count(output_path)

        document = await self.document_repository.create_document(
            title=title,
            uploaded_by=admin_id,
            original_path=str(output_path),
            total_pages=total_pages,
        )
        await self.session.commit()

        await self.audit_repository.create_log(
            document_id=document.id,
            user_id=admin_id,
            action="DOCUMENT_UPLOADED",
            ip_address="system",
            user_agent="system",
        )
        await self.session.commit()
        await self.event_bus.publish("workflow.events", {"type": "document_uploaded", "document_id": str(document.id)})
        return document

    async def create_regions(
        self,
        admin_id: UUID,
        document_id: UUID,
        region_payloads: list[dict],
        request_ip: str,
        user_agent: str,
    ) -> list[SignatureRegionEntity]:
        document = await self.document_repository.get_document_by_id(document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if document.uploaded_by != admin_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not owner of this document")

        prepared_regions: list[tuple[SignatureBox, UUID]] = []
        signer_ids: set[UUID] = set()
        for item in region_payloads:
            signer_id = UUID(str(item["assigned_to"]))
            signer = await self.user_repository.get_by_id(signer_id)
            if not signer or signer.role != UserRole.SIGNER:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"assigned_to {signer_id} is not a valid signer",
                )

            box = SignatureBox(
                page_number=item["page_number"],
                x=item["x"],
                y=item["y"],
                width=item["width"],
                height=item["height"],
            )
            box.validate()
            if box.page_number > document.total_pages:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="page_number out of range")
            signer_ids.add(signer_id)
            prepared_regions.append((box, signer_id))

        regions = await self.document_repository.create_signature_regions(document_id=document_id, regions=prepared_regions)
        await self.session.commit()

        await self.audit_repository.create_log(
            document_id=document_id,
            user_id=admin_id,
            action="REGIONS_DEFINED",
            ip_address=request_ip,
            user_agent=user_agent,
        )
        await self.session.commit()

        for signer_id in signer_ids:
            await self.event_bus.invalidate_key(f"pending_documents:{signer_id}")
        await self.event_bus.publish("workflow.events", {"type": "regions_created", "document_id": str(document_id)})
        return regions

    async def list_admin_documents(self, admin_id: UUID) -> list[DocumentEntity]:
        return await self.document_repository.list_documents_uploaded_by(admin_id)

    async def list_signer_pending_documents(self, signer_id: UUID) -> list[DocumentEntity]:
        cache_key = f"pending_documents:{signer_id}"
        cached = await self.event_bus.get_json(cache_key)
        if cached:
            return [self.document_repository.deserialize_document_from_cache(item) for item in cached]

        documents = await self.document_repository.list_pending_documents_for_signer(signer_id)
        await self.event_bus.set_json(cache_key, [self.document_repository.serialize_document_for_cache(doc) for doc in documents])
        return documents

    async def get_document_for_user(self, document_id: UUID, requester_id: UUID, role: UserRole) -> DocumentEntity:
        document = await self.document_repository.get_document_by_id(document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        if role == UserRole.ADMIN and document.uploaded_by != requester_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not owner of this document")
        if role == UserRole.SIGNER:
            has_access = any(region.assigned_to == requester_id for region in document.regions)
            if not has_access:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this document")
        return document

    async def sign_region(
        self,
        document_id: UUID,
        signer_id: UUID,
        sign_request: dict,
        request_ip: str,
        user_agent: str,
    ) -> DocumentEntity:
        document = await self.document_repository.get_document_by_id(document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        region_id = UUID(str(sign_request["region_id"]))
        region = await self.document_repository.get_region_by_id(region_id)
        if not region or region.document_id != document_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signature region not found")
        if region.assigned_to != signer_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Region is assigned to a different signer")

        client_box = SignatureBox(
            page_number=sign_request["page_number"],
            x=sign_request["x"],
            y=sign_request["y"],
            width=sign_request["width"],
            height=sign_request["height"],
        )
        client_box.validate()
        if not client_box.is_approximately_equal(region.box):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Region coordinates mismatch")

        method = SignatureMethod(sign_request["method"])
        signature_bytes = self._build_signature_bytes(method=method, payload=sign_request)

        target_pdf = self.settings.signed_storage_dir / f"{document.id}_{region.id}_{uuid4()}.pdf"
        signature_image_path = self.settings.signature_images_dir / f"{region.id}_{uuid4()}.png"
        signature_image_path.write_bytes(signature_bytes)

        # Rebuild from original to avoid signature stacking artifacts and allow precise re-signing.
        all_signatures = self._collect_signatures_for_render(
            document=document,
            target_region_id=region.id,
            target_signature_bytes=signature_bytes,
        )
        self.pdf_service.apply_signatures(
            source_pdf=Path(document.original_path),
            target_pdf=target_pdf,
            signatures=all_signatures,
        )

        signed_at = datetime.now(UTC)
        was_resigned = region.signed
        await self.document_repository.mark_region_signed(
            region_id=region.id,
            signature_image_path=str(signature_image_path),
            signed_at=signed_at,
        )

        final_hash = hashlib.sha256(target_pdf.read_bytes()).hexdigest()
        updated_document = await self.document_repository.update_document_after_sign(
            document_id=document_id,
            final_path=str(target_pdf),
            final_hash=final_hash,
        )

        await self.audit_repository.create_log(
            document_id=document_id,
            user_id=signer_id,
            action="REGION_RESIGNED" if was_resigned else "REGION_SIGNED",
            ip_address=request_ip,
            user_agent=user_agent,
            document_hash=final_hash,
        )
        await self.session.commit()

        await self.event_bus.invalidate_key(f"pending_documents:{signer_id}")
        await self.event_bus.publish("workflow.events", {"type": "region_signed", "document_id": str(document_id)})
        return updated_document

    def _collect_signatures_for_render(
        self,
        document: DocumentEntity,
        target_region_id: UUID,
        target_signature_bytes: bytes,
    ) -> list[tuple[SignatureBox, bytes]]:
        signatures: list[tuple[SignatureBox, bytes]] = []

        for region in document.regions:
            if region.id == target_region_id:
                signatures.append((region.box, target_signature_bytes))
                continue

            if not region.signed or not region.signature_image_path:
                continue

            image_path = Path(region.signature_image_path)
            if image_path.exists():
                signatures.append((region.box, image_path.read_bytes()))

        signatures.sort(key=lambda item: (item[0].page_number, item[0].y, item[0].x))
        return signatures

    async def get_download_path_for_admin(self, document_id: UUID, admin_id: UUID) -> Path:
        document = await self.document_repository.get_document_by_id(document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if document.uploaded_by != admin_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not owner of this document")
        if document.status != DocumentStatus.COMPLETED or not document.final_path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document is not fully signed yet")
        return Path(document.final_path)

    async def get_render_path_for_user(self, document_id: UUID, requester_id: UUID, role: UserRole) -> Path:
        document = await self.get_document_for_user(document_id=document_id, requester_id=requester_id, role=role)
        active_path = document.final_path if document.final_path else document.original_path
        return Path(active_path)

    def _build_signature_bytes(self, method: SignatureMethod, payload: dict) -> bytes:
        if method == SignatureMethod.DRAW:
            data = payload.get("drawn_signature_base64")
            if not data:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drawn signature")
            return self._decode_base64_image(data)

        if method == SignatureMethod.UPLOAD:
            data = payload.get("uploaded_signature_base64")
            if not data:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing uploaded signature")
            return self._decode_base64_image(data)

        typed_name = payload.get("typed_name")
        typed_font = payload.get("typed_font")
        if not typed_name or not typed_font:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing typed signature fields")
        return self.pdf_service.render_typed_signature(typed_name=typed_name, typed_font=typed_font)

    @staticmethod
    def _decode_base64_image(data: str) -> bytes:
        raw = data.split(",", 1)[-1] if "," in data else data
        try:
            return base64.b64decode(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid base64 image payload") from exc
