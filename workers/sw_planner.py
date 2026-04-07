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
    "cuda-toolkit": "cuda",
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
    """
    요구사항 라인 추출.
    - 마크다운 불릿(- / *) 지원
    - 불릿 없는 일반 라인도 허용 (예: 'torch==2.4.0', 'nvidia-driver-560')
    - 마크다운 헤더(#), 코드블록(```), 구분자(---, ===) 제외
    """
    lines = []
    for line in md_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "```", "---", "===")):
            continue
        if len(stripped) > 1 and stripped[0] in ("-", "*") and stripped[1] in (" ", "\t"):
            content = stripped[2:].strip()
            if content:
                lines.append(content)
        else:
            lines.append(stripped)
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


def _is_alias_boundary(s: str, pos: int) -> bool:
    """alias 끝 위치(pos)가 경계인지 확인: 문자열 끝·공백·구분자·버전 하이픈."""
    if pos >= len(s):
        return True
    ch = s[pos]
    if ch in (" ", "\t", "=", ">", "<", "~", "!"):
        return True
    # 하이픈 뒤에 숫자가 오면 버전 접미어 (예: -560, -12-6)
    if ch == "-" and pos + 1 < len(s) and s[pos + 1].isdigit():
        return True
    return False


def _extract_version(line: str) -> str | None:
    """
    버전 추출. 우선순위:
    1. pip/conda 스타일 — torch==2.4.0, cuda>=12.4
    2. 공백 구분 순수 숫자 토큰 — 'CUDA 12.4', 'nvidia-driver 550'
    3. 하이픈 구분 후미 버전 — cuda-toolkit-12-6 → '12.6', nvidia-driver-560 → '560'
    """
    # 1. pip/conda 스타일 (==, >=, <=, ~=, !=, =)
    m = re.search(r"[=!<>~]=?\s*(\d[\d.]*)", line)
    if m:
        return m.group(1)

    # 2. 공백으로 구분된 순수 숫자 토큰
    for token in line.split():
        if re.match(r"^\d[\d.]*$", token):
            return token

    # 3. 하이픈 구분 후미 버전 (패키지명-버전 형식)
    m = re.search(r"-(\d[\d-]*)$", line.lower())
    if m:
        return m.group(1).replace("-", ".")

    return None


def _parse_sw_install(line: str) -> dict:
    """
    'CUDA 12.4', 'nvidia-driver 550', 'torch==2.4.0', 'cuda-toolkit-12-6' 등 파싱.

    P2: alias 매칭 시 경계 문자 확인 — 'docker'가 'docker-compose'로 오매핑되는 문제 방지.
    """
    lower = line.lower().strip()

    # 긴 alias 우선 매칭 (longest-match + 경계 확인)
    matched_name: str | None = None
    matched_len = 0
    for alias, canonical in _SW_ALIASES.items():
        if (
            lower.startswith(alias)
            and len(alias) > matched_len
            and _is_alias_boundary(lower, len(alias))
        ):
            matched_name = canonical
            matched_len = len(alias)

    if matched_name is None:
        # 알 수 없는 패키지: 첫 토큰에서 버전 구분자 앞까지를 이름으로 사용
        first = re.split(r"[\s=><~!]", line)[0]
        matched_name = first.lower().replace("-", "_") if first else line.lower()

    return {
        "type": "sw_install",
        "name": matched_name,
        "version": _extract_version(line),
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
