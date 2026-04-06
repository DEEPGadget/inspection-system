# Workers 규칙

## Celery 큐 구성

| 큐 | Concurrency | 역할 |
|----|-------------|------|
| `q_inspect` | 4 | Preflight Runner, Post-install Runner, Cleanup |
| `q_sw_install` | 2 | SW Install Runner (에이전트 호출 가능, rate limit 대응) |
| `q_validate` | 2 | Rule Validator + Verify Agent fallback |
| `q_report` | 2 | PDF/XLSX 생성 |

## Worker 파일 역할

| 파일 | 역할 |
|------|------|
| `app.py` | Celery app 인스턴스 (q_inspect, q_sw_install, q_validate, q_report) |
| `inspect.py` | preflight/post-install 실행 + cleanup task |
| `validate.py` | rule validator 우선, Verify Agent fallback |
| `report.py` | Jinja2 PDF/XLSX |
| `ssh_client.py` (미구현) | SSH 접속 관리 (SecretStr 지원, 접속 후 pw 즉시 폐기) |
| `rule_validator.py` (미구현) | `validation.rules` 기반 PASS/FAIL 판정 (토큰 0) |
| `agent_gateway.py` (미구현) | 에이전트 호출 판단 + compact input 구성 |
| `sw_install.py` (미구현) | q_sw_install — 설치 실행/검증/재시도 |
| `sw_planner.py` (미구현) | SW 요구사항 파싱 + 계획 JSON 생성 오케스트레이션 |

## 핵심 Celery 설정

- `task_acks_late=True`: 워커 crash 시 재할당 보장
- stress timeout: soft `7200s`, hard `7500s`
- per-script SSH timeout: 프로파일 phase별 `timeout` → `conn.run(timeout=N)`

## Task 코드 패턴

```python
@app.task(bind=True, queue="q_inspect", acks_late=True)
def run_phase(self, job_id: str, phase: str) -> dict:
    ...
```

- Celery task 내부: sync 허용 (asyncio.run() 감싸서 SSH 호출)
- 에러 시 `self.retry()` 또는 명시적 FAIL 상태 기록

## Job 상태 전이

```
pending → preflight → sw_install* → rebooting* → post_install → validating → cleanup → reporting → pass
                                                                                                  ↘ failed
                                                                                                  ↘ rejected
                                                                                                  ↘ report_failed
* sw_install: sw_requirements 있을 때만 / rebooting: nvidia-driver 또는 GRUB 변경 시만
```

## pre_install 실행 방식

```python
# sudo 없이 password stdin 전달
await conn.run("sudo apt-get install -y ...", input=f"{password}\n", timeout=300)
```

TTY 없이 sudo 동작. password는 접속 후 즉시 job_data에서 제거.
