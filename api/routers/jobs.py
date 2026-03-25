from fastapi import APIRouter

router = APIRouter()


@router.post("/")
async def create_job():
    """검수 Job 생성 — TODO: 구현"""
    return {"message": "TODO"}


@router.get("/{job_id}")
async def get_job(job_id: str):
    """Job 상태 조회 — TODO: 구현"""
    return {"job_id": job_id, "status": "TODO"}
