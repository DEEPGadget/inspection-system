"""add expected_specs to jobs

Revision ID: b2c3d4e5
Revises: a1b2c3d4
Create Date: 2026-04-07

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5"
down_revision: Union[str, None] = "a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("expected_specs", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "expected_specs")
