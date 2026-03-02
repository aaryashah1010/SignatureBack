from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.entities.enums import DocumentStatus
from app.domain.entities.signature_region import SignatureRegionEntity


@dataclass(slots=True)
class DocumentEntity:
    id: UUID
    title: str
    uploaded_by: UUID
    original_path: str
    final_path: str | None
    final_hash: str | None
    total_pages: int
    status: DocumentStatus
    created_at: datetime
    regions: list[SignatureRegionEntity] = field(default_factory=list)

    def recompute_status(self) -> DocumentStatus:
        if not self.regions:
            self.status = DocumentStatus.DRAFT
            return self.status
        signed_count = len([region for region in self.regions if region.signed])
        if signed_count == 0:
            self.status = DocumentStatus.PENDING
        elif signed_count < len(self.regions):
            self.status = DocumentStatus.PARTIALLY_SIGNED
        else:
            self.status = DocumentStatus.COMPLETED
        return self.status
