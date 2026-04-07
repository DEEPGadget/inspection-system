"""
structlog 설정 및 민감필드 마스킹.

configure_logging()을 앱 시작 시 1회 호출.
API(api/main.py)와 Worker(workers/app.py) 모두에서 호출.
"""

import logging
import re

import structlog

# event_dict 키 중 값을 완전히 "***"로 대체할 목록 (대소문자 무관 비교)
_MASKED_KEYS = frozenset(
    {
        "password",
        "sudo_password",
        "api_key",
        "anthropic_api_key",
        "token",
        "secret",
    }
)

# 문자열 내 KEY=value 패턴 — traceback 환경변수 등 포함 처리
# 대상: PASSWORD, SECRET, TOKEN, KEY 를 포함하는 식별자 다음에 = value 가 있는 경우
_VALUE_PATTERN = re.compile(
    r"(\w*(?:password|secret|token|key)\w*\s*=\s*)\S+",
    re.IGNORECASE,
)


def _mask_value(s: str) -> str:
    """문자열 내 KEY=value 패턴을 KEY=*** 로 치환."""
    return _VALUE_PATTERN.sub(r"\1***", s)


def _mask_dict(d: dict) -> dict:
    """event_dict 를 재귀적으로 순회하며 민감 필드 마스킹."""
    for key in list(d.keys()):
        if key.lower() in _MASKED_KEYS:
            d[key] = "***"
        elif isinstance(d[key], dict):
            _mask_dict(d[key])
        elif isinstance(d[key], str):
            d[key] = _mask_value(d[key])
        elif isinstance(d[key], list):
            d[key] = [
                _mask_dict(item)
                if isinstance(item, dict)
                else (_mask_value(item) if isinstance(item, str) else item)
                for item in d[key]
            ]
    return d


def mask_sensitive_fields(
    logger: object,
    method_name: str,
    event_dict: dict,
) -> dict:
    """structlog 프로세서: event_dict 에서 민감 필드 마스킹."""
    return _mask_dict(event_dict)


def configure_logging() -> None:
    """structlog 전역 설정. 앱 시작 시 1회 호출."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            mask_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
