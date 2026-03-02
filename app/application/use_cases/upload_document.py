from uuid import UUID

from app.application.services.document_workflow_service import DocumentWorkflowService
from app.domain.entities.document import DocumentEntity


class UploadDocumentUseCase:
    def __init__(self, workflow_service: DocumentWorkflowService) -> None:
        self.workflow_service = workflow_service

    async def execute(self, admin_id: UUID, title: str, pdf_bytes: bytes, filename: str) -> DocumentEntity:
        return await self.workflow_service.upload_document(
            admin_id=admin_id,
            title=title,
            pdf_bytes=pdf_bytes,
            filename=filename,
        )
