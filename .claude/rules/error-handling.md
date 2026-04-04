# 에러 처리 규칙

## 파이프라인 실패 전파 원칙

Cleanup과 Report는 항상 실행. 그 외 단계는 실패 시 후속 단계를 건너뜀.

```
Preflight → SW Install → Post-install → Rule Validate → Cleanup → Report
                                                          ↑          ↑
                                                    항상 실행    항상 실행
```

### 단계별 실패 전파 규칙

| 실패 단계 | 건너뛰는 단계 | 계속 실행 |
|----------|--------------|----------|
| Preflight | SW Install, Post-install, Rule Validate | Cleanup, Report |
| SW Install | Post-install, Rule Validate | Cleanup, Report |
| Post-install | Rule Validate | Cleanup, Report |
| Rule Validate → REJECTED | 없음 | Cleanup, Report |

- SW Install 실패 시 Post-install을 **반드시 건너뜀** (SW 없는 상태에서 본검수 불가)
- Cleanup 실패는 `on_failure: "warn"` 처리 (프로파일 cleanup 정책 따름, job 상태에 영향 없음)
- Report 실패는 job 상태를 `report_failed`로 마킹, 다른 상태는 그대로 유지

---

## Job 상태 전이

```
pending
  → preflight
      → sw_install (sw_requirements 있을 때만)
          → post_install
              → validating
                  → cleanup
                      → reporting
                          → pass
                          → failed
                          → rejected      ← Verify Agent 판정 불합격
  (단계 실패 시)  → cleanup → reporting → failed
                                         → rejected
```

### JobStatus 전체 목록

| 상태 | 의미 |
|------|------|
| `pending` | 생성됨, 미시작 |
| `preflight` | Preflight Runner 실행 중 |
| `sw_install` | SW Install Runner 실행 중 |
| `rebooting` | nvidia-driver 설치 후 reboot 대기 중 |
| `post_install` | Post-install Runner 실행 중 |
| `validating` | Rule Validator / Verify Agent 실행 중 |
| `cleanup` | Cleanup Runner 실행 중 |
| `reporting` | Report Generator 실행 중 |
| `pass` | 검수 통과 |
| `failed` | 시스템/스크립트 오류 또는 Rule Validator FAIL 판정 |
| `rejected` | Verify Agent 불합격 판정 (경계값 검토 후 판단) |
| `report_failed` | 리포트 생성 실패 (검수 결과 자체는 유효) |
| ~~`inspecting`~~ | v1 deprecated — DB 값 유지, 코드에서 미사용 |
| ~~`error`~~ | v1 deprecated — DB 값 유지, 코드에서 미사용 |

`failed`와 `rejected`는 반드시 구분. `rejected`는 "에이전트가 검토해서 내린 불합격"임을 리포트에 명시.

---

## 재시도 정책

### 재시도 O (최대 3회, 20초 간격)

```python
@app.task(bind=True, max_retries=3, default_retry_delay=20)
def run_phase(self, job_id: str, ...):
    try:
        ...
    except (SSHConnectionError, asyncssh.DisconnectError, anthropic.APIConnectionError,
            anthropic.RateLimitError) as exc:
        raise self.retry(exc=exc)
```

| 조건 | 예시 |
|------|------|
| SSH 연결 실패 | `asyncssh.DisconnectError`, 타임아웃, connection refused |
| Anthropic API 통신 실패 | `APIConnectionError`, `RateLimitError` |

### 재시도 X (즉시 FAILED 처리)

| 조건 | 처리 |
|------|------|
| 스크립트 실행 오류 (exit code ≠ 0) | 즉시 FAILED, 에이전트 에스컬레이션은 별도 로직 |
| Agent 토큰 예산 초과 (`max_tokens` 도달) | 즉시 FAILED |
| 대상 서버 치명적 오류 (freezing, kernel panic) | 즉시 FAILED |
| 논리적 실패 (프로파일 파싱 오류, 스키마 불일치 등) | 즉시 FAILED |
| 의존성 위반으로 인한 설치 실패 | 즉시 FAILED (재시도 없음, sw-install.md 참조) |

3회 재시도 후 모두 실패하면 `job.status = "failed"` 로 확정.

---

## SSH 연결 실패 vs 스크립트 실행 실패 구분

두 오류는 반드시 다른 `error_type`으로 기록.

| 구분 | `error_type` | 처리 |
|------|-------------|------|
| SSH 연결 실패 | `ssh_connection_error` | 재시도 3회 → FAILED |
| 스크립트 실행 실패 | `script_execution_error` | 즉시 FAILED |
| SSH 접속 후 타임아웃 | `ssh_timeout` | 재시도 3회 → FAILED |
| 스크립트 JSON 파싱 실패 | `script_output_parse_error` | 즉시 FAILED |

에러 로그에 반드시 포함:
- `error_type`: 위 분류 중 하나
- `phase`: 어느 단계에서 발생했는지
- `script`: 실패한 스크립트 이름 (스크립트 실패 시)
- `exit_code`: 스크립트 exit code (스크립트 실패 시)
- `stderr`: 스크립트 stderr 출력 (스크립트 실패 시, 민감정보 마스킹 후)
- `retry_count`: 현재 재시도 횟수 (SSH/API 실패 시)

---

## 에러 로그

### 저장 위치

```
# 컨테이너 내부
/srv/inspection/logs/{job_id}/{task}.jsonl

# docker-compose.yml 볼륨 마운트
inspection_logs:/srv/inspection/logs

# NFS export 대상 외 (민감 에러 로그 포함 가능성으로 내부 전용)
```

task 이름별 파일 분리:

| 파일 | 내용 |
|------|------|
| `preflight.jsonl` | Preflight Runner 로그 |
| `sw_install.jsonl` | SW Install Runner 로그 |
| `post_install.jsonl` | Post-install Runner 로그 |
| `validate.jsonl` | Rule Validator + Verify Agent 로그 |
| `cleanup.jsonl` | Cleanup Runner 로그 |
| `report.jsonl` | Report Generator 로그 |

JSONL 형식 (1줄 = 1 이벤트):
```json
{"timestamp": "2026-04-03T10:00:00Z", "level": "error", "job_id": "...", "task": "preflight", "phase": "preflight", "error_type": "ssh_connection_error", "msg": "SSH connection refused", "retry_count": 2}
```

### 민감정보 마스킹

`config/logging.py`의 structlog processor에서 직렬화 직전에 처리.

| 필드 | 처리 |
|------|------|
| `password`, `sudo_password` | 완전 제거 → `"***"` |
| `api_key`, `anthropic_api_key` | 완전 제거 → `"***"` |
| `token` (인증 토큰류) | 완전 제거 → `"***"` |
| traceback 내 환경변수 | `PASSWORD`, `SECRET`, `TOKEN`, `KEY` 포함 키 → `"***"` |
| 서버 IP/호스트명 | 유지 (디버깅 필수) |
| SSH 유저명 | 유지 |

마스킹 대상 키는 대소문자 구분 없이 적용 (`password`, `PASSWORD`, `Password` 모두 처리).

---

## 에이전트 에스컬레이션과 에러의 구분

에이전트 호출은 에러 처리가 아닌 별도 판단 로직. 에러 처리 흐름과 혼용 금지.

| 상황 | 처리 주체 | 에러 여부 |
|------|----------|----------|
| 스크립트 실행 오류, SSH 실패 | Inspect Agent 호출 | 에러 → 에이전트 판단 |
| Rule Validator 경계값/복합 WARN | Verify Agent 호출 | 에러 아님 → 에이전트 판단 |
| 비정형 SW 요구사항, 설치 실패 | SW Planner Agent 호출 | 에러 → 에이전트 판단 |

에이전트가 "복구 불가" 또는 "불합격" 판정을 내린 경우:
- Inspect Agent → `job.status = "failed"`, 에이전트 판정 사유 로그 기록
- Verify Agent → `job.status = "rejected"`, 에이전트 판정 사유 리포트에 명시
- SW Planner Agent → `job.status = "failed"`, 후속 단계(Post-install) 건너뜀
