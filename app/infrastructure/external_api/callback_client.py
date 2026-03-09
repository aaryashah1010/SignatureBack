"""
External callback client with exponential-backoff retry and idempotency.

On document completion the signer triggers this to notify the external
software.  The callback is attempted up to MAX_ATTEMPTS times; each failure
is persisted to callback_audit_logs so retries survive restarts.
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.domain.repositories.integration_repository import CallbackAuditRepository

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: int = 5
BASE_DELAY_SECONDS: float = 2.0  # Backoff: 2, 4, 8, 16, 32 seconds
CALLBACK_ENDPOINT: str = "/api/callbacks/signature"  # TODO: confirm with external software team


class ExternalCallbackClient:
    """Posts signed-document notifications to the external system.

    Guarantees:
    - Idempotency: duplicate submissions for the same (doc, user, status)
      triple are skipped once a successful delivery is recorded.
    - Retry: up to MAX_ATTEMPTS attempts with exponential back-off.
    - Audit: every attempt (success or failure) is written to callback_audit_logs.
    """

    def __init__(
        self,
        base_url: str,
        auth_secret: str,
        callback_audit_repo: CallbackAuditRepository,
        http_timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_secret = auth_secret
        self._audit_repo = callback_audit_repo
        self._timeout = http_timeout

    # ── Public API ────────────────────────────────────────────────────────────

    async def send_completion_callback(
        self,
        external_document_id: str,
        external_user_id: str,
        admin_external_user_id: str | None,
        internal_document_id: UUID,
        document_hash: str | None,
        status: str = "SIGNATURE_COMPLETE",
        session=None,  # AsyncSession – needed so audit flushes are committed
    ) -> bool:
        """Send callback and return True if eventually delivered."""
        idempotency_key = self._build_key(external_document_id, external_user_id, status)

        # Skip if already succeeded (idempotent path).
        existing = await self._audit_repo.get_by_idempotency_key(idempotency_key)
        if existing and existing.succeeded:
            logger.info("Callback already delivered (idempotent). key=%s", idempotency_key)
            return True

        record = await self._audit_repo.create_record(
            idempotency_key=idempotency_key,
            external_document_id=external_document_id,
            external_user_id=external_user_id,
            status=status,
        )
        if session:
            await session.commit()

        payload = {
            "external_document_id": external_document_id,
            "signer_id": external_user_id,
            "admin_id": admin_external_user_id,
            "internal_document_id": str(internal_document_id),
            "status": status,
            "document_hash": document_hash,
            "timestamp": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key,
        }
        headers = {
            "X-Auth-Secret": self._auth_secret,
            "X-Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        f"{self._base_url}{CALLBACK_ENDPOINT}",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()

                await self._audit_repo.increment_attempt(record.id, succeeded=True)
                if session:
                    await session.commit()
                logger.info("Callback delivered. attempt=%d key=%s", attempt, idempotency_key)
                return True

            except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                error_msg = str(exc)
                await self._audit_repo.increment_attempt(
                    record.id, succeeded=False, error=error_msg[:500]
                )
                if session:
                    await session.commit()
                logger.warning(
                    "Callback attempt %d/%d failed: %s key=%s",
                    attempt,
                    MAX_ATTEMPTS,
                    error_msg,
                    idempotency_key,
                )

                if attempt < MAX_ATTEMPTS:
                    delay = BASE_DELAY_SECONDS**attempt
                    logger.debug("Retrying in %.1f s ...", delay)
                    await asyncio.sleep(delay)

        logger.error("All callback attempts exhausted. key=%s", idempotency_key)
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_key(external_document_id: str, external_user_id: str, status: str) -> str:
        """Deterministic idempotency key based on the event triple."""
        raw = f"{external_document_id}:{external_user_id}:{status}"
        return hashlib.sha256(raw.encode()).hexdigest()


class NullCallbackClient:
    """No-op callback client for environments where the external API is not configured."""

    async def send_completion_callback(self, *args, **kwargs) -> bool:  # noqa: ANN002
        logger.warning("External callback not configured – SIGNATURE_COMPLETE event dropped.")
        return False
