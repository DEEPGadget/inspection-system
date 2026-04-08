"""
Jobs API 엔드포인트 테스트.
DB는 SQLite in-memory, Celery task는 mock.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.database import Base, get_db
from api.main import app


# ---------------------------------------------------------------------------
# Test DB 픽스처
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_db():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def mock_celery_task():
    """run_preflight Celery task mock."""
    mock_task = MagicMock()
    mock_task.id = str(uuid.uuid4())
    mock_apply = MagicMock(return_value=mock_task)
    with patch("workers.inspect.run_preflight.apply_async", mock_apply):
        yield mock_apply


# ---------------------------------------------------------------------------
# POST /api/jobs/ 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_success(test_db, mock_celery_task):
    """Job 생성 성공 — 201 반환, run_preflight dispatch 확인."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.1",
                "target_user": "root",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["target_host"] == "10.0.0.1"
    assert data["status"] == "pending"
    mock_celery_task.assert_called_once()


@pytest.mark.asyncio
async def test_create_job_with_sw_requirements(test_db, mock_celery_task):
    """sw_requirements 포함 Job 생성."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.2",
                "target_user": "ubuntu",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
                "sw_requirements": "- CUDA 12.4\n- PyTorch 2.3",
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert data["sw_requirements"] == "- CUDA 12.4\n- PyTorch 2.3"


@pytest.mark.asyncio
async def test_create_job_dispatches_run_preflight(test_db):
    """run_preflight가 올바른 인자로 dispatch되는지 확인."""
    mock_task = MagicMock()
    mock_task.id = str(uuid.uuid4())

    with patch("workers.inspect.run_preflight") as mock_run_preflight:
        mock_run_preflight.apply_async.return_value = mock_task
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
        ) as client:
            response = await client.post(
                "/api/jobs/",
                json={
                    "target_host": "10.0.0.3",
                    "target_user": "root",
                    "product_profile": "gpu_server",
                    "sudo_password": "pw123",
                },
            )

    assert response.status_code == 201
    mock_run_preflight.apply_async.assert_called_once()
    call_kwargs = mock_run_preflight.apply_async.call_args
    assert call_kwargs.kwargs["queue"] == "q_inspect"
    # sudo_password는 kwargs로 전달 (args에 포함 안됨)
    task_kwargs = call_kwargs.kwargs.get("kwargs", {})
    assert task_kwargs.get("sudo_password") == "pw123"


# ---------------------------------------------------------------------------
# GET /api/jobs/ 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_empty(test_db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.get("/api/jobs/")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_jobs_after_create(test_db, mock_celery_task):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.1",
                "target_user": "root",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
            },
        )
        response = await client.get("/api/jobs/")
    assert response.status_code == 200
    assert len(response.json()) == 1


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/ 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_not_found(test_db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.get(f"/api/jobs/{uuid.uuid4()}/")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_job_invalid_uuid(test_db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.get("/api/jobs/not-a-uuid/")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_job_success(test_db, mock_celery_task):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        create_resp = await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.1",
                "target_user": "root",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
            },
        )
        job_id = create_resp.json()["id"]
        response = await client.get(f"/api/jobs/{job_id}/")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job_id
    assert data["target_host"] == "10.0.0.1"


# ---------------------------------------------------------------------------
# DELETE /api/jobs/{job_id}/ 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_job_success(test_db, mock_celery_task):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        create_resp = await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.1",
                "target_user": "root",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
            },
        )
        job_id = create_resp.json()["id"]
        response = await client.delete(f"/api/jobs/{job_id}/")

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_job_not_found(test_db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        response = await client.delete(f"/api/jobs/{uuid.uuid4()}/")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_job_hw_manual_checks_persisted(test_db, mock_celery_task):
    """hw_manual_checks가 DB에 저장되는지 검증 — 리포트 Section 2 데이터 손실 방지."""
    hw_data = {"외관 이상 없음": True, "케이블 체결 상태": True, "팬 동작": True}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        create_resp = await client.post(
            "/api/jobs/",
            json={
                "target_host": "10.0.0.10",
                "target_user": "root",
                "product_profile": "gpu_server",
                "sudo_password": "secret",
                "hw_manual_checks": hw_data,
            },
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["id"]

        detail_resp = await client.get(f"/api/jobs/{job_id}/")

    assert detail_resp.status_code == 200
    assert detail_resp.json()["hw_manual_checks"] == hw_data
