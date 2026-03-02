from uuid import UUID

from app.application.services.document_workflow_service import DocumentWorkflowService
from app.domain.entities.signature_region import SignatureRegionEntity


class CreateRegionsUseCase:
    def __init__(self, workflow_service: DocumentWorkflowService) -> None:
        self.workflow_service = workflow_service

    async def execute(
        self,
        admin_id: UUID,
        document_id: UUID,
        region_payloads: list[dict],
        request_ip: str,
        user_agent: str,
    ) -> list[SignatureRegionEntity]:
        return await self.workflow_service.create_regions(
            admin_id=admin_id,
            document_id=document_id,
            region_payloads=region_payloads,
            request_ip=request_ip,
            user_agent=user_agent,
        )
