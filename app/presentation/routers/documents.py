from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse

from app.application.services.document_workflow_service import DocumentWorkflowService
from app.application.use_cases.create_regions import CreateRegionsUseCase
from app.application.use_cases.sign_document import SignDocumentUseCase
from app.application.use_cases.upload_document import UploadDocumentUseCase
from app.core.dependencies import get_current_user, get_document_workflow_service, request_context, require_role
from app.domain.entities.enums import UserRole
from app.domain.entities.user import UserEntity
from app.presentation.controllers.schemas import (
    DocumentResponse,
    DocumentUploadResponse,
    RegionCreateRequest,
    RegionResponse,
    SignDocumentRequest,
)
from app.presentation.controllers.serializers import to_document_response, to_region_response


router = APIRouter(prefix="/documents", tags=["Documents"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    title: Annotated[str, Form(...)],
    file: Annotated[UploadFile, File(...)],
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
) -> DocumentUploadResponse:
    use_case = UploadDocumentUseCase(workflow_service)
    payload = await file.read()
    document = await use_case.execute(admin_id=admin_user.id, title=title, pdf_bytes=payload, filename=file.filename)
    return DocumentUploadResponse(
        id=document.id,
        title=document.title,
        status=document.status,
        total_pages=document.total_pages,
        created_at=document.created_at,
    )


@router.post("/{document_id}/regions", response_model=list[RegionResponse], status_code=status.HTTP_201_CREATED)
async def create_regions(
    document_id: UUID,
    payload: RegionCreateRequest,
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    metadata: Annotated[dict[str, str], Depends(request_context)],
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
) -> list[RegionResponse]:
    use_case = CreateRegionsUseCase(workflow_service)
    regions = await use_case.execute(
        admin_id=admin_user.id,
        document_id=document_id,
        region_payloads=[item.model_dump() for item in payload.regions],
        request_ip=metadata["ip_address"],
        user_agent=metadata["user_agent"],
    )
    return [to_region_response(region) for region in regions]


@router.get("/my", response_model=list[DocumentResponse])
async def list_my_documents(
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
) -> list[DocumentResponse]:
    documents = await workflow_service.list_admin_documents(admin_user.id)
    return [to_document_response(document) for document in documents]


@router.get("/pending", response_model=list[DocumentResponse])
async def list_pending_documents(
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    signer_user: Annotated[UserEntity, Depends(require_role({UserRole.SIGNER}))],
) -> list[DocumentResponse]:
    documents = await workflow_service.list_signer_pending_documents(signer_user.id)
    return [to_document_response(document) for document in documents]


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID,
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    current_user: Annotated[UserEntity, Depends(get_current_user)],
) -> DocumentResponse:
    document = await workflow_service.get_document_for_user(document_id, current_user.id, current_user.role)
    return to_document_response(document)


@router.get("/{document_id}/file")
async def get_document_file(
    document_id: UUID,
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    current_user: Annotated[UserEntity, Depends(get_current_user)],
) -> FileResponse:
    file_path = await workflow_service.get_render_path_for_user(
        document_id=document_id,
        requester_id=current_user.id,
        role=current_user.role,
    )
    return FileResponse(path=Path(file_path), media_type="application/pdf", filename=f"{document_id}.pdf")


@router.post("/{document_id}/sign", response_model=DocumentResponse)
async def sign_document(
    document_id: UUID,
    payload: SignDocumentRequest,
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    metadata: Annotated[dict[str, str], Depends(request_context)],
    signer_user: Annotated[UserEntity, Depends(require_role({UserRole.SIGNER}))],
) -> DocumentResponse:
    use_case = SignDocumentUseCase(workflow_service)
    updated_document = await use_case.execute(
        document_id=document_id,
        signer_id=signer_user.id,
        sign_payload=payload.model_dump(),
        request_ip=metadata["ip_address"],
        user_agent=metadata["user_agent"],
    )
    return to_document_response(updated_document)


@router.get("/{document_id}/download")
async def download_document(
    document_id: UUID,
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
) -> FileResponse:
    file_path = await workflow_service.get_download_path_for_admin(document_id=document_id, admin_id=admin_user.id)
    return FileResponse(
        path=Path(file_path),
        media_type="application/pdf",
        filename=f"signed_{document_id}.pdf",
    )
