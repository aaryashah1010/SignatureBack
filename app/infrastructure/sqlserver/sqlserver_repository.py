"""
SQL Server repository using the real CpaDesk database schema.

Tables used (read-only):
    LoginDetail          – all users (both admins and clients)
                           LoginDetailID  INT  PK
                           RoleID         INT  (2=CPA Admin, 3=CPA User, 4=Client)
                           UserName       VARCHAR
                           Password       VARCHAR
                           FirstName      VARCHAR
                           LastName       VARCHAR
                           Email          VARCHAR
                           PhoneNumber    VARCHAR
                           Token          VARCHAR NULL

    CPAUser              – CPA admins, linked to LoginDetail
                           CPAID          INT  PK
                           LoginDetailID  INT  FK → LoginDetail
                           IsActive       BIT

    Client               – clients / signers (no LoginDetail record)
                           ClientID       INT  PK
                           CPAID          INT  FK → CPAUser.CPAID
                           FirstName      VARCHAR
                           LastName       VARCHAR
                           Email          VARCHAR
                           IsActive       BIT
                           (ClientID is used as external_user_id for signers)

    CAPUserClientMapping – which CPA admin is mapped to which clients
                           LoginDetailID  INT  (admin's LoginDetailID)
                           ClientID       INT  FK → Client.ClientID
                           IsActive       BIT

Role mapping to our system:
    RoleID 2 (CPA Admin) → ADMIN
    RoleID 3 (CPA User)  → ADMIN
    RoleID 4 (Client)    → SIGNER

    The launch token carries role as "admin" or "user";
    normalisation to "ADMIN"/"SIGNER" is done in IntegrationService.
"""

import logging

from app.domain.entities.integration import ExternalUserEntity
from app.domain.repositories.integration_repository import ExternalUserRepository
from app.infrastructure.sqlserver.sqlserver_client import SqlServerClient

logger = logging.getLogger(__name__)


class SqlServerExternalUserRepository(ExternalUserRepository):
    """Reads identity and CPA→client mapping data from the CpaDesk SQL Server database."""

    def __init__(self, client: SqlServerClient) -> None:
        self._client = client

    # ── User resolution ───────────────────────────────────────────────────────

    async def get_user_by_external_id(self, external_user_id: str) -> ExternalUserEntity | None:
        """Resolve a user by their LoginDetailID.

        Checks CPAUser first (admin path), then Client (signer path).
        The ResolvedRole column determines which role to assign locally.
        """
        try:
            login_detail_id = int(external_user_id)
        except ValueError:
            logger.warning("external_user_id is not a valid integer: %s", external_user_id)
            return None

        # First try: LoginDetail → CPAUser path (admin users have LoginDetailID)
        rows = await self._client.execute_query(
            """
            SELECT TOP 1
                   l.LoginDetailID,
                   l.UserName,
                   l.FirstName,
                   l.LastName,
                   l.Email,
                   l.RoleID,
                   l.Token,
                   CASE
                       WHEN c.CPAID IS NOT NULL THEN 'ADMIN'
                       ELSE 'SIGNER'
                   END AS ResolvedRole
            FROM   LoginDetail l
            LEFT JOIN CPAUser c ON c.LoginDetailID = l.LoginDetailID
                                AND c.IsActive = 1
            WHERE  l.LoginDetailID = :id
            """,
            {"id": login_detail_id},
        )
        if rows:
            return self._row_to_entity(rows[0])

        # Second try: Client table by ClientID (signers don't have LoginDetail records)
        client_rows = await self._client.execute_query(
            """
            SELECT TOP 1
                   cl.ClientID  AS LoginDetailID,
                   cl.Email     AS UserName,
                   cl.FirstName,
                   cl.LastName,
                   cl.Email,
                   4            AS RoleID,
                   NULL         AS Token,
                   'SIGNER'     AS ResolvedRole
            FROM   Client cl
            WHERE  cl.ClientID = :id
              AND  cl.IsActive = 1
            """,
            {"id": login_detail_id},
        )
        if client_rows:
            return self._row_to_entity(client_rows[0])

        logger.debug("No user found for id=%s in LoginDetail or Client", external_user_id)
        return None

    async def get_user_by_username(self, username: str) -> ExternalUserEntity | None:
        """Resolve a user by their UserName (used as a fallback lookup)."""
        rows = await self._client.execute_query(
            """
            SELECT TOP 1
                   l.LoginDetailID,
                   l.UserName,
                   l.FirstName,
                   l.LastName,
                   l.Email,
                   l.RoleID,
                   l.Token,
                   CASE
                       WHEN c.CPAID IS NOT NULL THEN 'ADMIN'
                       ELSE 'SIGNER'
                   END AS ResolvedRole
            FROM   LoginDetail l
            LEFT JOIN CPAUser c  ON c.LoginDetailID  = l.LoginDetailID AND c.IsActive  = 1
            LEFT JOIN Client  cl ON cl.LoginDetailID = l.LoginDetailID AND cl.IsActive = 1
            WHERE  l.UserName = :username
              AND  (c.CPAID IS NOT NULL OR cl.ClientID IS NOT NULL)
            """,
            {"username": username},
        )
        if not rows:
            return None
        return self._row_to_entity(rows[0])

    async def get_login_detail_id_by_email(self, email: str) -> int | None:
        """Look up a LoginDetailID by email address.

        Used when a user has a real email address stored locally (not the
        synthetic @external.local placeholder) and we need to recover their
        LoginDetailID for SQL Server mapping queries.
        """
        rows = await self._client.execute_query(
            """
            SELECT TOP 1 LoginDetailID
            FROM   LoginDetail
            WHERE  Email = :email
            """,
            {"email": email},
        )
        if rows:
            return int(rows[0]["LoginDetailID"])
        # Signers live in Client table (no LoginDetail record)
        rows = await self._client.execute_query(
            """
            SELECT TOP 1 ClientID AS LoginDetailID
            FROM   Client
            WHERE  Email = :email AND IsActive = 1
            """,
            {"email": email},
        )
        return int(rows[0]["LoginDetailID"]) if rows else None

    # ── Mapping queries ───────────────────────────────────────────────────────

    async def get_mapped_signers_for_admin(
        self, admin_external_user_id: str
    ) -> list[ExternalUserEntity]:
        """Return all clients mapped to the given CPA admin.

        CAPUserClientMapping.LoginDetailID  = admin's LoginDetailID
        CAPUserClientMapping.ClientID       → Client.ClientID → LoginDetail (name / email)
        """
        try:
            admin_login_detail_id = int(admin_external_user_id)
        except ValueError:
            logger.warning(
                "admin_external_user_id is not a valid integer: %s", admin_external_user_id
            )
            return []

        rows = await self._client.execute_query(
            """
            SELECT DISTINCT
                   cl.ClientID    AS LoginDetailID,
                   cl.Email       AS UserName,
                   cl.FirstName,
                   cl.LastName,
                   cl.Email,
                   4              AS RoleID,
                   NULL           AS Token,
                   'SIGNER'       AS ResolvedRole
            FROM   CAPUserClientMapping m
            JOIN   Client cl ON cl.ClientID = m.ClientID
            WHERE  m.LoginDetailID = :admin_id
              AND  m.IsActive  = 1
              AND  cl.IsActive = 1
            """,
            {"admin_id": admin_login_detail_id},
        )
        return [self._row_to_entity(row) for row in rows]

    # ── ESign Requests ────────────────────────────────────────────────────────

    async def get_esign_request(self, esign_request_id: int) -> dict | None:
        """Fetch a single ESignRequests row by primary key.

        Returns the raw row as a dict, or None if not found / table missing.
        """
        rows = await self._client.execute_query(
            """
            SELECT TOP 1
                   ESignRequestID, FileID, FileName, CPAID, CPAFirmName,
                   ClientID, ClientLoginDetailID, ClientName, ClientEmail,
                   AssignedByLoginID, AssignedByLoginUser, FileURL, Status,
                   RequestedOn, AssignedRole, AssignedRoleID,
                   SignedOn, ExpiredOn, CreatedBy, CreatedOn, IsActive, IsDeleted
            FROM   ESignRequests
            WHERE  ESignRequestID = :id
              AND  IsActive  = 1
              AND  IsDeleted = 0
            """,
            {"id": esign_request_id},
        )
        return rows[0] if rows else None

    # ── Document catalogue ────────────────────────────────────────────────────

    async def update_document_master_path(self, document_guid: str, new_path: str) -> bool:
        """UPDATE FileMaster.PhysicalRelativePath for the given FileID (FileID)."""
        rows_affected = await self._client.execute_non_query(
            """
            UPDATE FileMaster
            SET    PhysicalRelativePath = :new_path
            WHERE  FileID = :guid
            """,
            {"guid": document_guid, "new_path": new_path},
        )
        if rows_affected > 0:
            logger.info("FileMaster updated: guid=%s path=%s", document_guid, new_path)
            return True
        logger.warning("FileMaster update matched 0 rows for guid=%s", document_guid)
        return False

    async def get_external_document_path(self, _external_document_id: str) -> str | None:
        """CpaDesk always sends the encrypted document_path in the launch token.

        The path is decrypted in IntegrationService and stored locally.
        We never need to read it back from FileMaster.
        """
        return None

    async def get_document_path_by_guid(self, document_guid: str) -> str | None:
        """Return FileMaster.PhysicalRelativePath for the given FileID (FileID).

        Used as a fallback when the launch token does not include document_path.
        The token should always supply document_path directly; this method exists
        for scenarios where only the GUID is available.
        """
        rows = await self._client.execute_query(
            """
            SELECT TOP 1 PhysicalRelativePath
            FROM   FileMaster
            WHERE  FileID = :guid
            """,
            {"guid": document_guid},
        )
        if not rows:
            logger.debug("FileMaster: no row found for FileID=%s", document_guid)
            return None
        return rows[0].get("PhysicalRelativePath")

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _row_to_entity(row: dict) -> ExternalUserEntity:
        first = (row.get("FirstName") or "").strip()
        last = (row.get("LastName") or "").strip()
        full_name = f"{first} {last}".strip() or row.get("UserName", "")

        return ExternalUserEntity(
            external_user_id=str(row["LoginDetailID"]),
            username=row["UserName"],
            # ResolvedRole is always 'ADMIN' or 'SIGNER' – set by the query
            role=row.get("ResolvedRole", "SIGNER"),
            email=row.get("Email") or None,
            full_name=full_name,
            tenant_id=None,  # CpaDesk has no tenant concept
            login_token=row.get("Token") or None,
        )


class NullExternalUserRepository(ExternalUserRepository):
    """Fallback when SQL Server is not configured.

    Returns empty / None results without crashing, so the app stays usable
    without SQL Server during local development.
    """

    async def get_user_by_external_id(self, external_user_id: str) -> ExternalUserEntity | None:
        logger.warning("SQL Server not configured – cannot resolve user %s", external_user_id)
        return None

    async def get_user_by_username(self, username: str) -> ExternalUserEntity | None:
        logger.warning("SQL Server not configured – cannot resolve username %s", username)
        return None

    async def get_login_detail_id_by_email(self, _email: str) -> int | None:
        return None

    async def get_mapped_signers_for_admin(self, _admin_external_user_id: str) -> list[ExternalUserEntity]:
        logger.warning("SQL Server not configured – returning empty signer mapping")
        return []

    async def update_document_master_path(self, _document_guid: str, _new_path: str) -> bool:
        return False

    async def get_external_document_path(self, _external_document_id: str) -> str | None:
        return None

    async def get_document_path_by_guid(self, _guid: str) -> str | None:
        return None

    async def get_esign_request(self, _esign_request_id: int) -> dict | None:
        logger.warning("SQL Server not configured – cannot fetch ESignRequest")
        return None
