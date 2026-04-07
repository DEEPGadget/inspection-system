"""
q_sw_install worker — SW 설치 실행/검증/재시도.

흐름:
  1. sw_planner.build_plan() → 설치계획 생성
  2. 표준 sys_config 항상 적용 (Ubuntu 직접, 그 외 OS → SW Planner Agent):
     - GRUB 커널 파라미터 (CPU 종류별)
     - CPU 거버너 performance 고정
     - GPU 영구 모드 (NVIDIA 탑재 시)
     - 자동 업데이트 방지
  3. 설치계획 항목별 처리:
     - sw_install (agent_required=False): apt/pip/conda 직접 설치
     - sw_install (agent_required=True): SW Planner Agent 위임
     - account: useradd/chpasswd/sudo
     - storage_mount: mkfs/fstab/mount
     - sys_config (비정형): SW Planner Agent 위임
  4. nvidia-driver 설치 시 → sudo reboot → 300s SSH 재접속 폴링 → 검증
  5. 설치 실패 항목 → SW Planner Agent 복구 시도, 복구 실패 시 job FAILED
  6. 완료 후 run_post_install 디스패치

에러 전파:
  - 복구 불가 실패 → _mark_failed + cleanup 디스패치, post_install skip
  - SSH 재접속 실패 (reboot_timeout) → FAILED
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings
from workers.app import app
from workers.notify import publish_job_status
from workers.ssh_client import secret_input, wrap_password

log = structlog.get_logger(__name__)

# CUDA 스택 의존성: nvidia_driver 설치 + reboot 후에만 설치 가능
_POST_REBOOT_DEPS = frozenset({"cuda", "cudnn", "torch"})

# 의존성 순서 인덱스 (낮을수록 먼저)
_INSTALL_ORDER: list[str] = [
    "gcc",
    "nvidia_driver",
    "cuda",
    "cudnn",
    "torch",
    "docker",
    "docker_container_toolkit",
    "miniconda",
    "python",
    "rustup",
    "tt_kmd",
    "tt_smi",
    "tt_burnin",
]


# ---------------------------------------------------------------------------
# 세션 팩토리 + 경로 헬퍼
# ---------------------------------------------------------------------------


def _make_session() -> tuple:
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_local


def _nfs_sw_dir(job_id: str) -> Path:
    return Path(settings.nfs_base_path) / "results" / job_id / "sw_install_raw"


def _ssh_key_path(target_host: str) -> str | None:
    key_dir = Path(settings.ssh_key_dir)
    for candidate in [key_dir / target_host, key_dir / "default"]:
        if candidate.exists():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------


async def _update_job(session: AsyncSession, job_id: str, **kwargs) -> None:
    from api.models import Job

    result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    for k, v in kwargs.items():
        setattr(job, k, v)
    job.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def _save_check_result(
    session: AsyncSession,
    job_id: str,
    check_name: str,
    status: str,
    detail: str,
    raw_output: dict,
) -> None:
    from api.models import CheckResult

    cr = CheckResult(
        job_id=uuid.UUID(job_id),
        check_name=check_name,
        status=status,
        detail=detail,
        raw_output=raw_output,
    )
    session.add(cr)
    await session.commit()


async def _mark_failed(job_id: str, message: str) -> None:
    engine, SessionLocal = _make_session()
    try:
        async with SessionLocal() as session:
            await _update_job(session, job_id, status="failed", error_message=message[:2000])
        await publish_job_status(job_id, "failed", message[:2000])
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# SSH 헬퍼
# ---------------------------------------------------------------------------


def _build_connect_kwargs(target_host: str, target_user: str) -> dict:
    kwargs: dict = {
        "host": target_host,
        "username": target_user,
        "known_hosts": None,  # TODO(W-3): known_hosts 명시 경로로 교체
    }
    key_path = _ssh_key_path(target_host)
    if key_path:
        kwargs["client_keys"] = [key_path]
    return kwargs


async def _run_sudo(
    conn: asyncssh.SSHClientConnection,
    script: str,
    secret,
    timeout: int = 120,
) -> tuple[bool, str]:
    """bash 스크립트를 sudo로 실행. 비밀번호는 stdin 첫 줄로 전달."""
    result = await conn.run(
        "sudo -S bash -s",
        input=f"{secret.get_secret_value()}\n{script}" if secret else script,
        check=False,
        timeout=timeout,
    )
    output = (result.stdout or "").strip()
    return result.exit_status == 0, output


async def _apt_install(
    conn: asyncssh.SSHClientConnection,
    packages: list[str],
    secret,
    timeout: int = 300,
) -> tuple[bool, str]:
    pkg_str = " ".join(packages)
    script = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg_str} 2>&1"
    return await _run_sudo(conn, script, secret, timeout)


# ---------------------------------------------------------------------------
# OS / 시스템 감지
# ---------------------------------------------------------------------------


async def _detect_os(conn: asyncssh.SSHClientConnection) -> dict:
    """os-release 파싱 → {'id': 'ubuntu', 'version_id': '22.04', ...}"""
    result = await conn.run("cat /etc/os-release", check=False)
    info: dict = {}
    for line in (result.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.lower()] = v.strip().strip('"')
    return info


async def _detect_cpu_vendor(conn: asyncssh.SSHClientConnection) -> str:
    """'intel' | 'amd' | 'unknown'"""
    result = await conn.run("lscpu | grep 'Vendor ID'", check=False)
    stdout = (result.stdout or "").lower()
    if "genuineintel" in stdout:
        return "intel"
    if "authenticamd" in stdout:
        return "amd"
    return "unknown"


async def _detect_has_nvidia_gpu(conn: asyncssh.SSHClientConnection) -> bool:
    result = await conn.run("command -v nvidia-smi", check=False)
    if result.exit_status != 0:
        # nvidia-smi 없어도 GPU 있을 수 있음 (드라이버 미설치 상태)
        result2 = await conn.run("lspci | grep -i nvidia", check=False)
        return bool((result2.stdout or "").strip())
    return True


# ---------------------------------------------------------------------------
# 표준 sys_config (Ubuntu 전용 직접 구현)
# ---------------------------------------------------------------------------


def _grub_params_for_cpu(cpu_vendor: str) -> str:
    """CPU 종류별 GRUB 파라미터 문자열 반환."""
    common = "iommu=pt pcie_aspm=off pcie_acs_override=downstream,multifunction"
    if cpu_vendor == "intel":
        extra = "intel_iommu=on intel_idle.max_cstate=0 processor.max_cstate=1"
    elif cpu_vendor == "amd":
        extra = "amd_iommu=on amd_pstate=passive"
    else:
        extra = ""
    return f"{common} {extra}".strip()


async def _apply_grub_params(
    conn: asyncssh.SSHClientConnection,
    secret,
    cpu_vendor: str,
) -> tuple[bool, str]:
    params = _grub_params_for_cpu(cpu_vendor)
    script = f"""\
GRUB_FILE=/etc/default/grub
if grep -q "iommu=pt" "$GRUB_FILE"; then
    echo "already_applied"
    exit 0
fi
CURRENT=$(grep "^GRUB_CMDLINE_LINUX_DEFAULT=" "$GRUB_FILE" | head -1 | sed 's/GRUB_CMDLINE_LINUX_DEFAULT="\\(.*\\)"/\\1/')
NEW_VALUE="${{CURRENT}} {params}"
sed -i "s|GRUB_CMDLINE_LINUX_DEFAULT=\\".*\\"|GRUB_CMDLINE_LINUX_DEFAULT=\\"${{NEW_VALUE}}\\"|" "$GRUB_FILE"
update-grub 2>&1
echo "grub_updated"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=60)
    return ok, out


async def _apply_cpu_governor(
    conn: asyncssh.SSHClientConnection,
    secret,
) -> tuple[bool, str]:
    script = """\
DEBIAN_FRONTEND=noninteractive apt-get install -y linux-tools-$(uname -r) linux-tools-generic 2>&1 || true
cpupower frequency-set -g performance 2>&1
systemctl enable --now cpupower.service 2>&1 || true
echo "cpu_governor_done"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=120)
    return ok, out


async def _apply_gpu_persistence_mode(
    conn: asyncssh.SSHClientConnection,
    secret,
) -> tuple[bool, str]:
    # PM 지원 여부 먼저 확인 (sudo 없이)
    pm_check = await conn.run("nvidia-smi -pm 1 2>&1", check=False)
    pm_out = (pm_check.stdout or "").lower()
    if "not supported" in pm_out or pm_check.exit_status != 0:
        if "not supported" in pm_out:
            return True, "persistence_mode_not_supported_skipped"
        # nvidia-smi 자체가 없으면 skip
        return True, "nvidia_smi_not_found_skipped"

    script = """\
cat > /etc/systemd/system/nvidia-power.service << 'EOF'
[Unit]
Description=NVIDIA GPU Persistence Mode
After=nvidia-persistenced.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nvidia-smi -pm 1

[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now nvidia-power.service 2>&1
echo "gpu_pm_done"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=30)
    return ok, out


async def _disable_auto_update(
    conn: asyncssh.SSHClientConnection,
    secret,
) -> tuple[bool, str]:
    script = """\
systemctl disable --now unattended-upgrades 2>/dev/null || true
systemctl disable --now apt-daily.timer 2>/dev/null || true
systemctl disable --now apt-daily-upgrade.timer 2>/dev/null || true
DEBIAN_FRONTEND=noninteractive apt-get purge -y unattended-upgrades 2>&1 || true
apt-mark hold linux-image-$(uname -r) linux-headers-$(uname -r) 2>&1 || true
echo "auto_update_disabled"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=60)
    return ok, out


async def _apply_sys_config(
    conn: asyncssh.SSHClientConnection,
    secret,
    cpu_vendor: str,
    has_nvidia: bool,
) -> list[dict]:
    """표준 sys_config 4가지 항목 적용. 결과 리스트 반환."""
    if not secret:
        log.warning("sw_install.sys_config.skip", reason="sudo_password not provided")
        return [
            {
                "name": "sys_config_standard",
                "type": "sys_config",
                "status": "warn",
                "detail": "sudo_password not provided — standard sys_config skipped",
            }
        ]

    results = []

    # 1. GRUB 파라미터
    ok, out = await _apply_grub_params(conn, secret, cpu_vendor)
    results.append(
        {
            "name": "sys_config_grub",
            "type": "sys_config",
            "status": "pass" if ok else "fail",
            "detail": out[:500],
        }
    )
    if not ok:
        log.warning("sw_install.sys_config.grub_failed", out=out[:200])

    # 2. CPU 거버너
    ok, out = await _apply_cpu_governor(conn, secret)
    results.append(
        {
            "name": "sys_config_cpu_governor",
            "type": "sys_config",
            "status": "pass" if ok else "warn",
            "detail": out[:500],
        }
    )

    # 3. GPU 영구 모드 (NVIDIA 있을 때만)
    if has_nvidia:
        ok, out = await _apply_gpu_persistence_mode(conn, secret)
        results.append(
            {
                "name": "sys_config_gpu_pm",
                "type": "sys_config",
                "status": "pass" if ok else "warn",
                "detail": out[:500],
            }
        )

    # 4. 자동 업데이트 방지
    ok, out = await _disable_auto_update(conn, secret)
    results.append(
        {
            "name": "sys_config_auto_update",
            "type": "sys_config",
            "status": "pass" if ok else "warn",
            "detail": out[:500],
        }
    )

    return results


# ---------------------------------------------------------------------------
# SW 항목별 설치 함수
# ---------------------------------------------------------------------------


async def _install_nvidia_driver(
    conn: asyncssh.SSHClientConnection,
    secret,
    version: str | None,
    os_info: dict,
) -> tuple[bool, str]:
    ver_str = version or ""
    pkg = f"nvidia-driver-{ver_str}" if ver_str else "nvidia-driver"
    script = f"""\
DEBIAN_FRONTEND=noninteractive
wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu{os_info.get("version_id", "2204").replace(".", "")}/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb
apt-get update -q
apt-get install -y gcc-12 g++-12
update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 12
update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 12
DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg} 2>&1
echo "nvidia_driver_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=600)
    return ok, out[:500]


async def _install_cuda(
    conn: asyncssh.SSHClientConnection,
    secret,
    version: str | None,
) -> tuple[bool, str]:
    ver_str = (version or "").replace(".", "-")
    pkg = f"cuda-toolkit-{ver_str}" if ver_str else "cuda-toolkit"
    script = f"""\
DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg} 2>&1
# PATH 추가 (이미 있으면 skip)
BASHRC=~/.bashrc
grep -q "cuda/bin" "$BASHRC" || echo 'export PATH=/usr/local/cuda/bin:$PATH' >> "$BASHRC"
grep -q "cuda/lib64" "$BASHRC" || echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> "$BASHRC"
echo "cuda_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=600)
    if ok:
        verify = await conn.run("nvcc --version 2>&1", check=False)
        if verify.exit_status != 0:
            return False, f"nvcc not found after install: {(verify.stdout or '')[:200]}"
    return ok, out[:500]


async def _install_cudnn(
    conn: asyncssh.SSHClientConnection,
    secret,
) -> tuple[bool, str]:
    script = "DEBIAN_FRONTEND=noninteractive apt-get install -y cudnn 2>&1\necho cudnn_installed"
    ok, out = await _run_sudo(conn, script, secret, timeout=300)
    return ok, out[:500]


async def _install_torch(
    conn: asyncssh.SSHClientConnection,
    secret,
    version: str | None,
    cuda_version: str | None,
) -> tuple[bool, str]:
    ver_spec = f"=={version}" if version else ""
    cuda_short = (cuda_version or "").replace(".", "")
    whl_url = (
        f"https://download.pytorch.org/whl/cu{cuda_short}"
        if cuda_short
        else "https://download.pytorch.org/whl/cu124"
    )
    cmd = f"pip install torch{ver_spec} torchvision torchaudio --index-url {whl_url} 2>&1"
    result = await conn.run(cmd, check=False, timeout=600)
    ok = result.exit_status == 0
    if ok:
        verify = await conn.run(
            'python3 -c "import torch; print(torch.cuda.is_available())"',
            check=False,
            timeout=30,
        )
        if (verify.stdout or "").strip() != "True":
            return False, f"torch.cuda.is_available() != True: {(verify.stdout or '')[:100]}"
    return ok, (result.stdout or "")[:500]


async def _install_docker(
    conn: asyncssh.SSHClientConnection,
    secret,
    os_info: dict,
) -> tuple[bool, str]:
    codename_expr = '. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}"'
    script = f"""\
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
CODENAME=$({codename_expr})
tee /etc/apt/sources.list.d/docker.sources > /dev/null << EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $CODENAME
Components: stable
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl start docker
echo "docker_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=300)
    if ok:
        verify = await conn.run("sudo docker run hello-world 2>&1", check=False, timeout=60)
        ok = verify.exit_status == 0
        if not ok:
            return False, f"docker hello-world failed: {(verify.stdout or '')[:200]}"
    return ok, out[:500]


async def _install_docker_container_toolkit(
    conn: asyncssh.SSHClientConnection,
    secret,
) -> tuple[bool, str]:
    script = """\
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -q
VER=1.19.0-1
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  nvidia-container-toolkit=${VER} \
  nvidia-container-toolkit-base=${VER} \
  libnvidia-container-tools=${VER} \
  libnvidia-container1=${VER}
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
echo "nct_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=300)
    return ok, out[:500]


async def _install_miniconda(conn: asyncssh.SSHClientConnection) -> tuple[bool, str]:
    script = """\
wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
bash /tmp/miniconda.sh -b -u -p ~/miniconda3
~/miniconda3/bin/conda init bash
echo "miniconda_installed"
"""
    result = await conn.run("bash -s", input=script, check=False, timeout=300)
    ok = result.exit_status == 0
    if ok:
        verify = await conn.run(
            "~/miniconda3/bin/conda list 2>&1 | head -3", check=False, timeout=30
        )
        ok = verify.exit_status == 0
    return ok, (result.stdout or "")[:500]


async def _install_python(
    conn: asyncssh.SSHClientConnection,
    secret,
    version: str | None,
) -> tuple[bool, str]:
    if not version:
        return False, "python version not specified"
    minor = version.split(".")[-1] if "." in version else version
    full_ver = f"3.{minor}" if not version.startswith("3.") else version
    script = f"""\
DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y python{full_ver}
echo "python_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=180)
    if ok:
        verify = await conn.run(f"python{full_ver} --version 2>&1", check=False, timeout=10)
        ok = verify.exit_status == 0
    return ok, out[:500]


async def _install_gcc(
    conn: asyncssh.SSHClientConnection,
    secret,
    version: str | None,
) -> tuple[bool, str]:
    ver = version or "12"
    major = ver.split(".")[0]
    script = f"""\
DEBIAN_FRONTEND=noninteractive apt-get install -y gcc-{major} g++-{major}
update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-{major} {major}
update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-{major} {major}
echo "gcc_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=120)
    return ok, out[:500]


async def _install_rustup(conn: asyncssh.SSHClientConnection) -> tuple[bool, str]:
    script = """\
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
echo "rustup_installed"
"""
    result = await conn.run("bash -s", input=script, check=False, timeout=180)
    ok = result.exit_status == 0
    return ok, (result.stdout or "")[:500]


async def _install_tt_kmd(conn: asyncssh.SSHClientConnection, secret) -> tuple[bool, str]:
    script = """\
DEBIAN_FRONTEND=noninteractive apt-get install -y dkms
git clone https://github.com/tenstorrent/tt-kmd.git ~/tt-kmd
cd ~/tt-kmd
make dkms
echo "tt_kmd_installed"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=300)
    if ok:
        verify = await conn.run("ls /dev/tenstorrent 2>&1", check=False, timeout=10)
        ok = verify.exit_status == 0
    return ok, out[:500]


async def _install_tt_smi(conn: asyncssh.SSHClientConnection) -> tuple[bool, str]:
    script = """\
source "$HOME/.cargo/env" 2>/dev/null || true
pip install tt-smi 2>&1
echo "tt_smi_installed"
"""
    result = await conn.run("bash -s", input=script, check=False, timeout=180)
    ok = result.exit_status == 0
    if ok:
        verify = await conn.run("tt-smi -ls 2>&1", check=False, timeout=30)
        ok = verify.exit_status == 0
    return ok, (result.stdout or "")[:500]


async def _install_tt_burnin(conn: asyncssh.SSHClientConnection) -> tuple[bool, str]:
    script = """\
git clone https://github.com/tenstorrent/tt-burnin.git ~/tt-burnin
cd ~/tt-burnin
pip3 install --upgrade pip
pip3 install . 2>&1
echo "tt_burnin_installed"
"""
    result = await conn.run("bash -s", input=script, check=False, timeout=300)
    ok = result.exit_status == 0
    if ok:
        verify = await conn.run("tt-burnin --help 2>&1", check=False, timeout=10)
        ok = verify.exit_status == 0
    return ok, (result.stdout or "")[:500]


async def _install_unknown(
    conn: asyncssh.SSHClientConnection,
    secret,
    name: str,
    version: str | None,
) -> tuple[bool, str]:
    """알 수 없는 패키지: apt 시도 후 pip fallback."""
    pkg = f"{name}={version}" if version else name
    script = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg} 2>&1"
    ok, out = await _run_sudo(conn, script, secret, timeout=120)
    if not ok:
        ver_spec = f"=={version}" if version else ""
        pip_result = await conn.run(f"pip install {name}{ver_spec} 2>&1", check=False, timeout=120)
        ok = pip_result.exit_status == 0
        out = pip_result.stdout or ""
    return ok, out[:500]


# ---------------------------------------------------------------------------
# SW 항목 디스패처
# ---------------------------------------------------------------------------


async def _install_sw_item(
    conn: asyncssh.SSHClientConnection,
    secret,
    item: dict,
    installed_set: set[str],
    failed_deps: set[str],
    os_info: dict,
    cuda_version: str | None,
) -> dict:
    """단일 sw_install 항목 처리. 의존성 실패 시 skip."""
    name = item.get("name", "")
    version = item.get("version")

    # 의존성 실패 체크
    if name in _POST_REBOOT_DEPS and "nvidia_driver" in failed_deps:
        return _make_result(name, "warn", "skipped_due_to_dependency: nvidia_driver failed")
    if name == "docker_container_toolkit" and "docker" in failed_deps:
        return _make_result(name, "warn", "skipped_due_to_dependency: docker failed")
    if name == "tt_smi" and "rustup" in failed_deps:
        return _make_result(name, "warn", "skipped_due_to_dependency: rustup failed")
    if name == "tt_burnin" and "tt_kmd" in failed_deps:
        return _make_result(name, "warn", "skipped_due_to_dependency: tt_kmd failed")

    log.info("sw_install.item.start", name=name, version=version)

    try:
        if name == "nvidia_driver":
            ok, detail = await _install_nvidia_driver(conn, secret, version, os_info)
        elif name == "cuda":
            ok, detail = await _install_cuda(conn, secret, version)
        elif name == "cudnn":
            ok, detail = await _install_cudnn(conn, secret)
        elif name == "torch":
            ok, detail = await _install_torch(conn, secret, version, cuda_version)
        elif name == "docker":
            ok, detail = await _install_docker(conn, secret, os_info)
        elif name == "docker_container_toolkit":
            ok, detail = await _install_docker_container_toolkit(conn, secret)
        elif name == "miniconda":
            ok, detail = await _install_miniconda(conn)
        elif name == "python":
            ok, detail = await _install_python(conn, secret, version)
        elif name == "gcc":
            ok, detail = await _install_gcc(conn, secret, version)
        elif name == "rustup":
            ok, detail = await _install_rustup(conn)
        elif name == "tt_kmd":
            ok, detail = await _install_tt_kmd(conn, secret)
        elif name == "tt_smi":
            ok, detail = await _install_tt_smi(conn)
        elif name == "tt_burnin":
            ok, detail = await _install_tt_burnin(conn)
        else:
            ok, detail = await _install_unknown(conn, secret, name, version)

        status = "pass" if ok else "fail"
        log.info("sw_install.item.done", name=name, status=status)
        return _make_result(name, status, detail)

    except (asyncssh.misc.DisconnectError, TimeoutError) as exc:
        log.warning("sw_install.item.timeout", name=name, error=str(exc))
        return _make_result(name, "fail", f"ssh_timeout: {exc}")


def _make_result(name: str, status: str, detail: str) -> dict:
    return {"name": name, "type": "sw_install", "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# 계정 생성 / 스토리지 마운트 / 비정형 sys_config
# ---------------------------------------------------------------------------


async def _handle_account(
    conn: asyncssh.SSHClientConnection,
    secret,
    item: dict,
) -> dict:
    username = item.get("username", "")
    password = item.get("password", "")
    sudo = item.get("sudo", False)

    if not username:
        return {
            "name": "account_create",
            "type": "account",
            "status": "fail",
            "detail": "username empty",
        }

    sudo_line = f"usermod -aG sudo {username}" if sudo else "true"
    script = f"""\
useradd -m -s /bin/bash {username} 2>&1 || true
echo '{username}:{password}' | chpasswd
{sudo_line}
echo "account_created"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=30)
    if ok:
        verify = await conn.run(f"id {username}", check=False, timeout=10)
        ok = verify.exit_status == 0

    return {
        "name": f"account_{username}",
        "type": "account",
        "status": "pass" if ok else "fail",
        "detail": out[:200],
    }


async def _handle_storage_mount(
    conn: asyncssh.SSHClientConnection,
    secret,
    item: dict,
) -> dict:
    mount_point = item.get("mount_point", "")
    if not mount_point:
        return {
            "name": "storage_mount",
            "type": "storage_mount",
            "status": "fail",
            "detail": "mount_point empty",
        }

    script = f"""\
# 마운트되지 않은 보조 디스크 탐색 (첫 번째)
DEVICE=$(lsblk -rno NAME,MOUNTPOINT,TYPE | awk '$2=="" && $3=="disk" {{print $1; exit}}')
if [ -z "$DEVICE" ]; then
    echo "no_unmounted_disk"
    exit 1
fi
DEVICE="/dev/$DEVICE"
# fstab 백업
cp /etc/fstab /etc/fstab.bak
# ext4 포맷
mkfs.ext4 "$DEVICE" -F 2>&1
# 마운트 포인트 생성
mkdir -p {mount_point}
# UUID 기반 fstab 등록
UUID=$(blkid -s UUID -o value "$DEVICE")
echo "UUID=${{UUID}} {mount_point} ext4 defaults 0 2" >> /etc/fstab
# 마운트 확인
mount -a 2>&1
echo "storage_mounted"
"""
    ok, out = await _run_sudo(conn, script, secret, timeout=120)
    if ok:
        verify = await conn.run(f"mountpoint {mount_point}", check=False, timeout=10)
        if verify.exit_status != 0:
            # fstab 복구
            await _run_sudo(conn, "cp /etc/fstab.bak /etc/fstab", secret, timeout=10)
            return {
                "name": f"storage_mount_{mount_point.replace('/', '_')}",
                "type": "storage_mount",
                "status": "fail",
                "detail": f"mountpoint check failed: {mount_point}",
            }

    return {
        "name": f"storage_mount_{mount_point.replace('/', '_')}",
        "type": "storage_mount",
        "status": "pass" if ok else "fail",
        "detail": out[:300],
    }


async def _handle_sys_config_agent(
    conn: asyncssh.SSHClientConnection,
    secret,
    job_id: str,
    item: dict,
    sw_requirements: str,
) -> dict:
    """비정형 sys_config 항목 → SW Planner Agent 위임 후 SSH 실행."""
    from workers.agent_gateway import call_sw_planner_agent

    raw = item.get("raw", "")
    log.info("sw_install.sys_config_agent", job_id=job_id, raw=raw[:100])

    agent_result = await call_sw_planner_agent(
        job_id=job_id,
        sw_requirements=sw_requirements,
        failed_step=f"sys_config item: {raw}",
    )
    plan = agent_result.get("plan", [])
    if not plan:
        return {
            "name": f"sys_config_agent_{raw[:30]}",
            "type": "sys_config",
            "status": "fail",
            "detail": f"agent returned empty plan: {agent_result.get('reason', '')}",
        }

    ok, detail = await _execute_agent_plan(conn, secret, plan)
    return {
        "name": f"sys_config_agent_{raw[:30]}",
        "type": "sys_config",
        "status": "pass" if ok else "fail",
        "detail": detail,
    }


async def _execute_agent_plan(
    conn: asyncssh.SSHClientConnection,
    secret,
    plan: list[dict],
) -> tuple[bool, str]:
    """Agent plan의 cmd 항목을 순서대로 SSH 실행."""
    outputs = []
    for step in plan:
        cmd = step.get("cmd", "")
        if not cmd:
            continue
        description = step.get("description", cmd[:60])
        log.info("sw_install.agent_plan.step", cmd=cmd[:80])

        needs_sudo = cmd.strip().startswith("sudo") or step.get("sudo", False)
        if needs_sudo and secret:
            script = cmd.replace("sudo ", "", 1)
            ok, out = await _run_sudo(conn, script, secret, timeout=300)
        else:
            result = await conn.run(cmd, check=False, timeout=300)
            ok = result.exit_status == 0

        outputs.append(f"{description}: {'ok' if ok else 'FAIL'}")
        if not ok:
            return False, " | ".join(outputs)[-500:]

    return True, " | ".join(outputs)[-500:]


# ---------------------------------------------------------------------------
# Reboot 처리
# ---------------------------------------------------------------------------


async def _do_reboot(conn: asyncssh.SSHClientConnection, secret) -> None:
    """sudo reboot 실행 (연결 종료 예상)."""
    try:
        await conn.run(
            "sudo -S reboot",
            input=secret_input(secret),
            check=False,
            timeout=5,
        )
    except Exception:
        pass  # 서버 종료로 인한 연결 끊김은 정상


async def _poll_ssh_reconnect(
    connect_kwargs: dict,
    timeout: int = 300,
    interval: int = 10,
) -> asyncssh.SSHClientConnection | None:
    """SSH 재접속 폴링. 성공 시 connection 반환, 타임아웃 시 None."""
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            conn = await asyncssh.connect(**connect_kwargs)
            log.info("sw_install.reboot.reconnected", elapsed=elapsed)
            return conn
        except (OSError, asyncssh.DisconnectError, asyncssh.ConnectionLost):
            log.debug("sw_install.reboot.polling", elapsed=elapsed)
            continue
    return None


async def _verify_nvidia_driver_post_reboot(
    conn: asyncssh.SSHClientConnection,
) -> tuple[bool, str]:
    result = await conn.run("nvidia-smi 2>&1", check=False, timeout=30)
    ok = result.exit_status == 0
    return ok, (result.stdout or "")[:300]


# ---------------------------------------------------------------------------
# 항목 정렬 (의존성 순서)
# ---------------------------------------------------------------------------


def _sort_items(items: list[dict]) -> list[dict]:
    """_INSTALL_ORDER 기준 정렬. 알 수 없는 항목은 맨 뒤."""

    def _key(item: dict) -> int:
        name = item.get("name", "")
        try:
            return _INSTALL_ORDER.index(name)
        except ValueError:
            return len(_INSTALL_ORDER)

    sw = [i for i in items if i.get("type") == "sw_install"]
    rest = [i for i in items if i.get("type") != "sw_install"]
    return sorted(sw, key=_key) + rest


def _split_items_by_reboot(
    items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """nvidia_driver가 포함된 경우 pre/post reboot 분리."""
    has_driver = any(
        i.get("name") == "nvidia_driver" and i.get("type") == "sw_install" for i in items
    )
    if not has_driver:
        return items, []

    pre, post = [], []
    for item in items:
        name = item.get("name", "")
        if item.get("type") == "sw_install" and name in _POST_REBOOT_DEPS:
            post.append(item)
        else:
            pre.append(item)
    return pre, post


# ---------------------------------------------------------------------------
# 메인 비동기 함수
# ---------------------------------------------------------------------------


async def _async_sw_install(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    secret = wrap_password(sudo_password)
    engine, SessionLocal = _make_session()

    try:
        # SW 요구사항 로드
        async with SessionLocal() as session:
            from api.models import Job

            result = await session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
            job = result.scalar_one_or_none()
            if job is None:
                raise ValueError(f"Job {job_id} not found")
            sw_requirements = job.sw_requirements or ""

        async with SessionLocal() as session:
            await _update_job(session, job_id, status="sw_install")
        await publish_job_status(job_id, "sw_install")

        # 설치계획 생성
        from workers import sw_planner

        plan = await sw_planner.build_plan(job_id, sw_requirements)
        items = _sort_items(plan["items"])
        pre_items, post_items = _split_items_by_reboot(items)

        raw_dir = _nfs_sw_dir(job_id)
        raw_dir.mkdir(parents=True, exist_ok=True)

        install_results: list[dict] = []
        installed_set: set[str] = set()
        failed_deps: set[str] = set()
        reboot_needed = any(
            i.get("name") == "nvidia_driver" and i.get("type") == "sw_install" for i in items
        )

        connect_kwargs = _build_connect_kwargs(target_host, target_user)

        # ── 1차 SSH 세션: sys_config + pre-reboot 항목 ──────────────────────
        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                os_info = await _detect_os(conn)
                cpu_vendor = await _detect_cpu_vendor(conn)
                has_nvidia = await _detect_has_nvidia_gpu(conn)

                # 표준 sys_config 적용
                if os_info.get("id") == "ubuntu":
                    sys_results = await _apply_sys_config(conn, secret, cpu_vendor, has_nvidia)
                    install_results.extend(sys_results)
                else:
                    log.info(
                        "sw_install.sys_config.non_ubuntu",
                        os_id=os_info.get("id"),
                        job_id=job_id,
                    )
                    from workers.agent_gateway import call_sw_planner_agent

                    agent_r = await call_sw_planner_agent(
                        job_id=job_id,
                        sw_requirements=sw_requirements,
                        failed_step=f"Apply standard sys_config for OS: {os_info.get('id')} {os_info.get('version_id')}",
                    )
                    ok, detail = await _execute_agent_plan(conn, secret, agent_r.get("plan", []))
                    install_results.append(
                        {
                            "name": "sys_config_standard",
                            "type": "sys_config",
                            "status": "pass" if ok else "fail",
                            "detail": detail,
                        }
                    )

                # cuda_version 미리 추출 (torch 설치 시 필요)
                cuda_version = next(
                    (i.get("version") for i in items if i.get("name") == "cuda"),
                    None,
                )

                # pre-reboot 항목 설치
                for item in pre_items:
                    item_type = item.get("type")

                    if item_type == "sw_install":
                        if item.get("agent_required"):
                            from workers.agent_gateway import call_sw_planner_agent

                            agent_r = await call_sw_planner_agent(
                                job_id=job_id, sw_requirements=sw_requirements
                            )
                            ok, detail = await _execute_agent_plan(
                                conn, secret, agent_r.get("plan", [])
                            )
                            result_entry = _make_result(
                                item.get("name", ""), "pass" if ok else "fail", detail
                            )
                        else:
                            result_entry = await _install_sw_item(
                                conn,
                                secret,
                                item,
                                installed_set,
                                failed_deps,
                                os_info,
                                cuda_version,
                            )
                        install_results.append(result_entry)
                        if result_entry["status"] == "pass":
                            installed_set.add(item.get("name", ""))
                        else:
                            failed_deps.add(item.get("name", ""))

                    elif item_type == "account":
                        r = await _handle_account(conn, secret, item)
                        install_results.append(r)

                    elif item_type == "storage_mount":
                        r = await _handle_storage_mount(conn, secret, item)
                        install_results.append(r)

                    elif item_type == "sys_config":
                        r = await _handle_sys_config_agent(
                            conn, secret, job_id, item, sw_requirements
                        )
                        install_results.append(r)

                # reboot 발행 (nvidia_driver 설치 완료 + post_items 있을 때)
                if reboot_needed and post_items and "nvidia_driver" not in failed_deps:
                    await _do_reboot(conn, secret)

        except asyncssh.DisconnectError:
            if not (reboot_needed and post_items):
                raise

        # ── reboot 대기 ───────────────────────────────────────────────────────
        if reboot_needed and post_items and "nvidia_driver" not in failed_deps:
            async with SessionLocal() as session:
                await _update_job(session, job_id, status="rebooting")
            await publish_job_status(job_id, "rebooting")

            log.info("sw_install.reboot.waiting", job_id=job_id)
            new_conn = await _poll_ssh_reconnect(connect_kwargs, timeout=300, interval=10)
            if new_conn is None:
                await _mark_failed(
                    job_id,
                    "reboot_timeout: server did not respond within 300s",
                )
                _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
                return

            # ── 2차 SSH 세션: driver 검증 + post-reboot 항목 ─────────────────
            async with new_conn:
                driver_ok, driver_detail = await _verify_nvidia_driver_post_reboot(new_conn)
                install_results.append(
                    {
                        "name": "nvidia_driver_post_reboot_verify",
                        "type": "sw_install",
                        "status": "pass" if driver_ok else "fail",
                        "detail": driver_detail,
                    }
                )
                if not driver_ok:
                    failed_deps.add("nvidia_driver")

                for item in post_items:
                    if item.get("type") != "sw_install":
                        continue
                    if item.get("agent_required"):
                        from workers.agent_gateway import call_sw_planner_agent

                        agent_r = await call_sw_planner_agent(
                            job_id=job_id, sw_requirements=sw_requirements
                        )
                        ok, detail = await _execute_agent_plan(
                            new_conn, secret, agent_r.get("plan", [])
                        )
                        result_entry = _make_result(
                            item.get("name", ""), "pass" if ok else "fail", detail
                        )
                    else:
                        result_entry = await _install_sw_item(
                            new_conn,
                            secret,
                            item,
                            installed_set,
                            failed_deps,
                            os_info,
                            cuda_version,
                        )
                    install_results.append(result_entry)
                    if result_entry["status"] == "pass":
                        installed_set.add(item.get("name", ""))
                    else:
                        failed_deps.add(item.get("name", ""))

        # ── DB + NFS 결과 저장 ───────────────────────────────────────────────
        for r in install_results:
            db_status = r["status"]  # "pass" | "fail" | "warn"
            async with SessionLocal() as session:
                await _save_check_result(
                    session,
                    job_id,
                    r["name"],
                    db_status,
                    r["detail"],
                    r,
                )
            (raw_dir / f"{r['name']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2))

        # ── 실패 항목 → SW Planner Agent 복구 시도 ─────────────────────────
        hard_failures = [r for r in install_results if r["status"] == "fail"]
        unrecoverable = []

        if hard_failures:
            log.info(
                "sw_install.recovery.start",
                job_id=job_id,
                count=len(hard_failures),
            )
            failure_summary = "\n".join(
                f"- {r['name']}: {r['detail'][:100]}" for r in hard_failures
            )
            from workers.agent_gateway import call_sw_planner_agent

            agent_r = await call_sw_planner_agent(
                job_id=job_id,
                sw_requirements=sw_requirements,
                failed_step=failure_summary,
            )
            if agent_r.get("plan"):
                connect_kwargs2 = _build_connect_kwargs(target_host, target_user)
                async with asyncssh.connect(**connect_kwargs2) as conn:
                    ok, detail = await _execute_agent_plan(conn, secret, agent_r["plan"])
                if ok:
                    log.info("sw_install.recovery.success", job_id=job_id)
                    hard_failures = []
                else:
                    log.warning("sw_install.recovery.failed", job_id=job_id, detail=detail)
                    unrecoverable = hard_failures
            else:
                unrecoverable = hard_failures

        if unrecoverable:
            names = ", ".join(r["name"] for r in unrecoverable)
            await _mark_failed(job_id, f"sw_install failed: {names}")
            _dispatch_cleanup(job_id, target_host, target_user, product_profile, sudo_password)
            return

        # ── post_install 디스패치 ─────────────────────────────────────────
        from workers.inspect import run_post_install

        run_post_install.apply_async(
            args=[job_id, target_host, target_user, product_profile],
            kwargs={"sudo_password": sudo_password},
            queue="q_inspect",
        )
        log.info("sw_install.done", job_id=job_id)

    finally:
        await engine.dispose()


def _dispatch_cleanup(
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None,
) -> None:
    from workers.inspect import run_cleanup

    run_cleanup.apply_async(
        args=[job_id, target_host, target_user, product_profile],
        kwargs={"sudo_password": sudo_password},
        queue="q_inspect",
    )


# ---------------------------------------------------------------------------
# Celery 태스크
# ---------------------------------------------------------------------------


@app.task(
    bind=True,
    queue="q_sw_install",
    acks_late=True,
    max_retries=3,
    default_retry_delay=20,
    soft_time_limit=7200,
    time_limit=7500,
    name="workers.sw_install.run_sw_install",
)
def run_sw_install(
    self,
    job_id: str,
    target_host: str,
    target_user: str,
    product_profile: str,
    sudo_password: str | None = None,
) -> dict:
    log.info("sw_install.start", job_id=job_id, host=target_host)
    try:
        asyncio.run(
            _async_sw_install(job_id, target_host, target_user, product_profile, sudo_password)
        )
        return {"job_id": job_id, "phase": "sw_install", "result": "ok"}
    except asyncssh.DisconnectError as exc:
        asyncio.run(_mark_failed(job_id, f"SSH disconnect: {exc}"))
        raise self.retry(exc=exc)
    except asyncssh.PermissionDenied as exc:
        asyncio.run(_mark_failed(job_id, f"SSH auth failed: {exc}"))
        raise
    except Exception as exc:
        asyncio.run(_mark_failed(job_id, str(exc)))
        raise self.retry(exc=exc)
