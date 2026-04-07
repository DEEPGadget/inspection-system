"""
Rule Validator — threshold 기반 PASS/FAIL/agent_required 판정.
DB·API 호출 없음, 토큰 0.

validate.py가 호출하고 결과에 따라 job 상태를 전이시킨다.

반환 구조:
    {
        "verdict": "pass" | "fail" | "agent_required",
        "fail_items": [{"check", "metric", "value", "rule", "threshold"}, ...],
        "warn_items": [{"check", "metric", "value", "rule", "threshold"}, ...],
        "warn_count": int,
    }
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# detail 파싱
# ---------------------------------------------------------------------------


def _parse_detail(detail: str) -> dict[str, str]:
    """'key=val|key2=val2' 형식의 detail 문자열을 dict로 파싱."""
    result: dict[str, str] = {}
    for pair in detail.split("|"):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            result[k.strip()] = v.strip()
    return result


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 단일 규칙 판정
# ---------------------------------------------------------------------------


def _check_fail(
    rule: dict,
    metric_val: str,
    expected_specs: dict,
) -> tuple[bool, str]:
    """FAIL 여부 판정. (is_fail, rule_key) 반환."""
    fv = _to_float(metric_val)

    if "fail_above" in rule:
        if fv is not None and fv > float(rule["fail_above"]):
            return True, "fail_above"

    if "fail_below" in rule:
        if fv is not None and fv < float(rule["fail_below"]):
            return True, "fail_below"

    if "fail_if" in rule:
        if metric_val == str(rule["fail_if"]):
            return True, "fail_if"

    if "fail_if_not" in rule:
        if metric_val != str(rule["fail_if_not"]):
            return True, "fail_if_not"

    if "fail_if_not_equal" in rule:
        field = rule["fail_if_not_equal"]
        expected = expected_specs.get(field)
        if expected is None:
            log.warning("rule_validator.missing_expected_spec", field=field)
            return False, ""
        ef = _to_float(str(expected))
        # 둘 다 숫자로 변환 가능하면 float 비교, 아니면 str 비교
        if fv is not None and ef is not None:
            if fv != ef:
                return True, "fail_if_not_equal"
        elif metric_val != str(expected):
            return True, "fail_if_not_equal"

    return False, ""


def _check_agent_zone(rule: dict, metric_val: str) -> tuple[bool, str]:
    """agent_zone 해당 여부 판정. (is_warn, rule_key) 반환."""
    fv = _to_float(metric_val)

    if "agent_zone_above" in rule:
        if fv is not None and fv > float(rule["agent_zone_above"]):
            return True, "agent_zone_above"

    if "agent_zone_below" in rule:
        if fv is not None and fv < float(rule["agent_zone_below"]):
            return True, "agent_zone_below"

    return False, ""


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def evaluate(
    rules: list[dict],
    check_results: list,
    expected_specs: dict | None,
    warn_count_threshold: int = 3,
) -> dict:
    """
    프로파일 validation.rules를 기반으로 전체 판정.

    Args:
        rules: 프로파일 validation.rules 리스트
        check_results: CheckResult ORM 객체 리스트
        expected_specs: jobs.expected_specs dict (호출자가 전달, fail_if_not_equal용)
        warn_count_threshold: 이 값 이상이면 verdict = "agent_required"

    Returns:
        verdict dict (상단 모듈 docstring 참조)
    """
    specs = expected_specs or {}

    # check_name → detail 매핑
    detail_map: dict[str, str] = {cr.check_name: cr.detail or "" for cr in check_results}

    fail_items: list[dict] = []
    warn_items: list[dict] = []

    for rule in rules:
        check_name = rule.get("check")
        metric = rule.get("metric")
        if not check_name or not metric:
            log.warning("rule_validator.invalid_rule", rule=rule)
            continue

        if check_name not in detail_map:
            log.warning("rule_validator.missing_check", check=check_name)
            continue

        parsed = _parse_detail(detail_map[check_name])
        metric_val = parsed.get(metric)
        if metric_val is None:
            log.warning("rule_validator.missing_metric", check=check_name, metric=metric)
            continue

        # FAIL 판정 — FAIL이면 agent_zone 확인 불필요 (Decision 2: FAIL 우선 확정)
        is_fail, fail_rule = _check_fail(rule, metric_val, specs)
        if is_fail:
            fail_items.append(
                {
                    "check": check_name,
                    "metric": metric,
                    "value": metric_val,
                    "rule": fail_rule,
                    "threshold": rule.get(fail_rule),
                }
            )
            continue

        # agent_zone 판정 (FAIL 아닌 경우에만)
        is_warn, warn_rule = _check_agent_zone(rule, metric_val)
        if is_warn:
            warn_items.append(
                {
                    "check": check_name,
                    "metric": metric,
                    "value": metric_val,
                    "rule": warn_rule,
                    "threshold": rule.get(warn_rule),
                }
            )

    warn_count = len(warn_items)

    if fail_items:
        verdict = "fail"
    elif warn_count >= warn_count_threshold:
        verdict = "agent_required"
    else:
        verdict = "pass"

    log.info(
        "rule_validator.result",
        verdict=verdict,
        fail_count=len(fail_items),
        warn_count=warn_count,
    )

    return {
        "verdict": verdict,
        "fail_items": fail_items,
        "warn_items": warn_items,
        "warn_count": warn_count,
    }
