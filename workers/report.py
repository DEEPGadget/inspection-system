"""
q_report worker — claude_verdict.json을 읽어 PDF + XLSX 리포트 생성, NFS 저장, DB 업데이트.
concurrency=2 (celeryconfig).
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import structlog
from jinja2 import Environment, FileSystemLoader
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from workers.app import app
from workers.notify import publish_job_status

log = structlog.get_logger(__name__)

_engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
_SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# LaTeX 전용 Jinja2 환경 — \BLOCK{}, \VAR{} 구문 사용 (LaTeX {} 충돌 방지)
def _latex_escape(text: str) -> str:
    """LaTeX 특수문자 이스케이프."""
    text = str(text)
    for old, new in [
        ("\\", r"\textbackslash{}"),
        ("&",  r"\&"),
        ("%",  r"\%"),
        ("$",  r"\$"),
        ("#",  r"\#"),
        ("_",  r"\_"),
        ("{",  r"\{"),
        ("}",  r"\}"),
        ("~",  r"\textasciitilde{}"),
        ("^",  r"\textasciicircum{}"),
    ]:
        text = text.replace(old, new)
    return text

_latex_env = Environment(
    block_start_string=r"\BLOCK{",
    block_end_string="}",
    variable_start_string=r"\VAR{",
    variable_end_string="}",
    comment_start_string=r"\#{",
    comment_end_string="}",
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
)
_latex_env.filters["latex_escape"] = _latex_escape


# ---------------------------------------------------------------------------
# PDF 생성
# ---------------------------------------------------------------------------

def _render_pdf(context: dict, output_path: Path) -> None:
    """LaTeX → xelatex 컴파일 → PDF."""
    tex_str = _latex_env.get_template("report.tex.j2").render(**context)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        tex_file = tmpdir_path / "report.tex"
        tex_file.write_text(tex_str, encoding="utf-8")

        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={tmpdir}",
                str(tex_file),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise RuntimeError(f"xelatex failed:\n{result.stdout[-3000:]}")

        shutil.copy(tmpdir_path / "report.pdf", output_path)


# ---------------------------------------------------------------------------
# XLSX 생성
# ---------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="1A3A5C")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_PASS_FONT   = Font(bold=True, color="155724")
_FAIL_FONT   = Font(bold=True, color="721C24")
_WARN_FONT   = Font(bold=True, color="856404")

_STATUS_FONT = {"pass": _PASS_FONT, "fail": _FAIL_FONT, "warn": _WARN_FONT}


def _render_xlsx(context: dict, output_path: Path) -> None:
    wb = openpyxl.Workbook()

    # ── 시트1: 요약 ──────────────────────────────────────────────────────────
    ws_summary = wb.active
    ws_summary.title = "요약"

    summary_rows = [
        ("Job ID",        context["job_id"]),
        ("대상 서버",     context["target_host"]),
        ("접속 유저",     context["target_user"]),
        ("제품 프로파일", context["product_profile"]),
        ("검수 시작",     context["created_at"]),
        ("리포트 생성",   context["generated_at"]),
        ("최종 판정",     context["overall"].upper()),
    ]
    for row in summary_rows:
        ws_summary.append(row)
        ws_summary.cell(row=ws_summary.max_row, column=1).font = Font(bold=True)

    verdict_cell = ws_summary.cell(row=7, column=2)
    verdict_cell.font = _STATUS_FONT.get(context["overall"], Font(bold=True))

    ws_summary.append([])
    if context["fail_reasons"]:
        ws_summary.append(["FAIL 사유"])
        ws_summary.cell(row=ws_summary.max_row, column=1).font = Font(bold=True, color="721C24")
        for r in context["fail_reasons"]:
            ws_summary.append(["", r])

    if context["warn_reasons"]:
        ws_summary.append(["주의 사항"])
        ws_summary.cell(row=ws_summary.max_row, column=1).font = Font(bold=True, color="856404")
        for r in context["warn_reasons"]:
            ws_summary.append(["", r])

    if context["summary"]:
        ws_summary.append([])
        ws_summary.append(["Claude 요약"])
        ws_summary.cell(row=ws_summary.max_row, column=1).font = Font(bold=True)
        ws_summary.append(["", context["summary"]])

    ws_summary.column_dimensions["A"].width = 20
    ws_summary.column_dimensions["B"].width = 60

    # ── 시트2: 검수 항목 상세 ───────────────────────────────────────────────
    ws_detail = wb.create_sheet("검수 상세")
    headers = ["스크립트", "상태", "Claude 판정", "상세"]
    ws_detail.append(headers)
    for col_idx, _ in enumerate(headers, 1):
        cell = ws_detail.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center")

    for cr in context["check_results"]:
        ws_detail.append([
            cr["check_name"],
            cr["status"].upper(),
            cr["claude_verdict"] or "",
            cr["detail"],
        ])
        status_cell = ws_detail.cell(row=ws_detail.max_row, column=2)
        status_cell.font = _STATUS_FONT.get(cr["status"], Font())
        status_cell.alignment = Alignment(horizontal="center")
        ws_detail.cell(row=ws_detail.max_row, column=4).alignment = Alignment(wrap_text=True)

    ws_detail.column_dimensions["A"].width = 28
    ws_detail.column_dimensions["B"].width = 8
    ws_detail.column_dimensions["C"].width = 35
    ws_detail.column_dimensions["D"].width = 50

    wb.save(str(output_path))


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------

async def _load_job_and_results(session: AsyncSession, job_id: str) -> tuple:
    from api.models import CheckResult, Job

    job_result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = job_result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")

    cr_result = await session.execute(
        select(CheckResult).where(CheckResult.job_id == uuid.UUID(job_id))
    )
    return job, list(cr_result.scalars().all())


async def _save_report_record(
    session: AsyncSession,
    job_id: str,
    pdf_path: str,
    xlsx_path: str,
) -> None:
    from api.models import Report

    report = Report(
        job_id=uuid.UUID(job_id),
        pdf_path=pdf_path,
        xlsx_path=xlsx_path,
    )
    session.add(report)
    await session.commit()


async def _update_job_status(
    session: AsyncSession,
    job_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    from api.models import Job

    result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    job.status = status
    job.updated_at = datetime.now(timezone.utc)
    if error_message:
        job.error_message = error_message[:2000]
    await session.commit()


# ---------------------------------------------------------------------------
# 핵심 async 로직
# ---------------------------------------------------------------------------

async def _async_generate_report(job_id: str) -> None:
    # ── 1. DB 로드 ────────────────────────────────────────
    async with _SessionLocal() as session:
        job, check_results = await _load_job_and_results(session, job_id)
        job_data = {
            "job_id": str(job.id),
            "target_host": job.target_host,
            "target_user": job.target_user,
            "product_profile": job.product_profile,
            "created_at": job.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

    # ── 2. NFS에서 Claude 판독 결과 로드 ─────────────────
    verdict_file = Path(settings.nfs_base_path) / "results" / job_id / "claude_verdict.json"
    if not verdict_file.exists():
        raise FileNotFoundError(f"claude_verdict.json not found: {verdict_file}")

    verdict = json.loads(verdict_file.read_text(encoding="utf-8"))
    overall = verdict.get("overall", "error")

    # ── 3. 렌더링 컨텍스트 구성 ───────────────────────────
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    context = {
        **job_data,
        "overall": overall,
        "fail_reasons": verdict.get("fail_reasons", []),
        "warn_reasons": verdict.get("warn_reasons", []),
        "summary": verdict.get("summary", ""),
        "generated_at": generated_at,
        "check_results": [
            {
                "check_name": cr.check_name,
                "status": cr.status,
                "detail": cr.detail,
                "claude_verdict": cr.claude_verdict,
            }
            for cr in check_results
        ],
    }

    # ── 4. NFS 출력 경로 준비 ─────────────────────────────
    report_dir = Path(settings.nfs_base_path) / "results" / job_id
    report_dir.mkdir(parents=True, exist_ok=True)

    pdf_path  = report_dir / "report.pdf"
    xlsx_path = report_dir / "report.xlsx"

    # ── 5. PDF + XLSX 생성 (동기 I/O — WeasyPrint 제약) ──
    log.info("report.render_start", job_id=job_id, overall=overall)
    _render_pdf(context, pdf_path)
    _render_xlsx(context, xlsx_path)
    log.info("report.render_done", job_id=job_id, pdf=str(pdf_path), xlsx=str(xlsx_path))

    # ── 6. DB에 Report 레코드 저장 ────────────────────────
    async with _SessionLocal() as session:
        await _save_report_record(session, job_id, str(pdf_path), str(xlsx_path))

    # ── 7. Job.status → "pass" ────────────────────────────
    async with _SessionLocal() as session:
        await _update_job_status(session, job_id, "pass")
    await publish_job_status(job_id, "pass")

    log.info("report.complete", job_id=job_id)


async def _mark_error(job_id: str, message: str) -> None:
    async with _SessionLocal() as session:
        await _update_job_status(session, job_id, "error", message[:2000])
    await publish_job_status(job_id, "error", message[:2000])


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------

@app.task(
    bind=True,
    queue="q_report",
    acks_late=True,
    max_retries=3,
    default_retry_delay=30,
    name="workers.report.generate_report",
)
def generate_report(self, job_id: str) -> dict:
    """
    PDF + XLSX 리포트 생성 태스크.

    Args:
        job_id: Job UUID (str)
    """
    log.info("report.start", job_id=job_id)
    try:
        asyncio.run(_async_generate_report(job_id))
        return {"job_id": job_id, "result": "ok"}
    except FileNotFoundError as exc:
        asyncio.run(_mark_error(job_id, str(exc)))
        raise self.retry(exc=exc)
    except Exception as exc:
        asyncio.run(_mark_error(job_id, str(exc)))
        raise self.retry(exc=exc)
