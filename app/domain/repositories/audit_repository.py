from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.entities.audit_log import AuditLogEntity


class AuditRepository(ABC):
    @abstractmethod
    async def create_log(
        self,
        document_id: UUID,
        user_id: UUID,
        action: str,
        ip_address: str,
        user_agent: str,
        document_hash: str | None = None,
    ) -> AuditLogEntity:
        raise NotImplementedError
