"""
SW Planner — sw_requirements.md 파싱 + 설치계획 JSON 오케스트레이션.

흐름:
  1. parse(md_text) → 항목 분류 (sw_install / account / storage_mount / sys_config)
  2. _check_compat() → lookup table 기반 호환성 확인
  3. agent_required 항목 → call_sw_planner_agent 에스컬레이션
  4. 최종 설치계획 dict 반환

반환 구조:
    {
        "items": [
            {
                "type": "sw_install",
                "name": str,
                "version": str | None,
                "agent_required": bool,
            },
            {
                "type": "account",
                "username": str,
                "password": str,   # 마스킹 대상 — 로그 출력 금지
                "sudo": bool,
                "agent_required": False,
            },
            {
                "type": "storage_mount",
                "mount_point": str,
                "agent_required": False,
            },
            {
                "type": "sys_config",
                "raw": str,
                "agent_required": True,
            },
        ],
        "has_agent_items": bool,
        "agent_plan": dict | None,
    }
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from workers.agent_gateway import call_sw_planner_agent

log = structlog.get_logger(__name__)

_MATRIX_PATH = Path(__file__).parent.parent / "config" / "sw_compat_matrix.json"

# SW 이름 정규화 맵 (소문자 alias → 내부 canonical name)
# 긴 alias를 먼저 매칭하기 위해 정렬 시 길이 내림차순 사용
_SW_ALIASES: dict[str, str] = {
    "docker-container-toolkit": "docker_container_toolkit",
    "nvidia-container-toolkit": "docker_container_toolkit",
    "nvidia container toolkit": "docker_container_toolkit",
    "nvidia-driver": "nvidia_driver",
    "nvidia driver": "nvidia_driver",
    "cuda toolkit": "cuda",
    "pytorch": "torch",
    "miniconda": "miniconda",
    "anaconda": "miniconda",
    "tt-burnin": "tt_burnin",
    "tt-smi": "tt_smi",
    "tt-kmd": "tt_kmd",
    "rustup": "rustup",
    "docker": "docker",
    "cudnn": "cudnn",
    "torch": "torch",
    "python": "python",
    "cuda": "cuda",
    "gcc": "gcc",
}

# 분류 키워드
_SYS_CONFIG_KEYWORDS = [
    "grub",
    "crontab",
    "hibernate",
    "커널 파라미터",
    "kernel parameter",
    "grub_cmdline",
]
_STORAGE_KEYWORDS = ["마운트", "mount"]
_ACCOUNT_KEYWORDS = ["계정:", "계정 :"]


# ---------------------------------------------------------------------------
# 내부 — 파싱 헬퍼
# ---------------------------------------------------------------------------


def _load_compat_matrix() -> dict:
    try:
        return json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("sw_planner.compat_matrix_not_found", path=str(_MATRIX_PATH))
        return {}


def _parse_bullet_lines(md_text: str) -> list[str]:
    """마크다운 불릿 라인(- 또는 *)만 추출."""
    lines = []
    for line in md_text.splitlines():
        stripped = line.strip()
        if len(stripped) > 2 and stripped[0] in ("-", "*") and stripped[1] in (" ", "\t"):
            lines.append(stripped[2:].strip())
    return lines


def _classify_line(line: str) -> str:
    """라인 분류: account | storage_mount | sys_config | sw_install."""
    lower = line.lower()
    if any(kw in lower for kw in _ACCOUNT_KEYWORDS):
        return "account"
    if any(kw in lower for kw in _STORAGE_KEYWORDS):
        return "storage_mount"
    if any(kw in lower for kw in _SYS_CONFIG_KEYWORDS):
        return "sys_config"
    return "sw_install"


def _parse_account(line: str) -> dict:
    """'계정: user1 / pw: xxxx / sudo: yes' 파싱."""
    username = ""
    password = ""
    sudo = False

    m = re.search(r"계정\s*:\s*(\S+)", line)
    if m:
        username = m.group(1).rstrip("/").strip()

    m = re.search(r"(?:pw|password)\s*:\s*(\S+)", line, re.IGNORECASE)
    if m:
        password = m.group(1).rstrip("/").strip()

    m = re.search(r"sudo\s*:\s*(yes|no|true|false)", line, re.IGNORECASE)
    if m:
        sudo = m.group(1).lower() in ("yes", "true")

    return {
        "type": "account",
        "username": username,
        "password": password,
        "sudo": sudo,
        "agent_required": False,
    }


def _parse_storage_mount(line: str) -> dict:
    """'보조스토리지 /mnt/data 마운트' 파싱."""
    m = re.search(r"(/[\w/.-]+)", line)
    mount_point = m.group(1) if m else ""
    return {
        "type": "storage_mount",
        "mount_point": mount_point,
        "agent_required": False,
    }


def _parse_sw_install(line: str) -> dict:
    """'CUDA 12.4', 'nvidia-driver 550', 'docker' 등 파싱."""
    lower = line.lower().strip()

    # 긴 alias 우선 매칭 (longest-match)
    matched_name: str | None = None
    matched_len = 0
    for alias, canonical in _SW_ALIASES.items():
        if lower.startswith(alias) and len(alias) > matched_len:
            matched_name = canonical
            matched_len = len(alias)

    if matched_name is None:
        parts = line.split()
        matched_name = parts[0].lower().replace("-", "_") if parts else line.lower()

    # 버전 추출: 숫자 시작 토큰 (예: 12.4, 2.3, 570)
    version: str | None = None
    for token in line.split():
        if re.match(r"^\d[\d.]*$", token):
            version = token
            break

    return {
        "type": "sw_install",
        "name": matched_name,
        "version": version,
        "agent_required": False,
    }


# ---------------------------------------------------------------------------
# 내부 — 호환성 확인
# ---------------------------------------------------------------------------


def _check_compat(items: list[dict], matrix: dict) -> list[dict]:
    """
    driver+cuda, cuda+torch 조합 호환성 확인.
    불호환 또는 matrix 미등록 조합 → agent_required=True.
    """
    if not matrix:
        return items

    sw_map: dict[str, str | None] = {
        item["name"]: item.get("version") for item in items if item["type"] == "sw_install"
    }

    driver_ver = sw_map.get("nvidia_driver")
    cuda_ver = sw_map.get("cuda")
    torch_ver = sw_map.get("torch")

    driver_matrix = matrix.get("nvidia_driver", {})
    cuda_matrix = matrix.get("cuda", {})

    agent_names: set[str] = set()

    if driver_ver and cuda_ver:
        driver_entry = driver_matrix.get(driver_ver)
        if driver_entry is None or cuda_ver not in driver_entry.get("cuda", []):
            log.warning(
                "sw_planner.compat_miss.driver_cuda",
                driver=driver_ver,
                cuda=cuda_ver,
            )
            agent_names.update({"nvidia_driver", "cuda"})

    if cuda_ver and torch_ver:
        cuda_entry = cuda_matrix.get(cuda_ver)
        if cuda_entry is None or torch_ver not in cuda_entry.get("torch", []):
            log.warning(
                "sw_planner.compat_miss.cuda_torch",
                cuda=cuda_ver,
                torch=torch_ver,
            )
            agent_names.update({"cuda", "torch"})

    if not agent_names:
        return items

    return [
        {**item, "agent_required": True}
        if item["type"] == "sw_install" and item["name"] in agent_names
        else item
        for item in items
    ]


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def parse(md_text: str) -> list[dict]:
    """
    sw_requirements.md 파싱 → 항목 리스트 반환.

    Args:
        md_text: sw_requirements.md 원문

    Returns:
        항목 리스트 (agent_required는 호환성 확인 전 초기값)
    """
    lines = _parse_bullet_lines(md_text)
    items = []
    for line in lines:
        if not line:
            continue
        kind = _classify_line(line)
        if kind == "account":
            items.append(_parse_account(line))
        elif kind == "storage_mount":
            items.append(_parse_storage_mount(line))
        elif kind == "sys_config":
            items.append({"type": "sys_config", "raw": line, "agent_required": True})
        else:
            items.append(_parse_sw_install(line))
    return items


async def build_plan(job_id: str, sw_requirements: str) -> dict:
    """
    SW 설치계획 생성.

    Args:
        job_id: Job UUID str
        sw_requirements: sw_requirements.md 원문

    Returns:
        {"items": list[dict], "has_agent_items": bool, "agent_plan": dict | None}
    """
    matrix = _load_compat_matrix()
    items = parse(sw_requirements)
    items = _check_compat(items, matrix)

    agent_required_items = [item for item in items if item["agent_required"]]
    has_agent_items = bool(agent_required_items)
    agent_plan: dict | None = None

    if has_agent_items:
        log.info(
            "sw_planner.agent_required",
            job_id=job_id,
            count=len(agent_required_items),
        )
        try:
            agent_plan = await call_sw_planner_agent(
                job_id=job_id,
                sw_requirements=sw_requirements,
            )
        except Exception as exc:
            log.error("sw_planner.agent_call_failed", job_id=job_id, error=str(exc))
            agent_plan = {"error": str(exc)}

    log.info(
        "sw_planner.plan_built",
        job_id=job_id,
        total=len(items),
        has_agent=has_agent_items,
    )

    return {
        "items": items,
        "has_agent_items": has_agent_items,
        "agent_plan": agent_plan,
    }
