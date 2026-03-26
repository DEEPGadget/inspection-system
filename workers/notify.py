"""
Job 상태 변경을 Redis pub/sub으로 발행하는 공유 헬퍼.
channel: job:{job_id}
payload: {"job_id": str, "status": str, "ts": ISO8601, "error_message"?: str}
"""

import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from config.settings import settings


async def publish_job_status(
    job_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    """Job 상태 변경을 Redis pub/sub channel 'job:{job_id}'에 발행."""
    payload: dict = {
        "job_id": job_id,
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if error_message:
        payload["error_message"] = error_message

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.publish(f"job:{job_id}", json.dumps(payload))
    finally:
        await r.aclose()
