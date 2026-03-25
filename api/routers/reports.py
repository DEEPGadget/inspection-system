from fastapi import APIRouter

router = APIRouter()


@router.get("/{job_id}/download")
async def download_report(job_id: str):
    """리포트 다운로드 — TODO: 구현"""
    return {"job_id": job_id, "message": "TODO"}
