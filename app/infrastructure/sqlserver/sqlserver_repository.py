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
                       WHEN l.RoleID IN (1, 2, 3) THEN 'ADMIN'
                       ELSE 'SIGNER'
                   END AS ResolvedRole
            FROM   LoginDetail l
            WHERE  l.LoginDetailID = :id
            """,
            {"id": login_detail_id},
        )
        if rows:
            return self._row_to_entity(rows[0])

        # Second try: Client table by ClientID (signers don't have LoginDetail records)
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

    async def get_client_by_id(self, client_id: str) -> ExternalUserEntity | None:
        """Look up a signer directly from Client table — avoids conflict when ClientID == LoginDetailID."""
        try:
            cid = int(client_id)
        except ValueError:
            return None
        rows = await self._client.execute_query(
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
            {"id": cid},
        )
        if rows:
            return self._row_to_entity(rows[0])
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
            ORDER BY cl.FirstName, cl.LastName
            """,
            {"admin_id": admin_login_detail_id},
        )
        return [self._row_to_entity(row) for row in rows]

    # ── 3-tier: ClientUser + LoginDetail queries ──────────────────────────────

    async def get_client_users_by_parent_id(self, parent_client_id: int) -> list[ExternalUserEntity]:
        """Return all active ClientUser rows under a parent client joined with LoginDetail.

        Used by the admin mapped-signers dropdown (Admin → Client → Users model).
        """
        rows = await self._client.execute_query(
            """
            SELECT DISTINCT
                   cu.LoginDetailID,
                   ld.UserName,
                   ld.FirstName,
                   ld.LastName,
                   ld.Email,
                   4              AS RoleID,
                   NULL           AS Token,
                   'SIGNER'       AS ResolvedRole
            FROM   ClientUser cu
            JOIN   LoginDetail ld ON ld.LoginDetailID = cu.LoginDetailID
            WHERE  cu.ParentClientID = :parent_id
              AND  cu.IsActive = 1
            ORDER BY ld.FirstName, ld.LastName
            """,
            {"parent_id": parent_client_id},
        )
        return [self._row_to_entity(row) for row in rows]

    async def get_login_detail_by_id(self, login_detail_id: int) -> ExternalUserEntity | None:
        """Fetch a single LoginDetail row by LoginDetailID.

        Used in the 3-tier signer launch flow to resolve the user identified
        by the loginDetailId URL param sent by CPA.
        """
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
                   'SIGNER'  AS ResolvedRole
            FROM   LoginDetail l
            WHERE  l.LoginDetailID = :id
            """,
            {"id": login_detail_id},
        )
        if rows:
            return self._row_to_entity(rows[0])
        return None

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

    async def get_esign_request_by_guid(self, guid: str) -> dict | None:
        """Fetch ESignRequests row by EsignRequestGuid (used as the new launch token)."""
        rows = await self._client.execute_query(
            """
            SELECT TOP 1
                   ESignRequestID, EsignGuid, EsignToken,
                   FileID, FileName, CPAID, CPAFirmName,
                   ClientID, ClientLoginDetailID, ClientName, ClientEmail,
                   AssignedByLoginID, AssignedByLoginUser, FileURL, Status,
                   RequestedOn, AssignedRole, AssignedRoleID,
                   SignedOn, ExpiredOn, CreatedBy, CreatedOn, IsActive, IsDeleted
            FROM   ESignRequests
            WHERE  EsignGuid = :guid
              AND  IsActive  = 1
              AND  IsDeleted = 0
            """,
            {"guid": guid},
        )
        return rows[0] if rows else None

    # ── ESignClients tracking ─────────────────────────────────────────────────

    async def insert_esign_client_if_not_exists(
        self,
        esign_request_id: int,
        client_id: int | None,
        client_login_detail_id: int,
        client_name: str,
        client_email: str,
        created_by: int | None,
    ) -> bool:
        """INSERT into ESignClients only when no row exists for this request + user pair.

        Uses a SELECT after attempted INSERT to confirm success, because pyodbc returns
        -1 for @@ROWCOUNT inside compound IF/BEGIN/END batches.
        """
        # First do the conditional insert.
        await self._client.execute_non_query(
            """
            IF NOT EXISTS (
                SELECT 1 FROM ESignClients
                WHERE  ESignRequestId      = :request_id
                  AND  ClientLoginDetailId = :login_detail_id
            )
            BEGIN
                INSERT INTO ESignClients
                    (ESignRequestId, ClientId, ClientName, ClientLoginDetailId,
                     ClientEmail, ESignStatus, CreatedOn, CreatedBy, UpdatedOn, UpdatedBy)
                VALUES
                    (:request_id, :client_id, :client_name, :login_detail_id,
                     :client_email, 0, GETDATE(), :created_by, GETDATE(), :created_by)
            END
            """,
            {
                "request_id": esign_request_id,
                "client_id": client_id or 0,
                "login_detail_id": client_login_detail_id,
                "client_name": client_name,
                "client_email": client_email,
                "created_by": created_by or 0,
            },
        )
        # Verify the row exists (pyodbc rowcount is unreliable for compound batches).
        rows = await self._client.execute_query(
            """
            SELECT TOP 1 1 AS Found
            FROM   ESignClients
            WHERE  ESignRequestId      = :request_id
              AND  ClientLoginDetailId = :login_detail_id
            """,
            {"request_id": esign_request_id, "login_detail_id": client_login_detail_id},
        )
        inserted = bool(rows)
        if inserted:
            logger.info(
                "ESignClients inserted: request_id=%s login_detail_id=%s",
                esign_request_id, client_login_detail_id,
            )
        return inserted

    async def mark_esign_client_signed(self, esign_request_id: int, client_login_detail_id: int) -> bool:
        """Set ESignClients.ESignStatus = 1 for the given signer row."""
        rows_affected = await self._client.execute_non_query(
            """
            UPDATE ESignClients
            SET    ESignStatus = 1,
                   UpdatedOn   = GETDATE()
            WHERE  ESignRequestId      = :request_id
              AND  ClientLoginDetailId = :login_detail_id
            """,
            {"request_id": esign_request_id, "login_detail_id": client_login_detail_id},
        )
        if rows_affected > 0:
            logger.info(
                "ESignClients marked signed: request_id=%s login_detail_id=%s",
                esign_request_id, client_login_detail_id,
            )
            return True
        logger.warning(
            "ESignClients update matched 0 rows: request_id=%s login_detail_id=%s",
            esign_request_id, client_login_detail_id,
        )
        return False

    async def update_esign_request_completed(self, esign_request_id: int) -> bool:
        """Set Status='Completed' and SignedOn=now on ESignRequests when all signers done."""
        rows_affected = await self._client.execute_non_query(
            """
            UPDATE ESignRequests
            SET    Status    = 'Completed',
                   SignedOn  = GETDATE(),
                   UpdatedOn = GETDATE()
            WHERE  ESignRequestID = :request_id
            """,
            {"request_id": esign_request_id},
        )
        if rows_affected > 0:
            logger.info("ESignRequests marked Completed: request_id=%s", esign_request_id)
            return True
        logger.warning("ESignRequests update matched 0 rows: request_id=%s", esign_request_id)
        return False

    async def check_all_esign_clients_signed(self, esign_request_id: int) -> bool:
        """Return True if every ESignClients row for this request has ESignStatus = 1."""
        rows = await self._client.execute_query(
            """
            SELECT COUNT(*)                                          AS Total,
                   SUM(CASE WHEN ESignStatus = 1 THEN 1 ELSE 0 END) AS Signed
            FROM   ESignClients
            WHERE  ESignRequestId = :request_id
            """,
            {"request_id": esign_request_id},
        )
        if not rows:
            return False
        total = int(rows[0].get("Total") or 0)
        signed = int(rows[0].get("Signed") or 0)
        return total > 0 and total == signed

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

    async def get_client_by_id(self, client_id: str) -> ExternalUserEntity | None:  # noqa: ARG002
        return None

    async def get_login_detail_id_by_email(self, _email: str) -> int | None:
        return None

    async def get_mapped_signers_for_admin(self, _admin_external_user_id: str) -> list[ExternalUserEntity]:
        logger.warning("SQL Server not configured – returning empty signer mapping")
        return []

    async def get_client_users_by_parent_id(self, _parent_client_id: int) -> list[ExternalUserEntity]:
        return []

    async def get_login_detail_by_id(self, _login_detail_id: int) -> ExternalUserEntity | None:
        return None

    async def insert_esign_client_if_not_exists(
        self,
        _esign_request_id: int,
        _client_id: int | None,
        _client_login_detail_id: int,
        _client_name: str,
        _client_email: str,
        _created_by: int | None,
    ) -> bool:
        return False

    async def update_esign_request_completed(self, _esign_request_id: int) -> bool:
        return False

    async def mark_esign_client_signed(self, _esign_request_id: int, _client_login_detail_id: int) -> bool:
        return False

    async def check_all_esign_clients_signed(self, _esign_request_id: int) -> bool:
        return False

    async def update_document_master_path(self, _document_guid: str, _new_path: str) -> bool:
        return False

    async def get_external_document_path(self, _external_document_id: str) -> str | None:
        return None

    async def get_document_path_by_guid(self, _guid: str) -> str | None:
        return None

    async def get_esign_request(self, _esign_request_id: int) -> dict | None:
        logger.warning("SQL Server not configured – cannot fetch ESignRequest")
        return None

    async def get_esign_request_by_guid(self, guid: str) -> dict | None:  # noqa: ARG002
        logger.warning("SQL Server not configured – cannot fetch ESignRequest by guid")
        return None
