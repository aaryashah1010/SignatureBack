"""annotations table – admin-authored highlights, drawings and text comments

Revision ID: 0004_annotations
Revises: 0003_external_path
Create Date: 2026-05-01 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_annotations"
down_revision: Union[str, None] = "0003_external_path"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=False, server_default="#fde047"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("paths", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_annotations_document_id", "annotations", ["document_id"])
    op.create_index("ix_annotations_created_by", "annotations", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_annotations_created_by", table_name="annotations")
    op.drop_index("ix_annotations_document_id", table_name="annotations")
    op.drop_table("annotations")
