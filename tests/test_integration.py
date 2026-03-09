"""
Integration service tests – cover critical security and business logic paths.

Run with:  pytest backend/tests/test_integration.py -v

Dependencies (add to dev requirements):
    pytest==8.3.4
    pytest-asyncio==0.24.0
    pytest-mock==3.14.0
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from jose import jwt

# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures & helpers
# ──────────────────────────────────────────────────────────────────────────────

SHARED_SECRET = "test-integration-secret"
ALGORITHM = "HS256"


def make_token(
    external_user_id: str = "ext-user-1",
    role: str = "ADMIN",
    external_document_id: str = "doc-001",
    document_path: str = "/tmp/test.pdf",
    jti: str | None = None,
    exp_delta_minutes: int = 15,
) -> str:
    """Helper that creates a valid HMAC-signed launch token."""
    jti = jti or str(uuid4())
    payload = {
        "sub": external_user_id,
        "role": role,
        "external_document_id": external_document_id,
        "document_path": document_path,
        "jti": jti,
        "exp": datetime.now(UTC) + timedelta(minutes=exp_delta_minutes),
    }
    return jwt.encode(payload, SHARED_SECRET, algorithm=ALGORITHM)


def make_service(
    *,
    event_bus=None,
    ext_user_repo=None,
    user_repo=None,
    doc_repo=None,
    integration_audit_repo=None,
    callback_audit_repo=None,
    callback_client=None,
):
    """Build an IntegrationService with all dependencies mocked."""
    from app.application.services.integration_service import IntegrationService

    session = AsyncMock()
    session.commit = AsyncMock()

    if event_bus is None:
        event_bus = AsyncMock()
        event_bus.get_json = AsyncMock(return_value=None)
        event_bus.set_json = AsyncMock()

    if ext_user_repo is None:
        ext_user_repo = AsyncMock()

    if user_repo is None:
        user_repo = AsyncMock()

    if doc_repo is None:
        doc_repo = AsyncMock()

    if integration_audit_repo is None:
        audit = AsyncMock()
        audit.create_entry = AsyncMock()
        integration_audit_repo = audit

    if callback_audit_repo is None:
        cb_audit = AsyncMock()
        cb_audit.get_by_idempotency_key = AsyncMock(return_value=None)
        cb_audit.create_record = AsyncMock()
        cb_audit.increment_attempt = AsyncMock()
        callback_audit_repo = cb_audit

    if callback_client is None:
        callback_client = AsyncMock()
        callback_client.send_completion_callback = AsyncMock(return_value=True)

    # Patch settings
    with patch("app.application.services.integration_service.get_settings") as mock_settings:
        settings = MagicMock()
        settings.integration_shared_secret = SHARED_SECRET
        settings.nonce_ttl_seconds = 900
        settings.original_storage_dir = MagicMock()
        settings.allowed_document_path_prefixes = []
        mock_settings.return_value = settings

        svc = IntegrationService(
            session=session,
            user_repository=user_repo,
            document_repository=doc_repo,
            external_user_repository=ext_user_repo,
            integration_audit_repository=integration_audit_repo,
            callback_audit_repository=callback_audit_repo,
            callback_client=callback_client,
            event_bus=event_bus,
        )
        svc._settings = settings  # store so tests can access

    return svc, session


# ──────────────────────────────────────────────────────────────────────────────
# 1. Launch token validation
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_launch_token_returns_context():
    token = make_token()
    svc, _ = make_service()

    ctx = await svc.validate_launch_token(token)

    assert ctx.external_user_id == "ext-user-1"
    assert ctx.role == "ADMIN"
    assert ctx.external_document_id == "doc-001"
    assert ctx.jti is not None


@pytest.mark.asyncio
async def test_expired_launch_token_raises_401():
    from fastapi import HTTPException

    token = make_token(exp_delta_minutes=-5)  # already expired
    svc, _ = make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.validate_launch_token(token)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_tampered_launch_token_raises_401():
    from fastapi import HTTPException

    token = make_token() + "tamper"
    svc, _ = make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.validate_launch_token(token)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_replay_attack_blocked():
    """Using the same token twice must fail on the second attempt."""
    from fastapi import HTTPException

    jti = str(uuid4())
    token = make_token(jti=jti)

    # First call: nonce not yet in Redis → succeeds
    event_bus = AsyncMock()
    event_bus.get_json = AsyncMock(return_value=None)
    event_bus.set_json = AsyncMock()

    svc, _ = make_service(event_bus=event_bus)
    ctx = await svc.validate_launch_token(token)
    assert ctx.jti == jti

    # Second call: nonce now "in Redis" → must raise 401
    event_bus.get_json = AsyncMock(return_value={"used": True})
    svc2, _ = make_service(event_bus=event_bus)

    with pytest.raises(HTTPException) as exc_info:
        await svc2.validate_launch_token(token)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_role_in_token_raises_400():
    from fastapi import HTTPException

    token = make_token(role="SUPERADMIN")
    svc, _ = make_service()

    with pytest.raises(HTTPException) as exc_info:
        await svc.validate_launch_token(token)

    assert exc_info.value.status_code == 400


# ──────────────────────────────────────────────────────────────────────────────
# 2. User resolution + role enforcement
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_not_in_sqlserver_raises_401():
    """If SQL Server has no record for the external_user_id the launch must fail."""
    from fastapi import HTTPException

    ctx = MagicMock()
    ctx.external_user_id = "unknown-user"
    ctx.role = "ADMIN"

    ext_user_repo = AsyncMock()
    ext_user_repo.get_user_by_external_id = AsyncMock(return_value=None)

    svc, _ = make_service(ext_user_repo=ext_user_repo)

    with pytest.raises(HTTPException) as exc_info:
        await svc.resolve_or_create_local_user(ctx)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_role_mismatch_raises_403():
    """Token claims ADMIN but SQL Server says SIGNER → 403."""
    from fastapi import HTTPException
    from app.domain.entities.integration import ExternalUserEntity

    ctx = MagicMock()
    ctx.external_user_id = "user-1"
    ctx.role = "ADMIN"

    ext_user = ExternalUserEntity(
        external_user_id="user-1",
        username="user1",
        role="SIGNER",  # ← mismatch
        email="user1@example.com",
    )
    ext_user_repo = AsyncMock()
    ext_user_repo.get_user_by_external_id = AsyncMock(return_value=ext_user)

    svc, _ = make_service(ext_user_repo=ext_user_repo)

    with pytest.raises(HTTPException) as exc_info:
        await svc.resolve_or_create_local_user(ctx)

    assert exc_info.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# 3. Mapping enforcement
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signer_not_in_mapping_raises_403():
    """Admin cannot assign a signer outside their SQL Server mapping."""
    from fastapi import HTTPException
    from app.domain.entities.user import UserEntity
    from app.domain.entities.enums import UserRole

    # Mapped signer has a different UUID than the one being assigned.
    mapped_signer_id = uuid4()
    assigned_signer_id = uuid4()  # NOT in mapping

    mapped_signer = UserEntity(
        id=mapped_signer_id,
        name="Allowed Signer",
        email="allowed@example.com",
        password_hash="x",
        role=UserRole.SIGNER,
        created_at=datetime.now(UTC),
    )

    ext_user_repo = AsyncMock()
    ext_user_repo.get_mapped_signers_for_admin = AsyncMock(
        return_value=[
            MagicMock(
                external_user_id="ext-signer-1",
                email="allowed@example.com",
            )
        ]
    )

    user_repo = AsyncMock()
    user_repo.get_by_email = AsyncMock(return_value=mapped_signer)

    svc, _ = make_service(ext_user_repo=ext_user_repo, user_repo=user_repo)

    with pytest.raises(HTTPException) as exc_info:
        await svc.validate_signer_in_mapping(
            admin_external_user_id="admin-1",
            signer_local_id=assigned_signer_id,  # not in mapping
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_signer_in_mapping_passes():
    """Admin assigning a mapped signer should succeed without exception."""
    from app.domain.entities.user import UserEntity
    from app.domain.entities.enums import UserRole

    signer_id = uuid4()
    mapped_signer = UserEntity(
        id=signer_id,
        name="Allowed Signer",
        email="allowed@example.com",
        password_hash="x",
        role=UserRole.SIGNER,
        created_at=datetime.now(UTC),
    )

    ext_user_repo = AsyncMock()
    ext_user_repo.get_mapped_signers_for_admin = AsyncMock(
        return_value=[MagicMock(external_user_id="ext-s", email="allowed@example.com")]
    )

    user_repo = AsyncMock()
    user_repo.get_by_email = AsyncMock(return_value=mapped_signer)

    svc, _ = make_service(ext_user_repo=ext_user_repo, user_repo=user_repo)

    # Should not raise.
    await svc.validate_signer_in_mapping(
        admin_external_user_id="admin-1",
        signer_local_id=signer_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. Submit gating
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_blocked_when_unsigned_regions_exist():
    """Submit must be rejected if not all assigned regions are signed."""
    from fastapi import HTTPException
    from app.domain.entities.document import DocumentEntity
    from app.domain.entities.enums import DocumentStatus
    from app.domain.entities.signature_region import SignatureRegionEntity
    from app.domain.value_objects.signature_box import SignatureBox

    signer_id = uuid4()
    doc_id = uuid4()

    unsigned_region = SignatureRegionEntity(
        id=uuid4(),
        document_id=doc_id,
        box=SignatureBox(page_number=1, x=0.1, y=0.1, width=0.2, height=0.05),
        assigned_to=signer_id,
        signed=False,  # ← not signed
        signed_at=None,
        signature_image_path=None,
    )

    document = DocumentEntity(
        id=doc_id,
        title="Test",
        uploaded_by=uuid4(),
        original_path="/tmp/test.pdf",
        final_path=None,
        final_hash=None,
        total_pages=1,
        status=DocumentStatus.PENDING,
        created_at=datetime.now(UTC),
        regions=[unsigned_region],
    )

    doc_repo = AsyncMock()
    doc_repo.get_document_by_id = AsyncMock(return_value=document)

    svc, _ = make_service(doc_repo=doc_repo)

    with pytest.raises(HTTPException) as exc_info:
        await svc.submit_document(
            document_id=doc_id,
            signer_local_id=signer_id,
            signer_external_user_id="ext-signer-1",
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_submit_succeeds_when_all_signed():
    """Submit should return success when all regions are signed."""
    from app.domain.entities.document import DocumentEntity
    from app.domain.entities.enums import DocumentStatus
    from app.domain.entities.signature_region import SignatureRegionEntity
    from app.domain.value_objects.signature_box import SignatureBox

    signer_id = uuid4()
    doc_id = uuid4()

    signed_region = SignatureRegionEntity(
        id=uuid4(),
        document_id=doc_id,
        box=SignatureBox(page_number=1, x=0.1, y=0.1, width=0.2, height=0.05),
        assigned_to=signer_id,
        signed=True,
        signed_at=datetime.now(UTC),
        signature_image_path="/tmp/sig.png",
    )

    document = DocumentEntity(
        id=doc_id,
        title="Test",
        uploaded_by=uuid4(),
        original_path="/tmp/test.pdf",
        final_path="/tmp/signed.pdf",
        final_hash="abc123",
        total_pages=1,
        status=DocumentStatus.COMPLETED,
        created_at=datetime.now(UTC),
        regions=[signed_region],
    )

    doc_repo = AsyncMock()
    doc_repo.get_document_by_id = AsyncMock(return_value=document)
    doc_repo.get_external_document_id = AsyncMock(return_value="ext-doc-1")

    svc, _ = make_service(doc_repo=doc_repo)

    result = await svc.submit_document(
        document_id=doc_id,
        signer_local_id=signer_id,
        signer_external_user_id="ext-signer-1",
    )

    assert result["status"] == "submitted"
    assert result["signed_regions"] == 1
    assert result["total_regions"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 5. Callback retry / idempotency
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_skipped_when_already_succeeded():
    """Idempotent callback – if already succeeded, no HTTP request is made."""
    from app.infrastructure.external_api.callback_client import ExternalCallbackClient

    existing_record = MagicMock()
    existing_record.succeeded = True

    callback_audit_repo = AsyncMock()
    callback_audit_repo.get_by_idempotency_key = AsyncMock(return_value=existing_record)

    client = ExternalCallbackClient(
        base_url="http://external.example.com",
        auth_secret="secret",
        callback_audit_repo=callback_audit_repo,
    )

    result = await client.send_completion_callback(
        external_document_id="doc-1",
        external_user_id="user-1",
        internal_document_id=uuid4(),
        document_hash="abc",
    )

    assert result is True
    # No record was created (no new attempt needed).
    callback_audit_repo.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_callback_retries_on_failure():
    """Failed callback must be retried up to MAX_ATTEMPTS times."""
    import httpx
    from app.infrastructure.external_api.callback_client import ExternalCallbackClient, MAX_ATTEMPTS

    new_record = MagicMock()
    new_record.id = uuid4()
    new_record.succeeded = False

    callback_audit_repo = AsyncMock()
    callback_audit_repo.get_by_idempotency_key = AsyncMock(return_value=None)
    callback_audit_repo.create_record = AsyncMock(return_value=new_record)
    callback_audit_repo.increment_attempt = AsyncMock()

    client = ExternalCallbackClient(
        base_url="http://external.example.com",
        auth_secret="secret",
        callback_audit_repo=callback_audit_repo,
    )

    # All HTTP calls raise an error.
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.send_completion_callback(
                external_document_id="doc-1",
                external_user_id="user-1",
                internal_document_id=uuid4(),
                document_hash="abc",
            )

    assert result is False
    assert callback_audit_repo.increment_attempt.call_count == MAX_ATTEMPTS
