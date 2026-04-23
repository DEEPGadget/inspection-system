# 검수 스크립트 규칙

## 출력 규격

stdout에 JSON 한 줄만 출력. 디버그는 stderr.

```json
{"check": "sw_gpu_hw", "status": "pass|fail|warn", "detail": "key=val|key2=val2"}
```

## 작성 규칙

- shebang: `#!/usr/bin/env python3`
- stdlib만 사용 (원격 서버에 pip 설치 금지)
- 외부 명령: `subprocess.run(shell=True, capture_output=True, text=True, timeout=N)`
  - `shell=True` 이유: 파이프/리다이렉트 필요한 시스템 명령 조합, 대상 서버에서 1회성 실행 후 폐기
- 파라미터: `os.environ.get("VAR", "default")`로 수신 (sshd AcceptEnv 우회)
- apt/sudo 필요 패키지는 스크립트 내부가 아닌 프로파일 `pre_install.baseline` 또는 `stress_tools`에 등록
- gadget-burn / nccl-tests 같은 git 빌드 도구는 `$HOME/` 하위에 빌드 (sudo 불필요). cleanup `remove_dirs`에 `$HOME/...` 경로 등록

## 새 스크립트 추가 시 체크리스트

1. JSON 출력 검증: `python3 checks/base/{phase}/{name}.py | python3 -m json.tool`
2. 문법 확인
3. `checks/profiles/` 해당 프로파일에 등록

## Phase별 스크립트 목록

| Phase | 스크립트 | 설명 |
|-------|---------|------|
| preflight | `sw_gpu_hw.py` | lspci — GPU 존재/수량/PCIe width·speed (driver 불필요) |
| preflight | `sw_cpu.py` | /proc/cpuinfo, 온도 |
| preflight | `sw_memory.py` | /proc/meminfo, DIMM, NUMA, ECC |
| preflight | `sw_storage_hw.py` | lsblk — 디스크 목록·용량·RAID (nvme-cli 불필요) |
| preflight | `sw_network.py` | NIC 링크·속도·MTU |
| preflight | `sw_os_version.py` | OS·커널·필수 패키지 |
| preflight | `sw_power_mgmt.py` | sleep.target·CPU governor·C-state |
| preflight | `sw_auto_update.py` | unattended-upgrades 비활성화 확인 |
| post_install | `sw_gpu_sw.py` | nvidia-smi — driver·VRAM·온도·ECC·NVLink |
| post_install | `sw_storage_sw.py` | nvme-cli/smartctl — NVMe 헬스·SMART. nvme 장치 존재 + nvme-cli 미설치 시 즉시 fail (사유 포함) |
| post_install | `stress_gpu.py` | gadget-burn 전용. `~/gadget-burn` 빌드(`git clone` + `make`, sudo 불필요), `gadget_burn -t <duration>` 실행 (gaming/datacenter 공통). 단계별 실패 사유(stderr 마지막 5줄) 명시 |
| post_install | `stress_cpu.py` | CPU 부하 테스트 (기본 120s) |
| post_install | `nccl_bandwidth.py` | nccl-tests 전용. `~/nccl-tests` 빌드, `all_reduce_perf` 실행. 빌드/측정 실패 시 사유 포함 fail |
| collect | `collect_all_logs.py` | dmesg·journalctl·XID 수집 |

## v2 리팩토링 필요

- `checks/base/` 재구성: `preflight/` + `post_install/` + `collect/` 디렉토리로 이동
- `sw_gpu` → `sw_gpu_hw.py` (preflight) + `sw_gpu_sw.py` (post_install) 분리
- `sw_storage` → `sw_storage_hw.py` + `sw_storage_sw.py` 분리
