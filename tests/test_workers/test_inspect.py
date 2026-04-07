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


def test_profile_path():
    p = _profile_path("gpu_server")
    assert p.name == "gpu_server.json"
    assert "checks/profiles" in str(p)


def test_script_path_preflight():
    p = _script_path("preflight", "sw_gpu_hw")
    assert p.name == "sw_gpu_hw.py"
    assert "preflight" in str(p)


def test_script_path_post_install():
    p = _script_path("post_install", "sw_gpu_sw")
    assert p.name == "sw_gpu_sw.py"
    assert "post_install" in str(p)


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


def test_gpu_server_profile_loads():
    p = _profile_path("gpu_server")
    assert p.exists(), "gpu_server.json 프로파일이 없습니다"
    with p.open() as f:
        profile = json.load(f)
    assert "phases" in profile
    assert "preflight" in profile["phases"]
    assert "post_install" in profile["phases"]
    assert "collect" in profile["phases"]


def test_gpu_server_profile_preflight_scripts():
    with _profile_path("gpu_server").open() as f:
        profile = json.load(f)
    scripts = profile["phases"]["preflight"]["scripts"]
    assert "sw_gpu_hw" in scripts
    assert "sw_storage_hw" in scripts
    assert "sw_cpu" in scripts


def test_gpu_server_profile_post_install_scripts():
    with _profile_path("gpu_server").open() as f:
        profile = json.load(f)
    scripts = profile["phases"]["post_install"]["scripts"]
    assert "sw_gpu_sw" in scripts
    assert "sw_storage_sw" in scripts
    assert "stress_gpu" in scripts
    assert "nccl_bandwidth" in scripts


def test_gpu_server_profile_has_validation_rules():
    with _profile_path("gpu_server").open() as f:
        profile = json.load(f)
    assert "validation" in profile
    assert "rules" in profile["validation"]
    assert len(profile["validation"]["rules"]) > 0


def test_gpu_server_profile_has_cleanup():
    with _profile_path("gpu_server").open() as f:
        profile = json.load(f)
    assert "cleanup" in profile
    assert "remove_packages" in profile["cleanup"]


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
    stdout = '{"check":"sw_gpu_hw","status":"pass","detail":"gpu_count=8"}'
    out = _parse_output(stdout, "sw_gpu_hw")
    assert out["status"] == "pass"
    assert out["check"] == "sw_gpu_hw"


def test_parse_invalid_json_returns_fail():
    out = _parse_output("not json output", "sw_gpu_hw")
    assert out["status"] == "fail"
    assert "JSON parse error" in out["detail"]


def test_parse_warn_status():
    stdout = '{"check":"sw_storage_hw","status":"warn","detail":"md_degraded=0"}'
    out = _parse_output(stdout, "sw_storage_hw")
    assert out["status"] == "warn"


# ---------------------------------------------------------------------------
# 스크립트 파일 존재 확인
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script_name",
    [
        "sw_gpu_hw",
        "sw_cpu",
        "sw_memory",
        "sw_storage_hw",
        "sw_network",
        "sw_os_version",
        "sw_power_mgmt",
        "sw_auto_update",
    ],
)
def test_preflight_scripts_exist(script_name):
    p = _script_path("preflight", script_name)
    assert p.exists(), f"preflight/{script_name}.py 가 없습니다"


@pytest.mark.parametrize(
    "script_name",
    [
        "sw_gpu_sw",
        "sw_storage_sw",
        "stress_gpu",
        "stress_cpu",
        "nccl_bandwidth",
    ],
)
def test_post_install_scripts_exist(script_name):
    p = _script_path("post_install", script_name)
    assert p.exists(), f"post_install/{script_name}.py 가 없습니다"


def test_collect_script_exists():
    p = _script_path("collect", "collect_all_logs")
    assert p.exists(), "collect/collect_all_logs.py 가 없습니다"


def test_script_path_collect():
    p = _script_path("collect", "collect_all_logs")
    assert p.name == "collect_all_logs.py"
    assert "collect" in str(p)


def test_gpu_server_profile_collect_scripts():
    with _profile_path("gpu_server").open() as f:
        profile = json.load(f)
    scripts = profile["phases"]["collect"]["scripts"]
    assert "collect_all_logs" in scripts


# ---------------------------------------------------------------------------
# _async_preflight NFS 파일 생성 확인 (SSH + DB mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_preflight_creates_nfs_dir(tmp_path, monkeypatch):
    """SSH 성공 시나리오 — NFS 결과 디렉토리 생성 확인."""
    job_id = str(uuid.uuid4())
    monkeypatch.setattr("workers.inspect.settings.nfs_base_path", str(tmp_path))
    monkeypatch.setattr("workers.inspect.settings.ssh_key_dir", str(tmp_path / "keys"))

    # 테스트 프로파일 (스크립트 없는 빈 preflight)
    profiles_dir = Path(__file__).parent.parent.parent / "checks" / "profiles"
    test_profile = profiles_dir / "_test_preflight.json"
    test_profile.write_text(
        json.dumps(
            {
                "pre_install": {"baseline": [], "stress_tools": []},
                "phases": {"preflight": {"scripts": []}},
            }
        )
    )

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="", stderr=""))

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(
                return_value=MagicMock(
                    id=uuid.UUID(job_id),
                    status="pending",
                    updated_at=None,
                    sw_requirements=None,
                    target_host="10.0.0.1",
                    target_user="root",
                    product_profile="_test_preflight",
                )
            )
        )
    )
    mock_session.commit = AsyncMock()

    mock_run_post_install = MagicMock()
    mock_run_post_install.apply_async = MagicMock()

    with (
        patch("asyncssh.connect", return_value=mock_conn),
        patch(
            "workers.inspect._make_session",
            return_value=(MagicMock(), MagicMock(return_value=mock_session)),
        ),
        patch("workers.inspect.run_post_install", mock_run_post_install),
    ):
        from workers.inspect import _async_preflight

        try:
            await _async_preflight(job_id, "10.0.0.1", "root", "_test_preflight", None)
        except Exception:
            pass

    # NFS 디렉토리 생성 확인
    raw_dir = tmp_path / "results" / job_id / "inspect_raw"
    assert raw_dir.exists()

    test_profile.unlink(missing_ok=True)
