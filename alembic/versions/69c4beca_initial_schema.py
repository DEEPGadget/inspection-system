"""initial schema

Revision ID: 69c4beca
Revises:
Create Date: 2026-03-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "69c4beca"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ENUM 타입 ──────────────────────────────────────────
    job_status = postgresql.ENUM(
        "pending",
        "inspecting",
        "validating",
        "reporting",
        "pass",
        "fail",
        "error",
        name="job_status",
    )
    check_status = postgresql.ENUM(
        "pass",
        "fail",
        "warn",
        name="check_status",
    )
    job_status.create(op.get_bind(), checkfirst=True)
    check_status.create(op.get_bind(), checkfirst=True)

    # ── jobs ──────────────────────────────────────────────
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "inspecting",
                "validating",
                "reporting",
                "pass",
                "fail",
                "error",
                name="job_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("target_host", sa.String(255), nullable=False),
        sa.Column("target_user", sa.String(64), nullable=False, server_default="root"),
        sa.Column("product_profile", sa.String(128), nullable=False),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("result_path", sa.String(512), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── check_results ─────────────────────────────────────
    op.create_table(
        "check_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("check_name", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pass", "fail", "warn", name="check_status", create_type=False),
            nullable=False,
        ),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("raw_output", postgresql.JSON, nullable=True),
        sa.Column("claude_verdict", sa.Text, nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── reports ───────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("pdf_path", sa.String(512), nullable=True),
        sa.Column("xlsx_path", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("reports")
    op.drop_table("check_results")
    op.drop_table("jobs")

    op.execute("DROP TYPE IF EXISTS check_status")
    op.execute("DROP TYPE IF EXISTS job_status")
