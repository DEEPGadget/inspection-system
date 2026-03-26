import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Report
from api.schemas import ReportResponse

router = APIRouter()


async def _get_report_or_404(job_id: str, db: AsyncSession) -> Report:
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    result = await db.execute(select(Report).where(Report.job_id == uid))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found for this job")
    return report


@router.get("/{job_id}", response_model=ReportResponse)
async def get_report(job_id: str, db: AsyncSession = Depends(get_db)):
    """Report 메타데이터 조회 (파일 경로 포함)."""
    return await _get_report_or_404(job_id, db)


@router.get("/{job_id}/pdf")
async def download_pdf(job_id: str, db: AsyncSession = Depends(get_db)):
    """PDF 리포트 다운로드."""
    report = await _get_report_or_404(job_id, db)
    if not report.pdf_path:
        raise HTTPException(status_code=404, detail="PDF not available")
    path = Path(report.pdf_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found on storage")
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=f"inspection_{job_id}.pdf",
    )


@router.get("/{job_id}/xlsx")
async def download_xlsx(job_id: str, db: AsyncSession = Depends(get_db)):
    """XLSX 리포트 다운로드."""
    report = await _get_report_or_404(job_id, db)
    if not report.xlsx_path:
        raise HTTPException(status_code=404, detail="XLSX not available")
    path = Path(report.xlsx_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="XLSX file not found on storage")
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"inspection_{job_id}.xlsx",
    )
