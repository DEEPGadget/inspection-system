import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# --- Job ---

class JobCreate(BaseModel):
    target_host: str = Field(..., description="검수 대상 서버 IP 또는 호스트명")
    target_user: str = Field("root", description="SSH 접속 유저")
    product_profile: str = Field(..., description="제품 프로파일 이름 (checks/profiles/ 기준)")


class JobResponse(BaseModel):
    id: uuid.UUID
    status: str
    target_host: str
    target_user: str
    product_profile: str
    celery_task_id: str | None
    result_path: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- CheckResult ---

class CheckResultResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    check_name: str
    status: str
    detail: str
    claude_verdict: str | None
    validated_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Report ---

class ReportResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    pdf_path: str | None
    xlsx_path: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Job detail (with relations) ---

class JobDetailResponse(JobResponse):
    check_results: list[CheckResultResponse] = []
    report: ReportResponse | None = None
