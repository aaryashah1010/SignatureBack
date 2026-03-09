"""
Integration router – thin controller; all business logic lives in IntegrationService.

Endpoints:
    POST /integration/launch
        Validates a signed launch token from the external software, auto-logs
        in the user, bootstraps the document, and returns a JWT + redirect route.

    GET  /integration/documents/{document_id}/mapped-signers
        Returns only the signers that the current admin is allowed to assign
        (SQL Server mapping).

    GET  /integration/documents/{document_id}/progress
        Returns the signer's signing progress counter.

    POST /documents/{document_id}/submit
        SIGNER-only; gated on all assigned regions being signed; triggers callback.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.dependencies import get_current_user, require_role
from app.core.security import create_access_token
from app.domain.entities.enums import UserRole
from app.domain.entities.user import UserEntity
from app.presentation.controllers.schemas import (
    LaunchRequest,
    LaunchResponse,
    MappedSignerResponse,
    SigningProgressResponse,
    SubmitDocumentResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integration", tags=["Integration"])
# Submit lives under /documents to match the existing document URL hierarchy.
submit_router = APIRouter(prefix="/documents", tags=["Documents"])


# ── Dependency factory ────────────────────────────────────────────────────────


def _get_sqlserver_client():
    """Return a module-level singleton SqlServerClient so the connection pool
    is shared across all requests instead of being recreated on every call."""
    from app.core.config import get_settings
    from app.infrastructure.sqlserver.sqlserver_client import SqlServerClient

    global _SS_CLIENT  # noqa: PLW0603
    if _SS_CLIENT is None:
        url = get_settings().sqlserver_url
        _SS_CLIENT = SqlServerClient(url) if url else None
    return _SS_CLIENT


_SS_CLIENT = None  # module-level singleton


def _get_integration_service(
    session: AsyncSession,
    request: Request,
) -> "IntegrationService":  # noqa: F821 – imported lazily to avoid circular imports
    from app.application.services.integration_service import IntegrationService
    from app.core.config import get_settings
    from app.infrastructure.external_api.callback_client import ExternalCallbackClient, NullCallbackClient
    from app.infrastructure.persistence.repositories import (
        SqlAlchemyCallbackAuditRepository,
        SqlAlchemyDocumentRepository,
        SqlAlchemyIntegrationAuditRepository,
        SqlAlchemyUserRepository,
    )
    from app.infrastructure.redis.event_bus import RedisEventBus
    from app.infrastructure.sqlserver.sqlserver_repository import (
        NullExternalUserRepository,
        SqlServerExternalUserRepository,
    )

    settings = get_settings()
    correlation_id = getattr(request.state, "request_id", "")

    # SQL Server – reuse the module-level singleton so the connection pool is
    # shared across requests (avoids a 15-second reconnect on every call).
    ss_client = _get_sqlserver_client()
    if ss_client is not None:
        ext_user_repo = SqlServerExternalUserRepository(ss_client)
    else:
        ext_user_repo = NullExternalUserRepository()

    callback_audit_repo = SqlAlchemyCallbackAuditRepository(session)

    # Callback client – falls back to NullCallbackClient when URL is not configured.
    if settings.external_api_base_url:
        cb_client = ExternalCallbackClient(
            base_url=settings.external_api_base_url,
            auth_secret=settings.external_api_auth_secret,
            callback_audit_repo=callback_audit_repo,
        )
    else:
        cb_client = NullCallbackClient()

    return IntegrationService(
        session=session,
        user_repository=SqlAlchemyUserRepository(session),
        document_repository=SqlAlchemyDocumentRepository(session),
        external_user_repository=ext_user_repo,
        integration_audit_repository=SqlAlchemyIntegrationAuditRepository(session),
        callback_audit_repository=callback_audit_repo,
        callback_client=cb_client,
        event_bus=RedisEventBus(),
        correlation_id=correlation_id,
    )


def get_integration_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
):
    return _get_integration_service(session, request)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/launch", response_model=LaunchResponse, status_code=status.HTTP_200_OK)
async def launch(
    payload: LaunchRequest,
    service=Depends(get_integration_service),
) -> LaunchResponse:
    """Exchange a signed launch token for an internal JWT session.

    The external software generates a short-lived HMAC-HS256 token and either
    posts it here directly or has the user's browser relay it via the /launch
    frontend route.

    Returns the JWT and the `next_route` the frontend should navigate to.
    """
    ctx = await service.validate_launch_token(payload.token)
    local_user = await service.resolve_or_create_local_user(ctx)

    document = None
    if ctx.role == "ADMIN":
        document = await service.bootstrap_external_document(ctx, local_user.id)
    elif ctx.role == "SIGNER":
        # Admin may have already bootstrapped this document — find it by external ID.
        document = await service._doc_repo.get_by_external_document_id(ctx.external_document_id)

    # Issue internal JWT carrying the standard claims.
    access_token = create_access_token(
        subject=str(local_user.id),
        extra_claims={"role": local_user.role.value},
    )

    # Derive the correct deep-link route for the frontend router.
    if ctx.role == "ADMIN" and document:
        next_route = f"/admin/documents/{document.id}/regions"
    elif ctx.role == "SIGNER" and document:
        next_route = f"/signer/documents/{document.id}/sign"
    elif ctx.role == "SIGNER":
        next_route = "/signer"  # Admin hasn't launched yet; show pending list
    else:
        next_route = "/admin"

    return LaunchResponse(
        access_token=access_token,
        role=local_user.role.value,
        next_route=next_route,
        document_id=document.id if document else None,
        user=UserResponse(
            id=local_user.id,
            name=local_user.name,
            email=local_user.email,
            role=local_user.role,
            created_at=local_user.created_at,
        ),
    )


@router.get(
    "/documents/{document_id}/mapped-signers",
    response_model=list[MappedSignerResponse],
)
async def get_mapped_signers(
    document_id: UUID,
    request: Request,
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
    service=Depends(get_integration_service),
) -> list[MappedSignerResponse]:
    """Return signers the admin is allowed to assign.

    ESign flow: returns the single client from the ESignRequests row
                (document.external_document_id is an integer ESignRequestID).
    Legacy flow: returns all clients mapped to the admin in SQL Server.
    """
    from app.infrastructure.sqlserver.sqlserver_repository import NullExternalUserRepository

    # ── ESign flow: single signer from ESignRequests ──────────────────────────
    document = await service._doc_repo.get_document_by_id(document_id)
    if document and document.external_document_id:
        try:
            esign_request_id = int(document.external_document_id)
            signer = await service.get_esign_client_for_document(esign_request_id)
            if signer:
                return [MappedSignerResponse(id=signer.id, name=signer.name, email=signer.email)]
        except (TypeError, ValueError):
            pass  # not an integer ESignRequestID → fall through to legacy path

    # ── Legacy flow: mapping from CAPUserClientMapping ────────────────────────
    if isinstance(service._ext_user_repo, NullExternalUserRepository):
        signers = await service._user_repo.list_signers()
    else:
        admin_email = admin_user.email
        if admin_email.endswith("@external.local"):
            admin_external_id = admin_email.removesuffix("@external.local")
        else:
            found_id = await service._ext_user_repo.get_login_detail_id_by_email(admin_email)
            admin_external_id = str(found_id) if found_id else None

        signers = []
        if admin_external_id:
            signers = await service.get_allowed_signers_for_admin(
                admin_external_user_id=admin_external_id
            )

    return [MappedSignerResponse(id=s.id, name=s.name, email=s.email) for s in signers]


@router.post(
    "/documents/{document_id}/notify-prepared",
    status_code=status.HTTP_200_OK,
)
async def notify_prepared(
    document_id: UUID,
    admin_user: Annotated[UserEntity, Depends(require_role({UserRole.ADMIN}))],
    service=Depends(get_integration_service),
) -> dict:
    """Notify CpaDesk that the document is prepared for signing (Status=Pending).

    Called by the admin after saving signature regions.
    Sends the original PDF to CpaDesk's ProcessESignDocumentPrepared endpoint.
    Returns {"notified": true/false} — non-critical; frontend should not block on failure.
    """
    ok = await service.notify_document_prepared(document_id)
    return {"notified": ok}


@router.get(
    "/documents/{document_id}/progress",
    response_model=SigningProgressResponse,
)
async def get_signing_progress(
    document_id: UUID,
    signer_user: Annotated[UserEntity, Depends(require_role({UserRole.SIGNER}))],
    service=Depends(get_integration_service),
) -> SigningProgressResponse:
    """Return the current signer's progress on the document."""
    document = await service._doc_repo.get_document_by_id(document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    signer_regions = [r for r in document.regions if r.assigned_to == signer_user.id]
    assigned_total = len(signer_regions)
    assigned_signed = sum(1 for r in signer_regions if r.signed)

    return SigningProgressResponse(
        document_id=document_id,
        assigned_total=assigned_total,
        assigned_signed=assigned_signed,
        can_submit=(assigned_total > 0 and assigned_signed == assigned_total),
    )


# ── Submit endpoint (separate router, mounted under /documents) ────────────────


@submit_router.post("/{document_id}/submit", response_model=SubmitDocumentResponse)
async def submit_document(
    document_id: UUID,
    request: Request,
    signer_user: Annotated[UserEntity, Depends(require_role({UserRole.SIGNER}))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SubmitDocumentResponse:
    """Submit a fully-signed document and trigger the external completion callback.

    Rules:
    - Only the SIGNER role may call this.
    - All regions assigned to this signer must be signed.
    - Idempotent: repeated calls return success without re-triggering callback.
    """
    service = _get_integration_service(session, request)

    # Derive external_user_id from the signer's local email (inverse of the
    # upsert logic).  If the user was created via integration launch, the email
    # is `<external_user_id>@external.local`; otherwise it's a real email that
    # won't match any external record and the callback is skipped gracefully.
    email = signer_user.email
    external_user_id: str | None = None
    if email.endswith("@external.local"):
        external_user_id = email.removesuffix("@external.local")

    result = await service.submit_document(
        document_id=document_id,
        signer_local_id=signer_user.id,
        signer_external_user_id=external_user_id,
    )

    return SubmitDocumentResponse(
        document_id=document_id,
        status=result["status"],
        signed_regions=result["signed_regions"],
        total_regions=result["total_regions"],
        callback_triggered=result["callback_triggered"],
    )
