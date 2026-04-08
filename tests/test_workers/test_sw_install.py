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
        MagicMock(stdout="account_created", stderr="", exit_status=0),  # _run_sudo (useradd)
        MagicMock(stdout="", stderr="", exit_status=0),  # sudo -S chpasswd
        MagicMock(stdout="uid=1001(testuser)", stderr="", exit_status=0),  # id verify
    ]
    conn = MagicMock()
    conn.run = AsyncMock(side_effect=run_results)

    result = await _handle_account(conn, secret, item)
    assert result["status"] == "pass"
    assert "testuser" in result["name"]


@pytest.mark.asyncio
async def test_handle_account_single_quote_password():
    """싱글쿼트 포함 패스워드 — chpasswd stdin 전달이므로 bash 파싱 오류 없음."""
    from pydantic import SecretStr

    from workers.sw_install import _handle_account

    item = {
        "type": "account",
        "username": "alice",
        "password": "p'assword",  # 싱글쿼트 포함
        "sudo": False,
        "agent_required": False,
    }
    secret = SecretStr("rootpw")

    run_results = [
        MagicMock(stdout="account_created", stderr="", exit_status=0),
        MagicMock(stdout="", stderr="", exit_status=0),
        MagicMock(stdout="uid=1002(alice)", stderr="", exit_status=0),
    ]
    conn = MagicMock()
    conn.run = AsyncMock(side_effect=run_results)

    # 예외 없이 실행되고 pass 반환
    result = await _handle_account(conn, secret, item)
    assert result["status"] == "pass"

    # chpasswd 호출 시 stdin에 패스워드가 포함돼야 함 (쉘 스크립트 텍스트에는 없음)
    chpasswd_call = conn.run.call_args_list[1]
    assert "chpasswd" in chpasswd_call.args[0]
    chpasswd_input = chpasswd_call.kwargs.get("input", "")
    assert "p'assword" in chpasswd_input


@pytest.mark.asyncio
async def test_handle_account_username_not_in_script_text():
    """username이 _run_sudo 스크립트 인자에 shlex.quote 처리되어 들어가는지 확인."""
    from pydantic import SecretStr

    from workers.sw_install import _handle_account

    item = {
        "type": "account",
        "username": "normal_user",
        "password": "pw",
        "sudo": True,
        "agent_required": False,
    }
    secret = SecretStr("rootpw")

    run_results = [
        MagicMock(stdout="account_created", stderr="", exit_status=0),
        MagicMock(stdout="", stderr="", exit_status=0),
        MagicMock(stdout="uid=1003(normal_user)", stderr="", exit_status=0),
    ]
    conn = MagicMock()
    conn.run = AsyncMock(side_effect=run_results)

    result = await _handle_account(conn, secret, item)
    assert result["status"] == "pass"

    # 첫 번째 run은 _run_sudo (sudo -S bash -s); input에 useradd 스크립트 포함
    first_call_input = conn.run.call_args_list[0].kwargs.get("input", "")
    assert "normal_user" in first_call_input
    assert "usermod" in first_call_input  # sudo=True


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


# ---------------------------------------------------------------------------
# _install_python 버전 파싱
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version_input, expected_pkg",
    [
        ("3.11", "python3.11"),  # 2파트 — 기존 정상 케이스
        ("3.11.5", "python3.11"),  # 3파트 — 패치 제거 후 major.minor만 사용
        ("3.10.14", "python3.10"),  # 3파트 다른 버전
        ("11", "python3.11"),  # minor만 — 3. 접두사 추가
        ("12", "python3.12"),
    ],
)
@pytest.mark.asyncio
async def test_install_python_version_parsing(version_input, expected_pkg):
    """_install_python 이 올바른 apt 패키지명을 구성하는지 확인."""
    from pydantic import SecretStr

    from workers.sw_install import _install_python

    secret = SecretStr("rootpw")
    calls: list[str] = []

    async def mock_run(cmd, **kwargs):
        calls.append(kwargs.get("input", ""))
        return MagicMock(stdout="python_installed", stderr="", exit_status=0)

    conn = MagicMock()
    conn.run = mock_run

    await _install_python(conn, secret, version_input)

    # 첫 번째 conn.run 호출(_run_sudo)의 stdin에 올바른 패키지명이 포함돼야 함
    script_input = calls[0]
    assert expected_pkg in script_input, (
        f"version={version_input!r}: expected {expected_pkg!r} in script, got: {script_input[:300]}"
    )


@pytest.mark.asyncio
async def test_handle_storage_mount_empty_mount_point():
    from workers.sw_install import _handle_storage_mount

    item = {"type": "storage_mount", "mount_point": "", "agent_required": False}
    conn = MagicMock()
    result = await _handle_storage_mount(conn, None, item)
    assert result["status"] == "fail"
    assert "mount_point empty" in result["detail"]
