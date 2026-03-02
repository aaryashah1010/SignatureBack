from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.value_objects.signature_box import SignatureBox


@dataclass(slots=True)
class SignatureRegionEntity:
    id: UUID
    document_id: UUID
    box: SignatureBox
    assigned_to: UUID
    signed: bool
    signed_at: datetime | None
    signature_image_path: str | None
