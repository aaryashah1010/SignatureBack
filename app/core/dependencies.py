from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.auth_service import AuthService
from app.application.services.document_workflow_service import DocumentWorkflowService
from app.core.database import get_db_session
from app.core.security import decode_access_token
from app.domain.entities.user import UserEntity
from app.domain.entities.enums import UserRole
from app.infrastructure.persistence.repositories import (
    SqlAlchemyAuditLogRepository,
    SqlAlchemyDocumentRepository,
    SqlAlchemyUserRepository,
)
from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService
from app.infrastructure.redis.event_bus import RedisEventBus


bearer_scheme = HTTPBearer(auto_error=True)


def _get_user_repo(session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session=session)


def _get_document_repo(session: AsyncSession) -> SqlAlchemyDocumentRepository:
    return SqlAlchemyDocumentRepository(session=session)


def _get_audit_repo(session: AsyncSession) -> SqlAlchemyAuditLogRepository:
    return SqlAlchemyAuditLogRepository(session=session)


def get_auth_service(session: Annotated[AsyncSession, Depends(get_db_session)]) -> AuthService:
    return AuthService(session=session, user_repository=_get_user_repo(session))


def get_document_workflow_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentWorkflowService:
    return DocumentWorkflowService(
        session=session,
        user_repository=_get_user_repo(session),
        document_repository=_get_document_repo(session),
        audit_repository=_get_audit_repo(session),
        pdf_service=SignaturePdfService(),
        event_bus=RedisEventBus(),
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserEntity:
    token_payload = decode_access_token(credentials.credentials)
    if not token_payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    subject = token_payload.get("sub")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    try:
        user_id = UUID(subject)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token subject") from exc

    user = await _get_user_repo(session).get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(allowed_roles: set[UserRole]):
    async def role_dependency(user: Annotated[UserEntity, Depends(get_current_user)]) -> UserEntity:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return role_dependency


def request_context(request: Request) -> dict[str, str]:
    return {
        "ip_address": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown"),
    }
