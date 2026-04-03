"""v2 job_status and sw_requirements

Revision ID: a1b2c3d4
Revises: 69c4beca
Create Date: 2026-04-03

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4"
down_revision: Union[str, None] = "69c4beca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── job_status ENUM 확장 ───────────────────────────────
    # ALTER TYPE ... ADD VALUE IF NOT EXISTS 는 트랜잭션 밖에서 실행
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'preflight'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'sw_install'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'rebooting'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'post_install'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'cleanup'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'failed'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'rejected'")
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'report_failed'")

    # ── jobs.sw_requirements 컬럼 추가 ────────────────────
    op.add_column("jobs", sa.Column("sw_requirements", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "sw_requirements")
    # PostgreSQL은 ENUM 값 제거를 직접 지원하지 않음
    # 추가된 ENUM 값은 DB에 남음 (코드에서 미사용 상태로 유지)
