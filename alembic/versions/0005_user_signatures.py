"""user_signatures table – a signer's remembered signature, reused across documents

Revision ID: 0005_user_signatures
Revises: 0004_annotations
Create Date: 2026-06-26 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_user_signatures"
down_revision: Union[str, None] = "0004_annotations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_signatures",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True, nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("signature_data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("user_signatures")
