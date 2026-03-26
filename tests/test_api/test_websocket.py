"""
WebSocket endpoint 테스트.
DB와 Redis는 mock — 접속/상태전송/terminal close/disconnect/race window 흐름 검증.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from api.main import app


# ── 공통 픽스처 ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_lifespan_db():
    with patch("api.main.engine") as mock_engine:
        mock_conn = AsyncMock()
        mock_conn.run_sync = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()
        yield mock_engine


def _make_job(job_id: str, status: str):
    from datetime import datetime, timezone

    job = MagicMock()
    job.id = uuid.UUID(job_id)
    job.status = status
    job.updated_at = datetime(2026, 3, 26, 0, 0, 0, tzinfo=timezone.utc)
    return job


def _mock_db_session(job):
    """매 호출마다 동일한 job을 반환하는 DB 세션 mock."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = job
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_factory = MagicMock(return_value=mock_session)
    return mock_factory


def _mock_db_session_sequence(*jobs):
    """호출 순서대로 다른 job을 반환하는 DB 세션 mock (race window 테스트용)."""
    call_count = 0

    def factory():
        nonlocal call_count
        job = jobs[min(call_count, len(jobs) - 1)]
        call_count += 1
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    return MagicMock(side_effect=factory)


# ── 테스트 ───────────────────────────────────────────────────────────────────


def test_ws_invalid_uuid():
    """잘못된 UUID → 1008 close."""
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/jobs/not-a-uuid"):
                pass


def test_ws_job_not_found():
    """Job 없음 → error 메시지 후 close."""
    job_id = str(uuid.uuid4())
    mock_factory = _mock_db_session(None)

    with patch("api.websocket.AsyncSessionLocal", mock_factory):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                data = ws.receive_json()

    assert data["error"] == "Job not found"
    assert data["job_id"] == job_id


def test_ws_terminal_job_closes_immediately():
    """terminal 상태 Job → 초기 상태 전송 후 즉시 close."""
    job_id = str(uuid.uuid4())
    fake_job = _make_job(job_id, "pass")
    mock_factory = _mock_db_session(fake_job)

    with patch("api.websocket.AsyncSessionLocal", mock_factory):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                data = ws.receive_json()

    assert data["job_id"] == job_id
    assert data["status"] == "pass"


def test_ws_terminal_fail_job():
    """fail 상태 Job → 초기 상태 전송 후 close."""
    job_id = str(uuid.uuid4())
    fake_job = _make_job(job_id, "fail")
    mock_factory = _mock_db_session(fake_job)

    with patch("api.websocket.AsyncSessionLocal", mock_factory):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                data = ws.receive_json()

    assert data["status"] == "fail"


def test_ws_inprogress_job_receives_update():
    """진행 중 Job → 초기 상태 전송 후 Redis 메시지 수신 → terminal close."""
    job_id = str(uuid.uuid4())
    fake_job = _make_job(job_id, "validating")
    # 첫 조회(initial) + 두 번째 조회(post-subscribe 재확인) 모두 동일 상태
    mock_factory = _mock_db_session_sequence(fake_job, fake_job)

    terminal_msg = json.dumps(
        {"job_id": job_id, "status": "pass", "ts": "2026-03-26T00:01:00+00:00"}
    )

    # Redis pub/sub mock: listen()이 terminal 메시지 하나 반환
    async def fake_listen():
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message", "data": terminal_msg}

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen
    mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
    mock_pubsub.__aexit__ = AsyncMock(return_value=False)

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
    mock_redis.aclose = AsyncMock()

    with (
        patch("api.websocket.AsyncSessionLocal", mock_factory),
        patch("api.websocket.aioredis.from_url", return_value=mock_redis),
    ):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                initial = ws.receive_json()
                update = ws.receive_text()

    assert initial["status"] == "validating"
    assert json.loads(update)["status"] == "pass"


def test_ws_inprogress_multiple_updates():
    """중간 상태 업데이트 수신 후 terminal에서 close."""
    job_id = str(uuid.uuid4())
    fake_job = _make_job(job_id, "inspecting")
    mock_factory = _mock_db_session_sequence(fake_job, fake_job)

    msgs = [
        json.dumps({"job_id": job_id, "status": "validating", "ts": "2026-03-26T00:01:00+00:00"}),
        json.dumps({"job_id": job_id, "status": "reporting", "ts": "2026-03-26T00:02:00+00:00"}),
        json.dumps({"job_id": job_id, "status": "pass", "ts": "2026-03-26T00:03:00+00:00"}),
    ]

    async def fake_listen():
        yield {"type": "subscribe", "data": 1}
        for m in msgs:
            yield {"type": "message", "data": m}

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen
    mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
    mock_pubsub.__aexit__ = AsyncMock(return_value=False)

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
    mock_redis.aclose = AsyncMock()

    with (
        patch("api.websocket.AsyncSessionLocal", mock_factory),
        patch("api.websocket.aioredis.from_url", return_value=mock_redis),
    ):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                initial = ws.receive_json()
                updates = [json.loads(ws.receive_text()) for _ in msgs]

    assert initial["status"] == "inspecting"
    statuses = [u["status"] for u in updates]
    assert statuses == ["validating", "reporting", "pass"]


def test_ws_race_window_terminal_transition():
    """race window 픽스: subscribe 직전 terminal 전이 → 재확인 후 즉시 전송 및 close."""
    job_id = str(uuid.uuid4())
    # 첫 DB 조회: 진행 중 / 두 번째 조회(post-subscribe): 이미 terminal
    job_inprogress = _make_job(job_id, "validating")
    job_terminal = _make_job(job_id, "pass")
    mock_factory = _mock_db_session_sequence(job_inprogress, job_terminal)

    # listen()은 호출되지 않아야 하므로 빈 generator
    async def fake_listen():
        return
        yield  # makes this an async generator

    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.listen = fake_listen
    mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
    mock_pubsub.__aexit__ = AsyncMock(return_value=False)

    mock_redis = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
    mock_redis.aclose = AsyncMock()

    with (
        patch("api.websocket.AsyncSessionLocal", mock_factory),
        patch("api.websocket.aioredis.from_url", return_value=mock_redis),
    ):
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/jobs/{job_id}") as ws:
                initial = ws.receive_json()
                reconciled = ws.receive_json()

    assert initial["status"] == "validating"
    assert reconciled["status"] == "pass"
