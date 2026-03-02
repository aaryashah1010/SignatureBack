from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(slots=True)
class AuditLogEntity:
    id: UUID
    document_id: UUID
    user_id: UUID
    action: str
    ip_address: str
    user_agent: str
    document_hash: str | None
    timestamp: datetime
