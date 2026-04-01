"""
Integration application service.

Orchestrates all external-integration use-cases:

    1. validate_launch_token   – verify HMAC-signed token, consume nonce
    2. resolve_or_create_user  – upsert local user from SQL Server identity
    3. bootstrap_document      – copy & link external document to local workflow
    4. get_allowed_signers     – mapping-constrained signer list
    5. validate_signer_in_mapping – guard before region creation
    6. submit_document         – gated submit + external callback trigger

No business logic lives in the router; all domain rules are enforced here.
"""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.domain.entities.enums import UserRole
from app.domain.entities.integration import ExternalUserEntity, LaunchContextEntity
from app.domain.entities.user import UserEntity
from app.domain.repositories.document_repository import DocumentRepository
from app.domain.repositories.integration_repository import (
    CallbackAuditRepository,
    ExternalUserRepository,
    IntegrationAuditRepository,
)
from app.domain.repositories.user_repository import UserRepository
from app.infrastructure.external_api.callback_client import ExternalCallbackClient
from app.infrastructure.redis.event_bus import RedisEventBus

logger = logging.getLogger(__name__)


# ── Path encryption / decryption (CpaDesk algorithm) ─────────────────────────
#
# Algorithm: Triple DES (3DES), ECB mode, PKCS7 padding
# Key:       first 24 bytes of SHA-256("CPADesk@2023")
# Matches the C# EncryptDecryptValue class used by CpaDesk.

_CPADEST_SECRET = "CPADesk@2023"
_ESIGN_SECRET   = "ZXNpZ24ubXljcGFkZXNrLmNvbUAyMDI2"


def _tdes_key(secret: str = _CPADEST_SECRET) -> bytes:
    import hashlib
    return hashlib.sha256(secret.encode("utf-8")).digest()[:24]


def _decrypt_esign_token(encrypted: str) -> str:
    """Decrypt ESignRequests.EsignToken using the ESign 3DES key.

    Mirrors the C# Decrypt() method that uses commentKey = _ESIGN_SECRET.
    Returns the decrypted plaintext, or raises ValueError on failure.
    """
    import base64
    from Crypto.Cipher import DES3

    data = base64.b64decode(encrypted)
    cipher = DES3.new(_tdes_key(_ESIGN_SECRET), DES3.MODE_ECB)
    decrypted = cipher.decrypt(data)
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode("utf-8")


def decrypt_path(encrypted_path: str, _decryption_key: str = "") -> str:
    """Decrypt a Base64-encoded 3DES-ECB ciphertext produced by CpaDesk.

    The decryption key is derived from the hardcoded CpaDesk secret;
    the _decryption_key argument is accepted for interface compatibility
    but is not used.
    """
    import base64
    from Crypto.Cipher import DES3

    try:
        data = base64.b64decode(encrypted_path)
        cipher = DES3.new(_tdes_key(), DES3.MODE_ECB)
        decrypted = cipher.decrypt(data)
        # Remove PKCS7 padding
        pad_len = decrypted[-1]
        return decrypted[:-pad_len].decode("utf-8")
    except Exception as exc:
        logger.warning("decrypt_path failed (%s) – treating value as plain text", exc)
        return encrypted_path


def encrypt_path(plain_path: str, _encryption_key: str = "") -> str:
    """Encrypt a plain path with 3DES-ECB so it can be stored back in DocumentMaster."""
    import base64
    from Crypto.Cipher import DES3

    if not plain_path:
        return plain_path

    try:
        data = plain_path.encode("utf-8")
        # PKCS7 pad to 8-byte boundary
        pad_len = 8 - (len(data) % 8)
        data += bytes([pad_len] * pad_len)
        cipher = DES3.new(_tdes_key(), DES3.MODE_ECB)
        return base64.b64encode(cipher.encrypt(data)).decode("utf-8")
    except Exception as exc:
        logger.warning("encrypt_path failed (%s) – storing plain text", exc)
        return plain_path

# Role values the external system (CpaDesk) sends in the launch token.
_ROLE_MAP: dict[str, str] = {
    "admin":     "ADMIN",
    "user":      "SIGNER",
    "client":    "SIGNER",
    "cpauser":   "ADMIN",
    "cpaclient": "SIGNER",
    "cpaadmin":  "ADMIN",   # CPAAdmin (RoleID 2) from ESignRequests.AssignedRole
    # Pass-through
    "ADMIN":  "ADMIN",
    "SIGNER": "SIGNER",
}


def _normalise_role(raw: str) -> str:
    """Map CpaDesk role labels to internal role strings ('ADMIN' / 'SIGNER')."""
    return _ROLE_MAP.get(raw, _ROLE_MAP.get(raw.lower(), raw.upper()))


class IntegrationService:
    """Thin orchestration layer – delegates persistence to repositories, logic to domain."""

    def __init__(
        self,
        session: AsyncSession,
        user_repository: UserRepository,
        document_repository: DocumentRepository,
        external_user_repository: ExternalUserRepository,
        integration_audit_repository: IntegrationAuditRepository,
        callback_audit_repository: CallbackAuditRepository,
        callback_client: ExternalCallbackClient,
        event_bus: RedisEventBus,
        correlation_id: str = "",
    ) -> None:
        self._session = session
        self._settings = get_settings()
        self._user_repo = user_repository
        self._doc_repo = document_repository
        self._ext_user_repo = external_user_repository
        self._integration_audit_repo = integration_audit_repository
        self._callback_audit_repo = callback_audit_repository
        self._callback_client = callback_client
        self._event_bus = event_bus
        self._correlation_id = correlation_id or str(uuid4())

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Launch token validation
    # ─────────────────────────────────────────────────────────────────────────

    async def validate_launch_token(self, esign_request_guid: str, raw_role: str = "") -> LaunchContextEntity:
        """Validate a launch request using EsignRequestGuid.

        New flow (replaces JWT-based launch):
        1. Receive EsignRequestGuid as the 'token' query/body param.
        2. Fetch ESignRequests row by EsignRequestGuid from SQL Server.
        3. Decrypt EsignToken (3DES-ECB) and verify it matches the incoming guid.
        4. Build LaunchContextEntity from the row — role, user IDs, file path, etc.

        No JWT, no nonce, no shared-secret needed.
        """
        # 1. Fetch row by guid
        esign_row = await self._ext_user_repo.get_esign_request_by_guid(esign_request_guid)
        if not esign_row:
            await self._audit("ESIGN_GUID_NOT_FOUND", success=False,
                              details=f"guid={esign_request_guid}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="ESign request not found or inactive")

        # 2. Decrypt EsignToken and compare with incoming guid
        esign_token_enc: str = esign_row.get("EsignToken") or ""
        if not esign_token_enc:
            await self._audit("ESIGN_TOKEN_MISSING", success=False,
                              details=f"guid={esign_request_guid}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="ESign token not set for this request")

        try:
            decrypted_guid = _decrypt_esign_token(esign_token_enc)
        except Exception as exc:
            await self._audit("ESIGN_TOKEN_DECRYPT_FAILED", success=False, details=str(exc))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="ESign token could not be decrypted") from exc

        if decrypted_guid.strip() != esign_request_guid.strip():
            await self._audit("ESIGN_TOKEN_MISMATCH", success=False,
                              details=f"guid={esign_request_guid}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="ESign token does not match request guid")

        # 3. Build context from the row
        esign_request_id: int = esign_row["ESignRequestID"]
        # Role: prefer the param sent by CpaDesk; fall back to DB AssignedRole
        assigned_role: str = raw_role.strip() or (esign_row.get("AssignedRole") or "").strip()
        role = _normalise_role(assigned_role)

        if role not in ("ADMIN", "SIGNER"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Unrecognised AssignedRole '{assigned_role}' in ESignRequests")

        if role == "ADMIN":
            verify_login_id: int | None = esign_row.get("AssignedByLoginID")
        else:
            verify_login_id = esign_row.get("ClientLoginDetailID")

        if not verify_login_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="ESign request has no valid login ID")

        document_path: str = esign_row.get("FileURL") or ""
        external_document_id = str(esign_request_id)

        await self._audit("ESIGN_GUID_VALID",
                          external_user_id=str(verify_login_id),
                          external_document_id=external_document_id)

        return LaunchContextEntity(
            external_user_id=str(verify_login_id),
            role=role,
            external_document_id=external_document_id,
            document_path=document_path,
            login_token="",
            tenant_id=None,
            jti=esign_request_guid,
            expires_at=datetime.now(UTC),
            esign_request_id=esign_request_id,
            esign_client_id=esign_row.get("ClientID"),
            esign_client_login_detail_id=esign_row.get("ClientLoginDetailID"),
            esign_client_name=esign_row.get("ClientName"),
            esign_client_email=esign_row.get("ClientEmail"),
            esign_assigned_by_login_id=esign_row.get("AssignedByLoginID"),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 2. User resolution
    # ─────────────────────────────────────────────────────────────────────────

    async def resolve_or_create_local_user(self, ctx: LaunchContextEntity) -> UserEntity:
        """Upsert a local user record from the SQL Server identity.

        ESign flow:  user info is taken directly from the ESignRequests row
                     already embedded in ctx (no extra SQL Server lookup needed).
        Legacy flow: user info is looked up via LoginDetailID / ClientID.
        """
        # ── ESign flow: use data already in the context ───────────────────────
        if ctx.esign_request_id is not None:
            if ctx.role == "SIGNER":
                # CpaClient: use ClientEmail + ClientName from ESignRequests
                local_email = (
                    ctx.esign_client_email
                    or f"{ctx.esign_client_login_detail_id}@external.local"
                )
                local_name = ctx.esign_client_name or "Client"
            else:
                # CpaUser (ADMIN): resolved via external_user_id (AssignedByLoginID)
                ext_user = await self._ext_user_repo.get_user_by_external_id(ctx.external_user_id)
                if not ext_user:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                        detail="External user not found in directory")
                local_email = ext_user.email or f"{ctx.external_user_id}@external.local"
                local_name = ext_user.full_name or ext_user.username

            local_role = UserRole(ctx.role)
            local_user = await self._user_repo.get_by_email(local_email)
            if not local_user:
                local_user = await self._user_repo.create(
                    name=local_name,
                    email=local_email,
                    password_hash=hash_password(str(uuid4())),
                    role=local_role,
                )
                await self._session.commit()
                await self._audit("LOCAL_USER_CREATED", external_user_id=ctx.external_user_id,
                                  details=f"local_id={local_user.id}")
            else:
                await self._audit("LOCAL_USER_RESOLVED", external_user_id=ctx.external_user_id,
                                  details=f"local_id={local_user.id}")
            return local_user

        # ── Legacy flow: look up via SQL Server ───────────────────────────────
        # For signers use Client table directly to avoid conflicts where
        # ClientID == LoginDetailID but belong to different people.
        if ctx.role == "SIGNER":
            ext_user = await self._ext_user_repo.get_client_by_id(ctx.external_user_id)
        else:
            ext_user = await self._ext_user_repo.get_user_by_external_id(ctx.external_user_id)
        if not ext_user:
            await self._audit("USER_NOT_FOUND_IN_SQLSERVER", success=False,
                              external_user_id=ctx.external_user_id)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="External user not found in directory")

        if ctx.login_token and ext_user.login_token:
            if ctx.login_token != ext_user.login_token:
                await self._audit("TOKEN_MISMATCH", success=False,
                                  external_user_id=ctx.external_user_id)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                    detail="Token does not match user record")

        if ext_user.role != ctx.role:
            await self._audit("ROLE_MISMATCH", success=False,
                              external_user_id=ctx.external_user_id,
                              details=f"token_role={ctx.role} directory_role={ext_user.role}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Role mismatch between launch token and user directory")

        local_email = ext_user.email or f"{ext_user.external_user_id}@external.local"
        local_role = UserRole(ext_user.role)
        local_user = await self._user_repo.get_by_email(local_email)
        if not local_user:
            local_user = await self._user_repo.create(
                name=ext_user.full_name or ext_user.username,
                email=local_email,
                password_hash=hash_password(str(uuid4())),
                role=local_role,
            )
            await self._session.commit()
            await self._audit("LOCAL_USER_CREATED", external_user_id=ctx.external_user_id,
                              details=f"local_id={local_user.id}")
        else:
            await self._audit("LOCAL_USER_RESOLVED", external_user_id=ctx.external_user_id,
                              details=f"local_id={local_user.id}")
        return local_user

    # ─────────────────────────────────────────────────────────────────────────
    # 3. External document bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    async def bootstrap_external_document(
        self,
        ctx: LaunchContextEntity,
        local_admin_id: UUID,
    ):
        """Create or link a local workflow document for the externally-supplied PDF.

        Idempotent: returns the existing local document if already linked.
        """
        from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService

        # Idempotency – return existing record if already bootstrapped.
        existing = await self._doc_repo.get_by_external_document_id(ctx.external_document_id)
        if existing:
            await self._audit(
                "DOCUMENT_ALREADY_LINKED",
                external_user_id=ctx.external_user_id,
                external_document_id=ctx.external_document_id,
                document_id=existing.id,
            )
            return existing

        # Resolve path: token-supplied takes priority; SQL Server is the fallback.
        raw_path = ctx.document_path or await self._ext_user_repo.get_external_document_path(
            ctx.external_document_id
        )
        if not raw_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No document path available for the given external_document_id",
            )

        # Decrypt the path if CpaDesk sends it encrypted.
        # decryption_key comes from the launch token (ctx.decryption_key).
        # When the algorithm is confirmed, replace decrypt_path() body in this file.
        decryption_key = getattr(ctx, "decryption_key", "") or ""
        source_path_str = decrypt_path(raw_path, decryption_key)

        is_url = source_path_str.startswith("http://") or source_path_str.startswith("https://")
        source_path = Path(source_path_str).resolve()
        if not is_url:
            self._validate_document_path(source_path)

        # Copy or download PDF to local storage with a new UUID name.
        safe_name = f"ext_{uuid4()}.pdf"
        dest_path = self._settings.original_storage_dir / safe_name

        if is_url:
            # Remote URL – download via httpx
            await self._download_pdf(source_path_str, dest_path, ctx.external_document_id)
        else:
            # Local file path
            if not source_path.exists() or not source_path.is_file():
                await self._audit(
                    "DOCUMENT_PATH_NOT_FOUND",
                    success=False,
                    external_document_id=ctx.external_document_id,
                    details=str(source_path),
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="External document path does not exist or is not a file",
                )
            if source_path.suffix.lower() != ".pdf":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="External document must be a PDF file",
                )
            shutil.copy2(source_path, dest_path)

        total_pages = SignaturePdfService().get_page_count(dest_path)
        if is_url:
            title = source_path_str.rstrip("/").split("/")[-1].rsplit(".", 1)[0] or f"Document {ctx.external_document_id}"
        else:
            title = source_path.stem or f"Document {ctx.external_document_id}"

        document = await self._doc_repo.create_document(
            title=title,
            uploaded_by=local_admin_id,
            original_path=str(dest_path),
            total_pages=total_pages,
            external_document_id=ctx.external_document_id,
            # Store the DECRYPTED path so submit_document can write the signed PDF back.
            external_path=source_path_str,
        )
        await self._session.commit()

        await self._audit(
            "DOCUMENT_LINKED",
            external_user_id=ctx.external_user_id,
            external_document_id=ctx.external_document_id,
            document_id=document.id,
            details=f"pages={total_pages}",
        )
        return document

    async def _download_pdf(self, url: str, dest_path: Path, external_document_id: str) -> None:
        """Download a PDF from a remote URL into dest_path."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Remote document is not a PDF file",
                    )
                dest_path.write_bytes(response.content)
                logger.info("Downloaded PDF from %s -> %s", url, dest_path)
        except httpx.HTTPStatusError as exc:
            await self._audit(
                "DOCUMENT_DOWNLOAD_FAILED",
                success=False,
                external_document_id=external_document_id,
                details=f"HTTP {exc.response.status_code} from {url}",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to download external document: HTTP {exc.response.status_code}",
            ) from exc
        except Exception as exc:
            await self._audit(
                "DOCUMENT_DOWNLOAD_FAILED",
                success=False,
                external_document_id=external_document_id,
                details=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to download external document",
            ) from exc

    def _validate_document_path(self, path: Path) -> None:
        """Block path-traversal attacks and enforce the configured allowlist."""
        path_str = str(path)

        # Guard against null bytes and traversal components.
        if "\x00" in path_str or any(part == ".." for part in path.parts):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Document path contains invalid characters",
            )

        allowed_prefixes = self._settings.allowed_document_path_prefixes
        if allowed_prefixes and not any(path_str.startswith(p) for p in allowed_prefixes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Document path is outside the allowed locations",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Mapping-constrained signer list
    # ─────────────────────────────────────────────────────────────────────────

    async def get_allowed_signers_for_admin(
        self,
        admin_external_user_id: str,
    ) -> list[UserEntity]:
        """Return only the local users mapped to this admin in SQL Server."""
        ext_signers = await self._ext_user_repo.get_mapped_signers_for_admin(admin_external_user_id)
        await self._audit(
            "MAPPING_FETCH",
            external_user_id=admin_external_user_id,
            details=f"mapped_count={len(ext_signers)}",
        )

        local_signers: list[UserEntity] = []
        for ext_signer in ext_signers:
            local_email = ext_signer.email or f"{ext_signer.external_user_id}@external.local"
            local_user = await self._user_repo.get_by_email(local_email)
            if not local_user:
                # Signer hasn't launched yet — create a placeholder local account
                # so the admin can assign regions to them now.
                placeholder_hash = hash_password(str(uuid4()))
                local_user = await self._user_repo.create(
                    name=ext_signer.full_name or ext_signer.username,
                    email=local_email,
                    password_hash=placeholder_hash,
                    role=UserRole.SIGNER,
                )
                await self._session.commit()
            local_signers.append(local_user)

        return local_signers

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Mapping enforcement (server-side guard before region creation)
    # ─────────────────────────────────────────────────────────────────────────

    async def validate_signer_in_mapping(
        self,
        admin_external_user_id: str,
        signer_local_id: UUID,
    ) -> None:
        """Raise HTTP 403 if the signer is not in the admin's SQL Server mapping."""
        allowed = await self.get_allowed_signers_for_admin(admin_external_user_id)
        allowed_ids = {u.id for u in allowed}
        if signer_local_id not in allowed_ids:
            await self._audit(
                "MAPPING_VIOLATION",
                success=False,
                external_user_id=admin_external_user_id,
                details=f"rejected_signer={signer_local_id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Signer is not in your allowed client mapping",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Document submission
    # ─────────────────────────────────────────────────────────────────────────

    async def submit_document(
        self,
        document_id: UUID,
        signer_local_id: UUID,
        signer_external_user_id: str | None,
    ) -> dict:
        """Gate the SIGNER's submit action on all assigned regions being signed.

        Triggers the external callback as a background task (fire-and-forget
        with internal retry; the signer's response is not delayed).
        """
        document = await self._doc_repo.get_document_by_id(document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        signer_regions = [r for r in document.regions if r.assigned_to == signer_local_id]
        if not signer_regions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No signature regions are assigned to you for this document",
            )

        assigned_total = len(signer_regions)
        assigned_signed = sum(1 for r in signer_regions if r.signed)

        if assigned_signed < assigned_total:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot submit: {assigned_signed} of {assigned_total} required regions are signed",
            )

        external_doc_id = await self._doc_repo.get_external_document_id(document_id)
        callback_triggered = bool(
            external_doc_id and signer_external_user_id and self._callback_client
        )

        # ── ESign submit callback to CpaDesk ──────────────────────────────────
        esign_request_id: int | None = None
        try:
            esign_request_id = int(external_doc_id) if external_doc_id else None
        except (TypeError, ValueError):
            pass

        # Mark document as Completed in the database immediately.
        from sqlalchemy import update as _sa_update
        from app.infrastructure.persistence.models import DocumentModel
        await self._session.execute(
            _sa_update(DocumentModel)
            .where(DocumentModel.id == document_id)
            .values(status="Completed")
        )

        # Fire external callbacks in the background so the signer's response is
        # not delayed by slow / unreachable CpaDesk endpoints.
        def _log_task_error(task: "asyncio.Task") -> None:  # noqa: F821
            if not task.cancelled() and task.exception():
                logger.error("Background callback task failed: %s", task.exception())

        import asyncio as _asyncio
        if esign_request_id:
            # Required flow:
            # - Admin region-save -> ProcessESignDocumentPrepared (already sent by notify_document_prepared)
            # - Signer submit     -> ProcessESignCompletion
            t = _asyncio.create_task(self._send_esign_completion(esign_request_id=esign_request_id, document=document))
            t.add_done_callback(_log_task_error)
        else:
            t = _asyncio.create_task(self._writeback_signed_pdf(document))
            t.add_done_callback(_log_task_error)

        await self._audit(
            "DOCUMENT_SUBMITTED",
            external_user_id=signer_external_user_id,
            document_id=document_id,
            external_document_id=external_doc_id,
            details=f"signed={assigned_signed}/{assigned_total} callback_triggered={callback_triggered}",
        )
        await self._session.commit()

        return {
            "document_id": str(document_id),
            "status": "submitted",
            "signed_regions": assigned_signed,
            "total_regions": assigned_total,
            "callback_triggered": callback_triggered,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def get_esign_client_for_document(self, esign_request_id: int) -> "UserEntity | None":
        """Return the local user for the ESign client, creating a placeholder if needed."""
        esign_row = await self._ext_user_repo.get_esign_request(esign_request_id)
        if not esign_row:
            return None

        client_name: str = (esign_row.get("ClientName") or "Client").strip()
        client_email: str | None = esign_row.get("ClientEmail") or None
        client_login_detail_id = esign_row.get("ClientLoginDetailID")

        local_email = client_email or f"{client_login_detail_id}@external.local"
        local_user = await self._user_repo.get_by_email(local_email)
        if not local_user:
            local_user = await self._user_repo.create(
                name=client_name,
                email=local_email,
                password_hash=hash_password(str(uuid4())),
                role=UserRole.SIGNER,
            )
            await self._session.commit()
        return local_user

    async def notify_document_prepared(self, document_id: UUID) -> bool:
        """Send the original PDF to CpaDesk with Status='Pending'.

        Called by the admin after saving signature regions so CpaDesk knows the
        document is ready for signing.  Uses the original (unsigned) PDF.
        """
        document = await self._doc_repo.get_document_by_id(document_id)
        if not document:
            return False

        external_doc_id = await self._doc_repo.get_external_document_id(document_id)
        try:
            esign_request_id = int(external_doc_id) if external_doc_id else None
        except (TypeError, ValueError):
            esign_request_id = None

        if not esign_request_id:
            logger.debug("notify_document_prepared: not an ESign document (%s)", document_id)
            return False

        original_path = Path(document.original_path) if document.original_path else None
        return await self._send_esign_document_prepared(
            document=document,
            esign_request_id=esign_request_id,
            status="Pending",
            pdf_path=original_path,
        )

    async def _get_esign_base_url(self, esign_request_id: int) -> str | None:
        """Extract the base URL dynamically from the decrypted FileURL in ESignRequests.

        This makes callbacks work regardless of whether the client is using
        ngrok, a test server, or production — no hardcoded config needed.
        """
        from urllib.parse import urlparse
        row = await self._ext_user_repo.get_esign_request(esign_request_id)
        if not row:
            return self._settings.external_api_base_url or None
        encrypted_url = row.get("FileURL") or ""
        if not encrypted_url:
            return self._settings.external_api_base_url or None
        decrypted = decrypt_path(encrypted_url)
        try:
            parsed = urlparse(decrypted)
            base = f"{parsed.scheme}://{parsed.netloc}"
            logger.debug("Resolved callback base URL from FileURL: %s", base)
            return base
        except Exception:
            return self._settings.external_api_base_url or None

    async def _send_esign_document_prepared(
        self,
        document,
        esign_request_id: int,
        status: str = "Completed",
        pdf_path: "Path | None" = None,
    ) -> bool:
        """POST a PDF as Base64 to CpaDesk ProcessESignDocumentPrepared endpoint.

        Args:
            pdf_path: Override which PDF file to send.  Defaults to the signed PDF
                      (document.final_path).  Pass document.original_path for the
                      "Pending" (admin-prepared) notification.
        """
        import base64
        import httpx

        base_url = await self._get_esign_base_url(esign_request_id)
        if not base_url:
            logger.warning("No callback base URL available – skipping ProcessESignDocumentPrepared")
            return False

        if pdf_path is None:
            pdf_path = Path(document.final_path) if document.final_path else None
        if not pdf_path or not pdf_path.exists():
            logger.warning("ProcessESignDocumentPrepared skipped: PDF not found for doc %s", document.id)
            return False

        try:
            pdf_bytes = pdf_path.read_bytes()
            file_bytes_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/api/ESign/ProcessESignDocumentPrepared",
                    json={
                        "ESignRequestID": esign_request_id,
                        "FileBytes": file_bytes_b64,
                    },
                )
                response.raise_for_status()
            logger.info("ProcessESignDocumentPrepared succeeded for ESignRequestID=%s", esign_request_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("ProcessESignDocumentPrepared failed for ESignRequestID=%s: %s", esign_request_id, exc)
            return False

    async def _send_esign_completion(self, esign_request_id: int, document=None) -> bool:
        """POST signed PDF bytes to CpaDesk ProcessESignCompletion endpoint."""
        import base64
        import httpx

        base_url = await self._get_esign_base_url(esign_request_id)
        if not base_url:
            logger.warning("No callback base URL available – skipping ProcessESignCompletion")
            return False

        # Read signed PDF bytes
        pdf_bytes_b64 = None
        if document and document.final_path:
            try:
                pdf_bytes = Path(document.final_path).read_bytes()
                pdf_bytes_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            except Exception as exc:
                logger.warning("Could not read signed PDF for completion callback: %s", exc)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/api/ESign/ProcessESignCompletion",
                    json={
                        "ESignRequestID": esign_request_id,
                        "FileBytes": pdf_bytes_b64,
                    },
                )
                response.raise_for_status()
            logger.info("ProcessESignCompletion succeeded for ESignRequestID=%s", esign_request_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("ProcessESignCompletion failed for ESignRequestID=%s: %s", esign_request_id, exc)
            return False

    async def _writeback_signed_pdf(self, document) -> bool:
        """Upload the signed PDF back to the decrypted source URL (HTTP POST multipart).

        document.external_path holds the decrypted URL that came from the launch token
        (e.g. https://cpaapi.newtechtest.in/CPADeskDocumentUpload/contract.pdf).
        We POST the signed PDF as multipart/form-data to that URL.

        Returns True on success, False if skipped or failed.
        """
        import httpx

        dest_str = document.external_path
        if not dest_str:
            return False

        signed_src = Path(document.final_path) if document.final_path else None
        if not signed_src or not signed_src.exists():
            logger.warning(
                "Write-back skipped: signed PDF not found at %s for document %s",
                document.final_path,
                document.id,
            )
            return False

        if dest_str.startswith("http://") or dest_str.startswith("https://"):
            # Remote URL – POST the signed PDF as multipart to CpaDesk's server.
            try:
                pdf_bytes = signed_src.read_bytes()
                filename = Path(dest_str).name or "signed.pdf"
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(
                        dest_str,
                        files={"file": (filename, pdf_bytes, "application/pdf")},
                    )
                    response.raise_for_status()
                logger.info(
                    "Write-back succeeded: POST signed PDF to %s (document=%s)",
                    dest_str,
                    document.id,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Write-back (POST) failed for document %s: %s", document.id, exc)
                return False
        else:
            # Local file path – copy signed PDF over the original.
            try:
                dest = Path(dest_str)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(signed_src, dest)
                logger.info(
                    "Write-back succeeded: signed PDF saved to %s (document=%s)",
                    dest,
                    document.id,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Write-back failed for document %s: %s", document.id, exc)
                return False

    async def _audit(
        self,
        event: str,
        success: bool = True,
        external_user_id: str | None = None,
        document_id: UUID | None = None,
        external_document_id: str | None = None,
        details: str = "",
    ) -> None:
        """Write an integration audit entry, swallowing errors to avoid masking primary failures."""
        try:
            await self._integration_audit_repo.create_entry(
                event=event,
                correlation_id=self._correlation_id,
                external_user_id=external_user_id,
                document_id=document_id,
                external_document_id=external_document_id,
                details=details,
                success=success,
            )
            await self._session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write integration audit entry [%s]: %s", event, exc)
