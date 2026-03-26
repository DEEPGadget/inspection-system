import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base

import enum


class JobStatus(str, enum.Enum):
    pending = "pending"
    inspecting = "inspecting"
    validating = "validating"
    reporting = "reporting"
    pass_ = "pass"
    fail = "fail"
    error = "error"


class CheckStatus(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"
    warn = "warn"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(
        Enum(
            "pending", "inspecting", "validating", "reporting", "pass", "fail", "error",
            name="job_status",
        ),
        nullable=False,
        default="pending",
    )
    target_host: Mapped[str] = mapped_column(String(255), nullable=False)
    target_user: Mapped[str] = mapped_column(String(64), nullable=False, default="root")
    product_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    check_results: Mapped[list["CheckResult"]] = relationship(
        "CheckResult", back_populates="job", cascade="all, delete-orphan"
    )
    report: Mapped["Report | None"] = relationship(
        "Report", back_populates="job", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def nfs_result_dir(self) -> str:
        return f"/srv/inspection/results/{self.id}"


class CheckResult(Base):
    __tablename__ = "check_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("pass", "fail", "warn", name="check_status"),
        nullable=False,
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    claude_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["Job"] = relationship("Job", back_populates="check_results")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    xlsx_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["Job"] = relationship("Job", back_populates="report")
