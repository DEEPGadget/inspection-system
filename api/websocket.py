"""
WebSocket endpoint — Job 상태 실시간 push.

연결 흐름:
  1. 접속 시 현재 DB 상태 즉시 전송
  2. terminal 상태(pass/fail/error)면 즉시 close
  3. 진행 중이면 Redis pub/sub 구독 → 상태 변경 메시지 forwarding
  4. terminal 메시지 수신 또는 클라이언트 disconnect 시 종료
"""

import json
import uuid

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter
from fastapi.websockets import WebSocketDisconnect
from sqlalchemy import select
from starlette.websockets import WebSocket

from api.database import AsyncSessionLocal
from api.models import Job
from config.settings import settings

log = structlog.get_logger(__name__)
router = APIRouter()

_TERMINAL = frozenset({"pass", "fail", "error"})


@router.websocket("/jobs/{job_id}")
async def ws_job_status(websocket: WebSocket, job_id: str) -> None:
    """Job 상태 변경 실시간 스트림."""
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    log.info("ws.connected", job_id=job_id)

    # ── 현재 상태 조회 (short-lived session) ──────────────────────────────
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == uid))
        job = result.scalar_one_or_none()

    if job is None:
        await websocket.send_json({"error": "Job not found", "job_id": job_id})
        await websocket.close(code=1008)
        return

    initial = {
        "job_id": job_id,
        "status": job.status,
        "ts": job.updated_at.isoformat(),
    }
    await websocket.send_json(initial)

    if job.status in _TERMINAL:
        await websocket.close()
        return

    # ── Redis pub/sub 구독 ─────────────────────────────────────────────────
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with r.pubsub() as pubsub:
            await pubsub.subscribe(f"job:{job_id}")
            try:
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    await websocket.send_text(message["data"])
                    data = json.loads(message["data"])
                    if data.get("status") in _TERMINAL:
                        await websocket.close()
                        break
            except WebSocketDisconnect:
                log.info("ws.disconnected", job_id=job_id)
    finally:
        await r.aclose()
        log.info("ws.closed", job_id=job_id)
