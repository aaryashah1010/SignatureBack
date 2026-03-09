"""integration tables and external linkage columns

Revision ID: 0002_integration
Revises: 0001_initial
Create Date: 2026-03-04 00:00:00

Changes:
    - documents.external_document_id  (nullable VARCHAR – links to external system)
    - integration_audit_logs          (new table – lifecycle event log)
    - callback_audit_logs             (new table – outbound callback retry tracking)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_integration"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extend existing documents table ───────────────────────────────────────
    op.add_column(
        "documents",
        sa.Column("external_document_id", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_documents_external_document_id",
        "documents",
        ["external_document_id"],
        unique=False,
    )

    # ── Integration lifecycle audit log ───────────────────────────────────────
    op.create_table(
        "integration_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("event", sa.String(length=120), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("external_user_id", sa.String(length=200), nullable=True),
        # document_id is a soft reference (no FK) so we can log pre-doc events.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_document_id", sa.String(length=200), nullable=True),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_integration_audit_logs_event", "integration_audit_logs", ["event"])
    op.create_index(
        "ix_integration_audit_logs_correlation_id",
        "integration_audit_logs",
        ["correlation_id"],
    )
    op.create_index(
        "ix_integration_audit_logs_external_user_id",
        "integration_audit_logs",
        ["external_user_id"],
    )
    op.create_index(
        "ix_integration_audit_logs_document_id",
        "integration_audit_logs",
        ["document_id"],
    )

    # ── Outbound callback retry tracking ──────────────────────────────────────
    op.create_table(
        "callback_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("external_document_id", sa.String(length=200), nullable=False),
        sa.Column("external_user_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=80), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_callback_audit_logs_idempotency_key",
        "callback_audit_logs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_callback_audit_logs_external_document_id",
        "callback_audit_logs",
        ["external_document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_callback_audit_logs_external_document_id", table_name="callback_audit_logs")
    op.drop_index("ix_callback_audit_logs_idempotency_key", table_name="callback_audit_logs")
    op.drop_table("callback_audit_logs")

    op.drop_index("ix_integration_audit_logs_document_id", table_name="integration_audit_logs")
    op.drop_index("ix_integration_audit_logs_external_user_id", table_name="integration_audit_logs")
    op.drop_index("ix_integration_audit_logs_correlation_id", table_name="integration_audit_logs")
    op.drop_index("ix_integration_audit_logs_event", table_name="integration_audit_logs")
    op.drop_table("integration_audit_logs")

    op.drop_index("ix_documents_external_document_id", table_name="documents")
    op.drop_column("documents", "external_document_id")
