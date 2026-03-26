import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.database import get_db
from api.models import Job
from api.schemas import JobCreate, JobDetailResponse, JobResponse

router = APIRouter()


@router.post("/", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreate, db: AsyncSession = Depends(get_db)):
    """검수 Job 생성 및 inspect 워커 트리거."""
    job = Job(
        target_host=body.target_host,
        target_user=body.target_user,
        product_profile=body.product_profile,
    )
    db.add(job)
    await db.flush()  # id 확보

    # Celery 태스크 dispatch
    from workers.inspect import inspect_server

    task = inspect_server.apply_async(
        args=[str(job.id), job.target_host, job.target_user, job.product_profile],
        queue="q_inspect",
    )
    job.celery_task_id = task.id
    await db.commit()
    await db.refresh(job)
    return job


@router.get("/", response_model=list[JobResponse])
async def list_jobs(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).order_by(Job.created_at.desc()).offset(skip).limit(limit))
    return result.scalars().all()


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Job 상태 + CheckResult + Report 조회."""
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    result = await db.execute(
        select(Job)
        .where(Job.id == uid)
        .options(selectinload(Job.check_results), selectinload(Job.report))
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str, db: AsyncSession = Depends(get_db)):
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    result = await db.execute(select(Job).where(Job.id == uid))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    await db.delete(job)
    await db.commit()
