"""
Abstract repository interfaces for integration-specific persistence.

Concrete implementations live in the infrastructure layer.
The domain layer only knows these contracts.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.entities.integration import CallbackRecord, ExternalUserEntity, IntegrationAuditEntry


class ExternalUserRepository(ABC):
    """Read-only interface for external user/mapping data (CpaDesk SQL Server database).

    Actual schema – CpaDesk_Phase_2:

        LoginDetail (
            LoginDetailID  INT  PK,
            RoleID         INT,   -- 2=CPA Admin  3=CPA User  4=Client
            UserName       VARCHAR,
            Password       VARCHAR,
            FirstName      VARCHAR,
            LastName       VARCHAR,
            Email          VARCHAR,
            PhoneNumber    VARCHAR,
            Token          VARCHAR NULL
        )

        CPAUser (
            CPAID          INT  PK,
            LoginDetailID  INT  FK → LoginDetail,
            IsActive       BIT
        )

        Client (
            ClientID       INT  PK,
            LoginDetailID  INT  FK → LoginDetail,
            IsActive       BIT
        )

        CAPUserClientMapping (
            LoginDetailID  INT  FK → LoginDetail,  -- admin's LoginDetailID
            ClientID       INT  FK → Client,        -- client's ClientID
            IsActive       BIT
        )

    Role mapping:
        "admin" / RoleID 2-3  →  ADMIN  (CPAUser path)
        "user"  / RoleID 4    →  SIGNER (Client path)
    """

    @abstractmethod
    async def get_user_by_external_id(self, external_user_id: str) -> ExternalUserEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def get_user_by_username(self, username: str) -> ExternalUserEntity | None:
        raise NotImplementedError

    @abstractmethod
    async def get_login_detail_id_by_email(self, email: str) -> int | None:
        """Return the LoginDetailID for a given email address, or None if not found."""
        raise NotImplementedError

    @abstractmethod
    async def get_mapped_signers_for_admin(self, admin_external_user_id: str) -> list[ExternalUserEntity]:
        """Return clients mapped to this admin via CAPUserClientMapping."""
        raise NotImplementedError

    @abstractmethod
    async def get_external_document_path(self, external_document_id: str) -> str | None:
        """Resolve the document file path from the external document catalogue."""
        raise NotImplementedError

    @abstractmethod
    async def update_document_master_path(self, document_guid: str, new_path: str) -> bool:
        """Update DocumentMaster.PhysicalRelativePath with the signed PDF path.

        Called after the signer submits, so CpaDesk knows where the signed file is.
        Returns True if the row was updated, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_document_path_by_guid(self, document_guid: str) -> str | None:
        """Return DocumentMaster.PhysicalRelativePath for the given DocumentGUID.

        PhysicalRelativePath holds the full absolute path on the CpaDesk server
        (e.g. D:\\NewtechGitProjects\\CPADesk.API\\...\\contract.pdf).

        Queries the DocumentMaster table:
            DocumentMaster (
                DocumentGUID          VARCHAR / UNIQUEIDENTIFIER  PK,
                PhysicalRelativePath  VARCHAR  -- absolute path on the server
            )

        Returns None if the GUID is not found or SQL Server is not configured.
        """
        raise NotImplementedError


class IntegrationAuditRepository(ABC):
    """Audit log for all integration lifecycle events (stored in local PostgreSQL)."""

    @abstractmethod
    async def create_entry(
        self,
        event: str,
        correlation_id: str,
        external_user_id: str | None = None,
        document_id: UUID | None = None,
        external_document_id: str | None = None,
        details: str = "",
        success: bool = True,
    ) -> IntegrationAuditEntry:
        raise NotImplementedError


class CallbackAuditRepository(ABC):
    """Tracks idempotent outbound callback attempts to the external system."""

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> CallbackRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def create_record(
        self,
        idempotency_key: str,
        external_document_id: str,
        external_user_id: str,
        status: str,
    ) -> CallbackRecord:
        raise NotImplementedError

    @abstractmethod
    async def increment_attempt(
        self,
        record_id: UUID,
        succeeded: bool,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError
