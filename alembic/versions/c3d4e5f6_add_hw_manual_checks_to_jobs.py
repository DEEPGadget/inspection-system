"""add hw_manual_checks to jobs

Revision ID: c3d4e5f6
Revises: b2c3d4e5
Create Date: 2026-04-08

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6"
down_revision: Union[str, None] = "b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("hw_manual_checks", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "hw_manual_checks")
