from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.document_workflow_service import DocumentWorkflowService
from app.core.database import get_db_session
from app.core.dependencies import get_current_user, get_document_workflow_service, require_role
from app.domain.entities.enums import UserRole
from app.domain.entities.user import UserEntity
from app.infrastructure.persistence.repositories import SqlAlchemyUserRepository
from app.presentation.controllers.schemas import UserResponse


router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/signers", response_model=list[UserResponse])
async def list_signers(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
) -> list[UserResponse]:
    users = await SqlAlchemyUserRepository(session).list_signers()
    return [UserResponse.model_validate(user) for user in users]


@router.get("/me/signature")
async def get_my_signature(
    workflow_service: Annotated[DocumentWorkflowService, Depends(get_document_workflow_service)],
    current_user: Annotated[UserEntity, Depends(get_current_user)],
) -> dict:
    """Return the current user's remembered signature (if any) for one-click 'apply to all'."""
    signature = await workflow_service.get_user_signature(current_user.id)
    return {"has_signature": signature is not None, "signature": signature}
