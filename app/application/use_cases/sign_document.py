from uuid import UUID

from app.application.services.document_workflow_service import DocumentWorkflowService
from app.domain.entities.document import DocumentEntity


class SignDocumentUseCase:
    def __init__(self, workflow_service: DocumentWorkflowService) -> None:
        self.workflow_service = workflow_service

    async def execute(
        self,
        document_id: UUID,
        signer_id: UUID,
        sign_payload: dict,
        request_ip: str,
        user_agent: str,
    ) -> DocumentEntity:
        return await self.workflow_service.sign_region(
            document_id=document_id,
            signer_id=signer_id,
            sign_request=sign_payload,
            request_ip=request_ip,
            user_agent=user_agent,
        )
