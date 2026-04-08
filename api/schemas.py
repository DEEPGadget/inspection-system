import uuid
from datetime import datetime

from pydantic import BaseModel, Field, SecretStr


# --- Job ---


class JobCreate(BaseModel):
    target_host: str = Field(..., description="검수 대상 서버 IP 또는 호스트명")
    target_user: str = Field("root", description="SSH 접속 유저")
    product_profile: str = Field(..., description="제품 프로파일 이름 (checks/profiles/ 기준)")
    sudo_password: SecretStr | None = Field(
        None, description="sudo 비밀번호 (스크립트 내 권한 필요 작업용, DB 미저장)"
    )
    sw_requirements: str | None = Field(
        None, description="SW 요구사항 원문 (sw_requirements.md 내용)"
    )
    expected_specs: dict | None = Field(
        None, description="기대 스펙 (e.g. {expected_gpu_count: 8}), fail_if_not_equal 규칙용"
    )
    hw_manual_checks: dict | None = Field(None, description="Phase 1 수동 검수 8항목 GUI 입력값")


class JobResponse(BaseModel):
    id: uuid.UUID
    status: str
    target_host: str
    target_user: str
    product_profile: str
    sw_requirements: str | None
    expected_specs: dict | None
    hw_manual_checks: dict | None
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
