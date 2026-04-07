"""
SW Install Worker 유닛 테스트.
SSH와 DB는 mock — 순수 로직(경로, 항목 분류, 의존성 정렬)만 검증.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.sw_install import (
    _grub_params_for_cpu,
    _make_result,
    _nfs_sw_dir,
    _sort_items,
    _split_items_by_reboot,
)


@pytest.fixture(autouse=True)
def mock_publish(monkeypatch):
    monkeypatch.setattr("workers.sw_install.publish_job_status", AsyncMock())


# ---------------------------------------------------------------------------
# 경로 헬퍼
# ---------------------------------------------------------------------------


def test_nfs_sw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("workers.sw_install.settings.nfs_base_path", str(tmp_path))
    job_id = str(uuid.uuid4())
    result = _nfs_sw_dir(job_id)
    assert result == tmp_path / "results" / job_id / "sw_install_raw"


# ---------------------------------------------------------------------------
# GRUB 파라미터 생성
# ---------------------------------------------------------------------------


def test_grub_params_intel():
    params = _grub_params_for_cpu("intel")
    assert "iommu=pt" in params
    assert "intel_iommu=on" in params
    assert "amd_iommu" not in params


def test_grub_params_amd():
    params = _grub_params_for_cpu("amd")
    assert "iommu=pt" in params
    assert "amd_iommu=on" in params
    assert "intel_iommu" not in params


def test_grub_params_unknown():
    params = _grub_params_for_cpu("unknown")
    assert "iommu=pt" in params
    assert "intel_iommu" not in params
    assert "amd_iommu" not in params


def test_grub_params_common_always_present():
    for vendor in ("intel", "amd", "unknown"):
        params = _grub_params_for_cpu(vendor)
        assert "pcie_aspm=off" in params
        assert "pcie_acs_override=downstream,multifunction" in params


# ---------------------------------------------------------------------------
# 항목 정렬 (_sort_items)
# ---------------------------------------------------------------------------


def _sw(name: str) -> dict:
    return {"type": "sw_install", "name": name, "version": None, "agent_required": False}


def test_sort_items_driver_before_cuda():
    items = [_sw("cuda"), _sw("nvidia_driver")]
    sorted_items = _sort_items(items)
    names = [i["name"] for i in sorted_items]
    assert names.index("nvidia_driver") < names.index("cuda")


def test_sort_items_gcc_before_driver():
    items = [_sw("nvidia_driver"), _sw("gcc")]
    sorted_items = _sort_items(items)
    names = [i["name"] for i in sorted_items]
    assert names.index("gcc") < names.index("nvidia_driver")


def test_sort_items_cuda_before_torch():
    items = [_sw("torch"), _sw("cuda"), _sw("cudnn")]
    sorted_items = _sort_items(items)
    names = [i["name"] for i in sorted_items]
    assert names.index("cuda") < names.index("torch")


def test_sort_items_unknown_goes_last():
    items = [_sw("unknown_pkg"), _sw("gcc")]
    sorted_items = _sort_items(items)
    names = [i["name"] for i in sorted_items]
    assert names.index("gcc") < names.index("unknown_pkg")


def test_sort_items_non_sw_appended_after_sw():
    account_item = {"type": "account", "username": "user1", "agent_required": False}
    items = [account_item, _sw("gcc")]
    sorted_items = _sort_items(items)
    assert sorted_items[0]["type"] == "sw_install"
    assert sorted_items[-1]["type"] == "account"


# ---------------------------------------------------------------------------
# pre/post reboot 분리 (_split_items_by_reboot)
# ---------------------------------------------------------------------------


def test_split_no_nvidia_driver():
    items = [_sw("cuda"), _sw("torch")]
    pre, post = _split_items_by_reboot(items)
    # nvidia_driver 없으면 분리 없음
    assert len(pre) == 2
    assert len(post) == 0


def test_split_with_nvidia_driver():
    items = [_sw("gcc"), _sw("nvidia_driver"), _sw("cuda"), _sw("torch"), _sw("cudnn")]
    pre, post = _split_items_by_reboot(items)
    pre_names = {i["name"] for i in pre}
    post_names = {i["name"] for i in post}
    assert "nvidia_driver" in pre_names
    assert "gcc" in pre_names
    assert "cuda" in post_names
    assert "torch" in post_names
    assert "cudnn" in post_names


def test_split_non_sw_items_go_to_pre():
    account = {"type": "account", "username": "u", "agent_required": False}
    items = [_sw("nvidia_driver"), account, _sw("cuda")]
    pre, post = _split_items_by_reboot(items)
    assert account in pre
    assert all(i["name"] != "account" for i in post if i.get("name"))


def test_split_docker_not_in_post():
    """docker은 _POST_REBOOT_DEPS 아님 → pre에 남아야 함."""
    items = [_sw("nvidia_driver"), _sw("docker"), _sw("cuda")]
    pre, post = _split_items_by_reboot(items)
    pre_names = {i["name"] for i in pre}
    assert "docker" in pre_names


# ---------------------------------------------------------------------------
# _make_result 헬퍼
# ---------------------------------------------------------------------------


def test_make_result_fields():
    r = _make_result("cuda", "pass", "installed ok")
    assert r["name"] == "cuda"
    assert r["type"] == "sw_install"
    assert r["status"] == "pass"
    assert r["detail"] == "installed ok"


def test_make_result_fail():
    r = _make_result("torch", "fail", "cuda not found")
    assert r["status"] == "fail"


# ---------------------------------------------------------------------------
# _detect_cpu_vendor (SSH mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_cpu_vendor_intel():
    from workers.sw_install import _detect_cpu_vendor

    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(stdout="Vendor ID:  GenuineIntel\n", exit_status=0))
    result = await _detect_cpu_vendor(conn)
    assert result == "intel"


@pytest.mark.asyncio
async def test_detect_cpu_vendor_amd():
    from workers.sw_install import _detect_cpu_vendor

    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(stdout="Vendor ID:  AuthenticAMD\n", exit_status=0))
    result = await _detect_cpu_vendor(conn)
    assert result == "amd"


@pytest.mark.asyncio
async def test_detect_cpu_vendor_unknown():
    from workers.sw_install import _detect_cpu_vendor

    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(stdout="Vendor ID: ARM\n", exit_status=0))
    result = await _detect_cpu_vendor(conn)
    assert result == "unknown"


# ---------------------------------------------------------------------------
# _detect_has_nvidia_gpu (SSH mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_has_nvidia_via_smi():
    from workers.sw_install import _detect_has_nvidia_gpu

    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(stdout="/usr/bin/nvidia-smi", exit_status=0))
    assert await _detect_has_nvidia_gpu(conn) is True


@pytest.mark.asyncio
async def test_detect_has_nvidia_via_lspci():
    from workers.sw_install import _detect_has_nvidia_gpu

    call_count = 0

    async def mock_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # command -v nvidia-smi
            return MagicMock(stdout="", exit_status=1)
        return MagicMock(stdout="00:02.0 3D controller: NVIDIA Corporation\n", exit_status=0)

    conn = MagicMock()
    conn.run = mock_run
    assert await _detect_has_nvidia_gpu(conn) is True


@pytest.mark.asyncio
async def test_detect_has_nvidia_false():
    from workers.sw_install import _detect_has_nvidia_gpu

    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(stdout="", exit_status=1))
    assert await _detect_has_nvidia_gpu(conn) is False


# ---------------------------------------------------------------------------
# _handle_account (SSH mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_account_success():
    from pydantic import SecretStr

    from workers.sw_install import _handle_account

    item = {
        "type": "account",
        "username": "testuser",
        "password": "pw123",
        "sudo": False,
        "agent_required": False,
    }
    secret = SecretStr("rootpw")

    run_results = [
        MagicMock(stdout="account_created", stderr="", exit_status=0),  # _run_sudo
        MagicMock(stdout="uid=1001(testuser)", stderr="", exit_status=0),  # id verify
    ]
    conn = MagicMock()
    conn.run = AsyncMock(side_effect=run_results)

    result = await _handle_account(conn, secret, item)
    assert result["status"] == "pass"
    assert "testuser" in result["name"]


@pytest.mark.asyncio
async def test_handle_account_empty_username():
    from workers.sw_install import _handle_account

    item = {
        "type": "account",
        "username": "",
        "password": "pw",
        "sudo": False,
        "agent_required": False,
    }
    conn = MagicMock()
    result = await _handle_account(conn, None, item)
    assert result["status"] == "fail"
    assert "username empty" in result["detail"]


# ---------------------------------------------------------------------------
# _handle_storage_mount (SSH mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_storage_mount_empty_mount_point():
    from workers.sw_install import _handle_storage_mount

    item = {"type": "storage_mount", "mount_point": "", "agent_required": False}
    conn = MagicMock()
    result = await _handle_storage_mount(conn, None, item)
    assert result["status"] == "fail"
    assert "mount_point empty" in result["detail"]
