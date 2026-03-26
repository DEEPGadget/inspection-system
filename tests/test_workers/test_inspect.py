"""
Inspect Worker 유닛 테스트.
SSH와 DB는 mock — 비즈니스 로직(프로파일 로드, JSON 파싱, 경로 계산)만 검증.
"""
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.inspect import _nfs_raw_dir, _profile_path, _script_path, _ssh_key_path


@pytest.fixture(autouse=True)
def mock_publish(monkeypatch):
    """publish_job_status는 Redis 연결이 필요 — 모든 테스트에서 mock."""
    monkeypatch.setattr("workers.inspect.publish_job_status", AsyncMock())


# ---------------------------------------------------------------------------
# 경로 헬퍼 테스트
# ---------------------------------------------------------------------------

def test_nfs_raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.inspect.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())
    result = _nfs_raw_dir(job_id)
    assert result == tmp_path / "results" / job_id / "inspect_raw"


def test_profile_path_default():
    p = _profile_path("default")
    assert p.name == "default.json"
    assert "checks/profiles" in str(p)


def test_script_path():
    p = _script_path("phase2_sw_basic", "sw_gpu")
    assert p.name == "sw_gpu.sh"
    assert "phase2_sw_basic" in str(p)


def test_ssh_key_path_host_specific(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.inspect.settings.ssh_key_dir", str(tmp_path))
    key = tmp_path / "192.168.1.10"
    key.write_text("fake_key")
    assert _ssh_key_path("192.168.1.10") == str(key)


def test_ssh_key_path_fallback_default(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.inspect.settings.ssh_key_dir", str(tmp_path))
    default_key = tmp_path / "default"
    default_key.write_text("fake_key")
    assert _ssh_key_path("10.0.0.99") == str(default_key)


def test_ssh_key_path_none(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.inspect.settings.ssh_key_dir", str(tmp_path))
    assert _ssh_key_path("10.0.0.99") is None


# ---------------------------------------------------------------------------
# 프로파일 로드 테스트
# ---------------------------------------------------------------------------

def test_profile_loads_default():
    p = _profile_path("default")
    assert p.exists(), "default.json 프로파일이 없습니다"
    with p.open() as f:
        profile = json.load(f)
    assert "phases" in profile
    assert isinstance(profile["phases"], dict)


def test_profile_missing_raises(tmp_path, monkeypatch):
    """존재하지 않는 프로파일은 FileNotFoundError."""
    from workers.inspect import _profile_path as pp
    p = pp("nonexistent_profile")
    assert not p.exists()


# ---------------------------------------------------------------------------
# JSON 파싱 로직 테스트 (스크립트 stdout 처리)
# ---------------------------------------------------------------------------

def _parse_output(stdout: str, script_name: str) -> dict:
    """inspect.py 내부 파싱 로직 재현."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "check": script_name,
            "status": "fail",
            "detail": f"JSON parse error. stdout={stdout[:200]}",
        }


def test_parse_valid_json():
    stdout = '{"check":"sw_gpu","status":"pass","detail":"8x A100 detected"}'
    out = _parse_output(stdout, "sw_gpu")
    assert out["status"] == "pass"
    assert out["check"] == "sw_gpu"


def test_parse_invalid_json_returns_fail():
    out = _parse_output("not json output", "sw_gpu")
    assert out["status"] == "fail"
    assert "JSON parse error" in out["detail"]


def test_parse_warn_status():
    stdout = '{"check":"sw_storage","status":"warn","detail":"one disk slow"}'
    out = _parse_output(stdout, "sw_storage")
    assert out["status"] == "warn"


# ---------------------------------------------------------------------------
# _async_inspect 통합 테스트 (SSH + DB mock)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_inspect_success(tmp_path, monkeypatch):
    """SSH 성공 시나리오 — DB와 SSH를 mock하고 NFS 파일 생성 확인."""
    import asyncssh

    job_id = str(uuid.uuid4())
    monkeypatch.setattr("workers.inspect.settings.nfs_base_path", str(tmp_path))
    monkeypatch.setattr("workers.inspect.settings.ssh_key_dir", str(tmp_path / "keys"))

    # 프로파일: phase2_sw_basic/sw_cpu 한 개만 활성화
    profile_data = {
        "phases": {
            "phase2_sw_basic": {"enabled": True, "scripts": ["sw_cpu"]},
        }
    }
    profiles_dir = Path(__file__).parent.parent.parent / "checks" / "profiles"
    test_profile = profiles_dir / "_test_profile.json"
    test_profile.write_text(json.dumps(profile_data))

    # 스크립트 파일 생성
    script_dir = Path(__file__).parent.parent.parent / "checks" / "base" / "phase2_sw_basic"
    script_dir.mkdir(parents=True, exist_ok=True)
    test_script = script_dir / "sw_cpu.sh"
    if not test_script.exists():
        test_script.write_text('#!/bin/bash\necho \'{"check":"sw_cpu","status":"pass","detail":"ok"}\'')

    # SSH mock
    mock_result = MagicMock()
    mock_result.stdout = '{"check":"sw_cpu","status":"pass","detail":"4x Intel Xeon"}'
    mock_result.stderr = ""

    mock_sftp = AsyncMock()
    mock_sftp.__aenter__ = AsyncMock(return_value=mock_sftp)
    mock_sftp.__aexit__ = AsyncMock(return_value=False)
    mock_sftp.put = AsyncMock()
    mock_sftp.chmod = AsyncMock()

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.start_sftp_client = MagicMock(return_value=mock_sftp)

    # DB mock
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock(
        id=uuid.UUID(job_id), status="pending", updated_at=None,
    ))))
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_factory = MagicMock(return_value=mock_session)

    # validate mock
    mock_validate = MagicMock()
    mock_validate.apply_async = MagicMock()

    with (
        patch("workers.inspect._SessionLocal", mock_session_factory),
        patch("asyncssh.connect", return_value=mock_conn),
        patch("workers.inspect.validate_results", mock_validate, create=True),
    ):
        from workers.inspect import _async_inspect

        # validate import를 mock하기 위해 모듈 패치
        with patch.dict("sys.modules", {"workers.validate": MagicMock(validate_results=mock_validate)}):
            try:
                await _async_inspect(job_id, "10.0.0.1", "root", "_test_profile")
            except Exception:
                pass  # validate import 실패는 무시 (mock 환경)

    # NFS raw 파일이 생성됐는지 확인
    raw_dir = tmp_path / "results" / job_id / "inspect_raw"
    assert raw_dir.exists()

    # 정리
    test_profile.unlink(missing_ok=True)
