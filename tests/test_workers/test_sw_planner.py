"""
SW Planner 유닛 테스트.
Claude API 호출은 모두 mock — 실제 API 요청 없음.
"""

import pytest
from unittest.mock import AsyncMock, patch

from workers.sw_planner import (
    _check_compat,
    _parse_account,
    _parse_storage_mount,
    _parse_sw_install,
    build_plan,
    parse,
)


# ---------------------------------------------------------------------------
# parse() — 불릿 라인 파싱
# ---------------------------------------------------------------------------


def test_parse_empty_returns_empty():
    assert parse("") == []


def test_parse_non_bullet_lines_ignored():
    md = "## 요구사항\nCUDA 12.4\n\n요약 텍스트"
    assert parse(md) == []


def test_parse_sw_install_cuda():
    items = parse("- CUDA 12.4")
    assert len(items) == 1
    assert items[0]["type"] == "sw_install"
    assert items[0]["name"] == "cuda"
    assert items[0]["version"] == "12.4"
    assert items[0]["agent_required"] is False


def test_parse_sw_install_nvidia_driver():
    items = parse("- nvidia-driver 550")
    assert items[0]["name"] == "nvidia_driver"
    assert items[0]["version"] == "550"


def test_parse_sw_install_pytorch_alias():
    items = parse("- PyTorch 2.3")
    assert items[0]["name"] == "torch"
    assert items[0]["version"] == "2.3"


def test_parse_sw_install_docker_no_version():
    items = parse("- docker")
    assert items[0]["name"] == "docker"
    assert items[0]["version"] is None


def test_parse_sw_install_docker_container_toolkit():
    items = parse("- docker-container-toolkit")
    assert items[0]["name"] == "docker_container_toolkit"


def test_parse_sw_install_tt_kmd():
    items = parse("- tt-kmd")
    assert items[0]["name"] == "tt_kmd"


def test_parse_account_basic():
    items = parse("- 계정: user1 / pw: secret123 / sudo: yes")
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "account"
    assert item["username"] == "user1"
    assert item["password"] == "secret123"
    assert item["sudo"] is True
    assert item["agent_required"] is False


def test_parse_account_no_sudo():
    items = parse("- 계정: devuser / pw: pass456 / sudo: no")
    assert items[0]["sudo"] is False


def test_parse_storage_mount():
    items = parse("- 보조스토리지 /mnt/data 마운트")
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "storage_mount"
    assert item["mount_point"] == "/mnt/data"
    assert item["agent_required"] is False


def test_parse_sys_config_grub():
    items = parse("- grub 파라미터: iommu=pt pcie_aspm=off")
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "sys_config"
    assert item["agent_required"] is True
    assert "grub" in item["raw"].lower()


def test_parse_sys_config_crontab():
    items = parse("- crontab: 0 3 * * * /opt/backup.sh")
    assert items[0]["type"] == "sys_config"
    assert items[0]["agent_required"] is True


def test_parse_multiple_items():
    md = """
- nvidia-driver 550
- CUDA 12.4
- PyTorch 2.3
- docker
- 계정: user1 / pw: xxxx / sudo: yes
- 보조스토리지 /mnt/data 마운트
"""
    items = parse(md)
    types = [i["type"] for i in items]
    assert types.count("sw_install") == 4
    assert types.count("account") == 1
    assert types.count("storage_mount") == 1


def test_parse_star_bullet():
    items = parse("* CUDA 12.4")
    assert items[0]["name"] == "cuda"


# ---------------------------------------------------------------------------
# _parse_account
# ---------------------------------------------------------------------------


def test_parse_account_empty_line():
    result = _parse_account("계정: / pw: / sudo: no")
    assert result["username"] == ""
    assert result["password"] == ""
    assert result["sudo"] is False


# ---------------------------------------------------------------------------
# _parse_storage_mount
# ---------------------------------------------------------------------------


def test_parse_storage_mount_no_path():
    result = _parse_storage_mount("보조스토리지 마운트")
    assert result["mount_point"] == ""


# ---------------------------------------------------------------------------
# _parse_sw_install
# ---------------------------------------------------------------------------


def test_parse_sw_install_unknown_package():
    """알 수 없는 패키지는 이름 그대로 사용."""
    result = _parse_sw_install("vim")
    assert result["name"] == "vim"
    assert result["version"] is None


def test_parse_sw_install_python_version():
    result = _parse_sw_install("python 3.11")
    assert result["name"] == "python"
    assert result["version"] == "3.11"


# ---------------------------------------------------------------------------
# _check_compat — 호환성 확인
# ---------------------------------------------------------------------------

_MATRIX = {
    "nvidia_driver": {
        "550": {"cuda": ["12.4", "12.5"], "gcc_min": "12"},
        "560": {"cuda": ["12.6", "12.7"], "gcc_min": "11"},
    },
    "cuda": {
        "12.4": {"torch": ["2.3"], "cudnn": ["9.x"]},
        "12.6": {"torch": ["2.4", "2.5"], "cudnn": ["9.x"]},
    },
}


def _sw(name: str, version: str | None) -> dict:
    return {"type": "sw_install", "name": name, "version": version, "agent_required": False}


def test_compat_hit_no_agent_required():
    """driver 550 + cuda 12.4 → 매트릭스 hit → agent_required=False."""
    items = [_sw("nvidia_driver", "550"), _sw("cuda", "12.4")]
    result = _check_compat(items, _MATRIX)
    assert all(i["agent_required"] is False for i in result)


def test_compat_hit_cuda_torch():
    """cuda 12.4 + torch 2.3 → 매트릭스 hit → agent_required=False."""
    items = [_sw("cuda", "12.4"), _sw("torch", "2.3")]
    result = _check_compat(items, _MATRIX)
    assert all(i["agent_required"] is False for i in result)


def test_compat_miss_driver_cuda_sets_agent_required():
    """driver 560 + cuda 12.4 → 매트릭스 miss → nvidia_driver, cuda → agent_required."""
    items = [_sw("nvidia_driver", "560"), _sw("cuda", "12.4")]
    result = _check_compat(items, _MATRIX)
    result_map = {i["name"]: i for i in result}
    assert result_map["nvidia_driver"]["agent_required"] is True
    assert result_map["cuda"]["agent_required"] is True


def test_compat_miss_cuda_torch_sets_agent_required():
    """cuda 12.4 + torch 2.5 → 매트릭스 miss → cuda, torch → agent_required."""
    items = [_sw("cuda", "12.4"), _sw("torch", "2.5")]
    result = _check_compat(items, _MATRIX)
    result_map = {i["name"]: i for i in result}
    assert result_map["cuda"]["agent_required"] is True
    assert result_map["torch"]["agent_required"] is True


def test_compat_unknown_driver_version_sets_agent_required():
    """매트릭스에 없는 driver 버전 → agent_required."""
    items = [_sw("nvidia_driver", "999"), _sw("cuda", "12.4")]
    result = _check_compat(items, _MATRIX)
    result_map = {i["name"]: i for i in result}
    assert result_map["nvidia_driver"]["agent_required"] is True


def test_compat_empty_matrix_returns_unchanged():
    """매트릭스 없음 → 항목 그대로 반환."""
    items = [_sw("nvidia_driver", "550"), _sw("cuda", "12.4")]
    result = _check_compat(items, {})
    assert result == items


def test_compat_no_version_not_checked():
    """버전 없는 항목은 호환성 확인 대상 아님."""
    items = [_sw("cuda", None), _sw("torch", None)]
    result = _check_compat(items, _MATRIX)
    assert all(i["agent_required"] is False for i in result)


def test_compat_non_sw_items_unchanged():
    """sw_install 이외 타입은 그대로 유지."""
    items = [
        {"type": "account", "username": "user1", "agent_required": False},
        _sw("nvidia_driver", "560"),
        _sw("cuda", "12.4"),
    ]
    result = _check_compat(items, _MATRIX)
    account = next(i for i in result if i["type"] == "account")
    assert account["agent_required"] is False


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_plan_no_agent_items():
    """호환 조합 → agent 호출 없음, has_agent_items=False."""
    md = "- nvidia-driver 550\n- CUDA 12.4\n- PyTorch 2.3"
    with patch("workers.sw_planner._load_compat_matrix", return_value=_MATRIX):
        result = await build_plan("job-1", md)

    assert result["has_agent_items"] is False
    assert result["agent_plan"] is None
    assert len(result["items"]) == 3


@pytest.mark.asyncio
async def test_build_plan_with_compat_miss_calls_agent():
    """호환성 miss → agent 호출."""
    md = "- nvidia-driver 560\n- CUDA 12.4"
    mock_agent_result = {
        "plan": [{"name": "cuda", "version": "12.6", "action": "install"}],
        "reason": "호환 버전으로 변경",
    }
    with (
        patch("workers.sw_planner._load_compat_matrix", return_value=_MATRIX),
        patch("workers.sw_planner.call_sw_planner_agent", new_callable=AsyncMock) as mock_agent,
    ):
        mock_agent.return_value = mock_agent_result
        result = await build_plan("job-1", md)

    assert result["has_agent_items"] is True
    assert result["agent_plan"] == mock_agent_result
    mock_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_plan_sys_config_calls_agent():
    """sys_config 항목 → agent_required=True → agent 호출."""
    md = "- grub 파라미터: iommu=pt"
    with (
        patch("workers.sw_planner._load_compat_matrix", return_value={}),
        patch("workers.sw_planner.call_sw_planner_agent", new_callable=AsyncMock) as mock_agent,
    ):
        mock_agent.return_value = {"plan": [], "reason": "GRUB 파라미터 적용 계획 수립"}
        result = await build_plan("job-1", md)

    assert result["has_agent_items"] is True
    mock_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_plan_agent_error_stored_in_plan():
    """agent 호출 실패 → agent_plan에 error 기록, 예외 전파 안 함."""
    md = "- nvidia-driver 999\n- CUDA 12.4"
    with (
        patch("workers.sw_planner._load_compat_matrix", return_value=_MATRIX),
        patch("workers.sw_planner.call_sw_planner_agent", new_callable=AsyncMock) as mock_agent,
    ):
        mock_agent.side_effect = Exception("API 연결 실패")
        result = await build_plan("job-1", md)

    assert result["has_agent_items"] is True
    assert "error" in result["agent_plan"]


@pytest.mark.asyncio
async def test_build_plan_agent_call_passes_job_id():
    """agent 호출 시 job_id 전달 확인."""
    md = "- nvidia-driver 560\n- CUDA 12.4"
    with (
        patch("workers.sw_planner._load_compat_matrix", return_value=_MATRIX),
        patch("workers.sw_planner.call_sw_planner_agent", new_callable=AsyncMock) as mock_agent,
    ):
        mock_agent.return_value = {"plan": [], "reason": "ok"}
        await build_plan("job-uuid-999", md)

    call_kwargs = mock_agent.call_args.kwargs
    assert call_kwargs.get("job_id") == "job-uuid-999"
