from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from app.domain.entities.document import DocumentEntity
from app.domain.entities.signature_region import SignatureRegionEntity
from app.domain.value_objects.signature_box import SignatureBox


class DocumentRepository(ABC):
    @abstractmethod
    async def create_document(
        self,
        title: str,
        uploaded_by: UUID,
        original_path: str,
        total_pages: int,
        external_document_id: str | None = None,
        external_path: str | None = None,
    ) -> DocumentEntity:
        raise NotImplementedError

    @abstractmethod
    async def get_document_by_id(self, document_id: UUID) -> DocumentEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def list_documents_uploaded_by(self, admin_id: UUID) -> list[DocumentEntity]:
        raise NotImplementedError

    @abstractmethod
    async def list_pending_documents_for_signer(self, signer_id: UUID) -> list[DocumentEntity]:
        raise NotImplementedError

    @abstractmethod
    async def create_signature_regions(
        self, document_id: UUID, regions: list[tuple[SignatureBox, UUID]]
    ) -> list[SignatureRegionEntity]:
        raise NotImplementedError

    @abstractmethod
    async def get_region_by_id(self, region_id: UUID) -> SignatureRegionEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def mark_region_signed(
        self,
        region_id: UUID,
        signature_image_path: str,
        signed_at: datetime,
    ) -> SignatureRegionEntity:
        raise NotImplementedError

    @abstractmethod
    async def update_document_after_sign(
        self,
        document_id: UUID,
        final_path: str,
        final_hash: str,
    ) -> DocumentEntity:
        raise NotImplementedError

    @abstractmethod
    def serialize_document_for_cache(self, document: DocumentEntity) -> dict:
        raise NotImplementedError

    @abstractmethod
    def deserialize_document_from_cache(self, payload: dict) -> DocumentEntity:
        raise NotImplementedError

    @abstractmethod
    async def get_by_external_document_id(self, external_document_id: str) -> DocumentEntity | None:
        """Find the local document linked to a given external_document_id."""
        raise NotImplementedError

    @abstractmethod
    async def get_external_document_id(self, document_id: UUID) -> str | None:
        """Return the external_document_id for a local document, or None."""
        raise NotImplementedError
