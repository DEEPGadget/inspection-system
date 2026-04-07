"""
Rule Validator 유닛 테스트.
DB·API 호출 없음 — 순수 함수 판정 로직만 검증.
"""

from unittest.mock import MagicMock

from workers.rule_validator import _parse_detail, evaluate


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _cr(check_name: str, detail: str) -> MagicMock:
    return MagicMock(check_name=check_name, detail=detail)


# ---------------------------------------------------------------------------
# _parse_detail
# ---------------------------------------------------------------------------


def test_parse_detail_basic():
    result = _parse_detail("gpu_count=8|gpu_max_temp_c=45")
    assert result == {"gpu_count": "8", "gpu_max_temp_c": "45"}


def test_parse_detail_single():
    assert _parse_detail("status=pass") == {"status": "pass"}


def test_parse_detail_empty():
    assert _parse_detail("") == {}


def test_parse_detail_no_equals():
    assert _parse_detail("noequals") == {}


# ---------------------------------------------------------------------------
# evaluate — 모두 통과
# ---------------------------------------------------------------------------

ALL_PASS_RULES = [
    {"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87, "agent_zone_above": 75},
    {"check": "sw_power_mgmt", "metric": "sleep_target", "fail_if_not": "masked"},
]

ALL_PASS_RESULTS = [
    _cr("sw_gpu_sw", "gpu_max_temp_c=45"),
    _cr("sw_power_mgmt", "sleep_target=masked"),
]


def test_all_pass():
    result = evaluate(ALL_PASS_RULES, ALL_PASS_RESULTS, expected_specs=None)
    assert result["verdict"] == "pass"
    assert result["fail_items"] == []
    assert result["warn_items"] == []
    assert result["warn_count"] == 0


# ---------------------------------------------------------------------------
# evaluate — fail_above / fail_below
# ---------------------------------------------------------------------------


def test_fail_above():
    rules = [{"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87}]
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=92")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "fail"
    assert len(out["fail_items"]) == 1
    assert out["fail_items"][0]["rule"] == "fail_above"
    assert out["fail_items"][0]["value"] == "92"
    assert out["fail_items"][0]["threshold"] == 87


def test_fail_above_boundary_not_triggered():
    """threshold와 정확히 같으면 FAIL 아님."""
    rules = [{"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87}]
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=87")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


def test_fail_below():
    rules = [{"check": "nccl_bandwidth", "metric": "bw_2gpu_gbs", "fail_below": 30}]
    results = [_cr("nccl_bandwidth", "bw_2gpu_gbs=20")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "fail"
    assert out["fail_items"][0]["rule"] == "fail_below"


def test_fail_below_boundary_not_triggered():
    rules = [{"check": "nccl_bandwidth", "metric": "bw_2gpu_gbs", "fail_below": 30}]
    results = [_cr("nccl_bandwidth", "bw_2gpu_gbs=30")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


# ---------------------------------------------------------------------------
# evaluate — fail_if / fail_if_not
# ---------------------------------------------------------------------------


def test_fail_if():
    rules = [
        {"check": "sw_auto_update", "metric": "unattended_upgrades_active", "fail_if": "active"}
    ]
    results = [_cr("sw_auto_update", "unattended_upgrades_active=active")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "fail"
    assert out["fail_items"][0]["rule"] == "fail_if"


def test_fail_if_not_triggered_when_different():
    """fail_if: 값이 다르면 FAIL 아님."""
    rules = [
        {"check": "sw_auto_update", "metric": "unattended_upgrades_active", "fail_if": "active"}
    ]
    results = [_cr("sw_auto_update", "unattended_upgrades_active=inactive")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


def test_fail_if_not():
    rules = [{"check": "sw_power_mgmt", "metric": "sleep_target", "fail_if_not": "masked"}]
    results = [_cr("sw_power_mgmt", "sleep_target=enabled")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "fail"
    assert out["fail_items"][0]["rule"] == "fail_if_not"


def test_fail_if_not_pass_when_matches():
    rules = [{"check": "sw_power_mgmt", "metric": "sleep_target", "fail_if_not": "masked"}]
    results = [_cr("sw_power_mgmt", "sleep_target=masked")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


# ---------------------------------------------------------------------------
# evaluate — fail_if_not_equal
# ---------------------------------------------------------------------------


def test_fail_if_not_equal_numeric():
    rules = [
        {"check": "sw_gpu_hw", "metric": "gpu_count", "fail_if_not_equal": "expected_gpu_count"}
    ]
    results = [_cr("sw_gpu_hw", "gpu_count=4")]
    out = evaluate(rules, results, expected_specs={"expected_gpu_count": 8})
    assert out["verdict"] == "fail"
    assert out["fail_items"][0]["rule"] == "fail_if_not_equal"


def test_fail_if_not_equal_pass_when_matches():
    rules = [
        {"check": "sw_gpu_hw", "metric": "gpu_count", "fail_if_not_equal": "expected_gpu_count"}
    ]
    results = [_cr("sw_gpu_hw", "gpu_count=8")]
    out = evaluate(rules, results, expected_specs={"expected_gpu_count": 8})
    assert out["verdict"] == "pass"


def test_fail_if_not_equal_missing_expected_spec_skips():
    """expected_specs에 해당 키 없으면 규칙 건너뜀 (FAIL 아님)."""
    rules = [
        {"check": "sw_gpu_hw", "metric": "gpu_count", "fail_if_not_equal": "expected_gpu_count"}
    ]
    results = [_cr("sw_gpu_hw", "gpu_count=8")]
    out = evaluate(rules, results, expected_specs={})
    assert out["verdict"] == "pass"


def test_fail_if_not_equal_none_expected_specs_skips():
    rules = [
        {"check": "sw_gpu_hw", "metric": "gpu_count", "fail_if_not_equal": "expected_gpu_count"}
    ]
    results = [_cr("sw_gpu_hw", "gpu_count=8")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


# ---------------------------------------------------------------------------
# evaluate — agent_zone
# ---------------------------------------------------------------------------


def test_agent_zone_above_single_warn_below_threshold():
    """warn 1개, threshold 3 → verdict = pass (warn_items에 기록은 됨)."""
    rules = [
        {"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87, "agent_zone_above": 75}
    ]
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=80")]
    out = evaluate(rules, results, expected_specs=None, warn_count_threshold=3)
    assert out["verdict"] == "pass"
    assert len(out["warn_items"]) == 1
    assert out["warn_items"][0]["rule"] == "agent_zone_above"


def test_agent_zone_below_single_warn():
    # fail_below: 20 → FAIL if < 20
    # agent_zone_below: 30 → warn if < 30 (and not already failed)
    # value=25: 25 >= 20 (no fail), 25 < 30 (agent_zone) → warn
    rules = [
        {
            "check": "nccl_bandwidth",
            "metric": "bw_2gpu_gbs",
            "fail_below": 20,
            "agent_zone_below": 30,
        }
    ]
    results = [_cr("nccl_bandwidth", "bw_2gpu_gbs=25")]
    out = evaluate(rules, results, expected_specs=None, warn_count_threshold=3)
    assert out["verdict"] == "pass"
    assert out["warn_items"][0]["rule"] == "agent_zone_below"


def test_warn_count_reaches_threshold_triggers_agent_required():
    # 3개 모두 agent_zone 해당 → warn_count=3 >= threshold=3 → agent_required
    rules = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "fail_above": 87,
            "agent_zone_above": 75,
        },
        {"check": "sw_cpu", "metric": "cpu_max_temp_c", "fail_above": 100, "agent_zone_above": 85},
        {
            "check": "nccl_bandwidth",
            "metric": "bw_2gpu_gbs",
            "fail_below": 20,
            "agent_zone_below": 30,
        },
    ]
    results = [
        _cr("sw_gpu_sw", "gpu_max_temp_c=80"),  # 80 > 75, not > 87 → agent_zone
        _cr("sw_cpu", "cpu_max_temp_c=90"),  # 90 > 85, not > 100 → agent_zone
        _cr("nccl_bandwidth", "bw_2gpu_gbs=25"),  # 25 >= 20, 25 < 30 → agent_zone
    ]
    out = evaluate(rules, results, expected_specs=None, warn_count_threshold=3)
    assert out["verdict"] == "agent_required"
    assert out["warn_count"] == 3


def test_warn_count_below_threshold_is_pass():
    rules = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "fail_above": 87,
            "agent_zone_above": 75,
        },
        {"check": "sw_cpu", "metric": "cpu_max_temp_c", "fail_above": 100, "agent_zone_above": 85},
    ]
    results = [
        _cr("sw_gpu_sw", "gpu_max_temp_c=80"),
        _cr("sw_cpu", "cpu_max_temp_c=90"),
    ]
    out = evaluate(rules, results, expected_specs=None, warn_count_threshold=3)
    assert out["verdict"] == "pass"
    assert out["warn_count"] == 2


# ---------------------------------------------------------------------------
# evaluate — FAIL + agent_zone 동시 (Decision 2: FAIL 우선)
# ---------------------------------------------------------------------------


def test_fail_takes_priority_over_agent_zone():
    """GPU 온도 FAIL + NCCL agent_zone 동시 → verdict = fail, agent 미호출."""
    rules = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "fail_above": 87,
            "agent_zone_above": 75,
        },
        # fail_below: 20, agent_zone_below: 30 → value=25: no fail, agent_zone
        {
            "check": "nccl_bandwidth",
            "metric": "bw_2gpu_gbs",
            "fail_below": 20,
            "agent_zone_below": 30,
        },
    ]
    results = [
        _cr("sw_gpu_sw", "gpu_max_temp_c=92"),  # 92 > 87 → FAIL
        _cr("nccl_bandwidth", "bw_2gpu_gbs=25"),  # 25 >= 20, 25 < 30 → agent_zone
    ]
    out = evaluate(rules, results, expected_specs=None, warn_count_threshold=3)
    assert out["verdict"] == "fail"
    assert len(out["fail_items"]) == 1
    assert len(out["warn_items"]) == 1  # warn_items는 여전히 기록됨


def test_fail_metric_does_not_add_to_warn_items():
    """FAIL된 metric의 agent_zone은 warn_items에 포함되지 않음."""
    rules = [
        {
            "check": "sw_gpu_sw",
            "metric": "gpu_max_temp_c",
            "fail_above": 87,
            "agent_zone_above": 75,
        },
    ]
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=92")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "fail"
    assert out["warn_items"] == []  # 같은 metric은 agent_zone 미평가


# ---------------------------------------------------------------------------
# evaluate — 누락 check / metric 처리
# ---------------------------------------------------------------------------


def test_missing_check_result_skips_rule():
    rules = [{"check": "sw_missing", "metric": "some_metric", "fail_above": 0}]
    results = []
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"
    assert out["fail_items"] == []


def test_missing_metric_in_detail_skips_rule():
    rules = [{"check": "sw_gpu_sw", "metric": "nonexistent_metric", "fail_above": 0}]
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=45")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


def test_invalid_rule_missing_check_skips():
    rules = [{"metric": "gpu_max_temp_c", "fail_above": 87}]  # check 키 없음
    results = [_cr("sw_gpu_sw", "gpu_max_temp_c=92")]
    out = evaluate(rules, results, expected_specs=None)
    assert out["verdict"] == "pass"


# ---------------------------------------------------------------------------
# evaluate — 복합 시나리오 (gpu_server.json 프로파일 기반)
# ---------------------------------------------------------------------------


GPU_SERVER_RULES = [
    {"check": "sw_gpu_hw", "metric": "gpu_count", "fail_if_not_equal": "expected_gpu_count"},
    {"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87, "agent_zone_above": 75},
    {"check": "sw_cpu", "metric": "cpu_max_temp_c", "fail_above": 100, "agent_zone_above": 85},
    {"check": "sw_gpu_sw", "metric": "ecc_delta_uncorr", "fail_above": 0},
    {"check": "nccl_bandwidth", "metric": "bw_2gpu_gbs", "fail_below": 30, "agent_zone_below": 25},
    {"check": "nccl_bandwidth", "metric": "bw_4gpu_gbs", "fail_below": 5, "agent_zone_below": 3},
    {"check": "sw_power_mgmt", "metric": "sleep_target", "fail_if_not": "masked"},
    {"check": "sw_auto_update", "metric": "unattended_upgrades_active", "fail_if": "active"},
]


def test_gpu_server_all_pass():
    results = [
        _cr("sw_gpu_hw", "gpu_count=8"),
        _cr("sw_gpu_sw", "gpu_max_temp_c=45|ecc_delta_uncorr=0"),
        _cr("sw_cpu", "cpu_max_temp_c=60"),
        _cr("nccl_bandwidth", "bw_2gpu_gbs=150|bw_4gpu_gbs=80"),
        _cr("sw_power_mgmt", "sleep_target=masked"),
        _cr("sw_auto_update", "unattended_upgrades_active=inactive"),
    ]
    out = evaluate(GPU_SERVER_RULES, results, expected_specs={"expected_gpu_count": 8})
    assert out["verdict"] == "pass"


def test_gpu_server_gpu_count_mismatch():
    results = [
        _cr("sw_gpu_hw", "gpu_count=4"),  # 기대값 8, 실제 4 → FAIL
        _cr("sw_gpu_sw", "gpu_max_temp_c=45|ecc_delta_uncorr=0"),
        _cr("sw_cpu", "cpu_max_temp_c=60"),
        _cr("nccl_bandwidth", "bw_2gpu_gbs=150|bw_4gpu_gbs=80"),
        _cr("sw_power_mgmt", "sleep_target=masked"),
        _cr("sw_auto_update", "unattended_upgrades_active=inactive"),
    ]
    out = evaluate(GPU_SERVER_RULES, results, expected_specs={"expected_gpu_count": 8})
    assert out["verdict"] == "fail"
    assert any(i["metric"] == "gpu_count" for i in out["fail_items"])
