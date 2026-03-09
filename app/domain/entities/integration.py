"""
Integration domain entities.

These are pure data structures; no framework dependencies allowed here.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ExternalUserEntity:
    """Read-only user identity fetched from the SQL Server directory.

    The external software is the authoritative source for these fields.
    """

    external_user_id: str
    username: str
    role: str  # 'ADMIN' or 'SIGNER'
    email: str | None = None
    full_name: str | None = None
    tenant_id: str | None = None
    login_token: str | None = None  # LoginDetail.Token – used to verify the launch token


@dataclass(frozen=True, slots=True)
class LaunchContextEntity:
    """Validated, deserialized payload from a signed launch token.

    Produced after signature verification and nonce uniqueness check.
    """

    external_user_id: str
    role: str  # 'ADMIN' or 'SIGNER' – cross-checked against SQL Server
    external_document_id: str
    document_path: str  # May be encrypted; decrypted in IntegrationService.bootstrap_external_document
    tenant_id: str | None
    jti: str  # Nonce; single-use, tracked in Redis
    expires_at: datetime
    decryption_key: str = ""  # Key to decrypt document_path; empty = plain path (no-op)
    login_token: str = ""    # LoginDetail.Token sent by CpaDesk – verified against SQL Server
    # ── ESign flow fields (populated when token contains eSignRequestId) ──────
    esign_request_id: int | None = None          # ESignRequests.ESignRequestID
    esign_client_id: int | None = None           # ESignRequests.ClientID
    esign_client_login_detail_id: int | None = None  # ESignRequests.ClientLoginDetailID
    esign_client_name: str | None = None         # ESignRequests.ClientName
    esign_client_email: str | None = None        # ESignRequests.ClientEmail
    esign_assigned_by_login_id: int | None = None    # ESignRequests.AssignedByLoginID


@dataclass(slots=True)
class IntegrationAuditEntry:
    """Single entry in the integration lifecycle audit log."""

    id: UUID
    event: str
    correlation_id: str
    external_user_id: str | None
    document_id: UUID | None
    external_document_id: str | None
    details: str
    success: bool
    timestamp: datetime


@dataclass(slots=True)
class CallbackRecord:
    """Persistent record of an outbound callback attempt to the external system."""

    id: UUID
    idempotency_key: str
    external_document_id: str
    external_user_id: str
    status: str
    attempts: int
    last_attempt_at: datetime | None
    succeeded: bool
    last_error: str | None
    created_at: datetime
