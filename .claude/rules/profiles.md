# 프로파일 구조 규칙

## 파일 위치

`checks/profiles/{name}.json`

## 전체 구조 (gpu_server.json 기준)

```json
{
  "profile_name": "gpu_server",
  "pre_install": {
    "baseline": ["pciutils", "nvme-cli", "ipmitool", "lm-sensors", "smartctl"],
    "stress_tools": ["stress-ng"]
  },
  "phases": {
    "preflight": {
      "scripts": ["sw_gpu_hw", "sw_cpu", "sw_memory", "sw_storage_hw",
                  "sw_network", "sw_os_version", "sw_power_mgmt", "sw_auto_update"]
    },
    "post_install": {
      "scripts": ["sw_gpu_sw", "sw_storage_sw", "stress_gpu", "stress_cpu", "nccl_bandwidth"],
      "timeout": 7200,
      "env": {
        "GPU_BURNIN_DURATION": "300",
        "CPU_BURNIN_DURATION": "120"
      }
    },
    "collect": {
      "scripts": ["collect_all_logs"]
    }
  },
  "validation": {
    "rules": [
      {"check": "sw_gpu_hw",       "metric": "gpu_count",       "fail_if_not_equal": "expected_gpu_count"},
      {"check": "sw_gpu_sw",       "metric": "gpu_max_temp_c",  "fail_above": 87,  "agent_zone_above": 75},
      {"check": "sw_cpu",          "metric": "cpu_max_temp_c",  "fail_above": 100, "agent_zone_above": 85},
      {"check": "sw_gpu_sw",       "metric": "ecc_delta_uncorr","fail_above": 0},
      {"check": "nccl_bandwidth",  "metric": "bw_2gpu_gbs",     "fail_below": 30,  "agent_zone_below": 25},
      {"check": "nccl_bandwidth",  "metric": "bw_4gpu_gbs",     "fail_below": 5,   "agent_zone_below": 3},
      {"check": "sw_power_mgmt",   "metric": "sleep_target",    "fail_if_not": "masked"},
      {"check": "sw_auto_update",  "metric": "unattended_upgrades_active", "fail_if": "active"}
    ],
    "agent_trigger": {
      "warn_count_threshold": 3
    }
  },
  "cleanup": {
    "remove_packages": ["stress-ng"],
    "remove_dirs": ["/opt/gpu-burn", "/opt/nccl-tests"],
    "on_failure": "warn"
  }
}
```

## validation.rules 키 의미

| 키 | 의미 |
|----|------|
| `fail_above` | metric > 값이면 FAIL |
| `fail_below` | metric < 값이면 FAIL |
| `fail_if` | metric == 값이면 FAIL |
| `fail_if_not` | metric != 값이면 FAIL |
| `fail_if_not_equal` | metric != 다른 필드값이면 FAIL |
| `agent_zone_above` | 이 값 초과 시 Verify Agent 호출 구간 시작 |
| `agent_zone_below` | 이 값 미만 시 Verify Agent 호출 구간 시작 |

## pre_install 타이밍

- `baseline` → Preflight 직전 설치
- `stress_tools` → Post-install 직전 설치

## cleanup 정책

- `remove_packages`: apt 제거 대상 목록
- `remove_dirs`: rm -rf 대상 목록 (명시된 경로만)
- `on_failure: "warn"`: cleanup 실패해도 job은 진행

## 확장 시 주의

- 새 제품군 추가: `checks/profiles/{name}.json` 생성 후 `POST /api/jobs/`의 `product_profile` 필드에 지정
- `agent_zone` 구간 확장 가능 (추후 항목 세분화 예정)
