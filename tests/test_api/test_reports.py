"""
Reports API 테스트.
DB는 dependency_overrides로 mock, 파일 I/O는 tmp_path 사용.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.database import get_db
from api.main import app


def _make_report(job_id: str, pdf_path: str | None = None, xlsx_path: str | None = None):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.job_id = uuid.UUID(job_id)
    r.pdf_path = pdf_path
    r.xlsx_path = xlsx_path
    r.created_at = MagicMock()
    return r


def _make_client(mock_session):
    """dependency_overrides를 설정한 AsyncClient context manager 반환."""

    async def _override():
        yield mock_session

    app.dependency_overrides[get_db] = _override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


# lifespan DB 연결 막기
@pytest.fixture(autouse=True)
def mock_lifespan_db():
    with patch("api.main.engine") as mock_engine:
        mock_conn = AsyncMock()
        mock_conn.run_sync = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()
        yield mock_engine


def _make_session(report):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = report
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    return session


@pytest.mark.asyncio
async def test_get_report_not_found():
    job_id = str(uuid.uuid4())
    session = _make_session(None)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}")

    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_report_invalid_uuid():
    session = _make_session(None)

    async with _make_client(session) as c:
        r = await c.get("/api/reports/not-a-uuid")

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_report_ok():
    job_id = str(uuid.uuid4())
    fake_report = _make_report(job_id, pdf_path="/srv/x/report.pdf", xlsx_path="/srv/x/report.xlsx")
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}")

    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["pdf_path"] == "/srv/x/report.pdf"


@pytest.mark.asyncio
async def test_download_pdf_ok(tmp_path):
    job_id = str(uuid.uuid4())
    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    fake_report = _make_report(job_id, pdf_path=str(pdf_file))
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}/pdf")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.4 dummy"


@pytest.mark.asyncio
async def test_download_pdf_file_missing(tmp_path):
    """pdf_path가 DB에 있지만 파일이 없는 경우."""
    job_id = str(uuid.uuid4())
    fake_report = _make_report(job_id, pdf_path=str(tmp_path / "nonexistent.pdf"))
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}/pdf")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_pdf_no_path():
    """pdf_path가 None인 경우."""
    job_id = str(uuid.uuid4())
    fake_report = _make_report(job_id, pdf_path=None)
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}/pdf")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_xlsx_ok(tmp_path):
    job_id = str(uuid.uuid4())
    xlsx_file = tmp_path / "report.xlsx"
    xlsx_file.write_bytes(b"PK dummy xlsx")

    fake_report = _make_report(job_id, xlsx_path=str(xlsx_file))
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}/xlsx")

    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]


@pytest.mark.asyncio
async def test_download_xlsx_no_path():
    """xlsx_path가 None인 경우."""
    job_id = str(uuid.uuid4())
    fake_report = _make_report(job_id, xlsx_path=None)
    session = _make_session(fake_report)

    async with _make_client(session) as c:
        r = await c.get(f"/api/reports/{job_id}/xlsx")

    assert r.status_code == 404
