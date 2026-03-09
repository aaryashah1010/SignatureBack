"""documents.external_path – PhysicalRelativePath write-back column

Revision ID: 0003_external_path
Revises: 0002_integration
Create Date: 2026-03-05 00:00:00

Changes:
    - documents.external_path  (nullable Text – DocumentMaster.PhysicalRelativePath)
      Stores the relative path under document_base_path where the signed PDF must
      be written back when the signer submits.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_external_path"
down_revision: Union[str, None] = "0002_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("external_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "external_path")
