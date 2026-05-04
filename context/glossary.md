# Glossary — Inspection System

## 시스템 용어

| 용어 | 설명 |
|------|------|
| **Job** | 검수 작업 단위. UUID v4. 서버 1대 = Job 1개 |
| **Phase** | 스크립트 실행 묶음 단위 (Profile JSON의 `phases` 키): `preflight` / `post_install` / `collect`. JobStatus와 다름. `sw_install`은 Phase 아님 — 파이프라인 단계(JobStatus)임 |
| **Phase 1** | H/W 수동 검수 8항목. GUI 직접 입력, 시스템 자동화 범위 밖 |
| **JobStatus** | DB에 기록되는 Job 전체 수명 상태. Phase + 시스템 처리 상태 + 완료/에러 상태 포함. 전이: `pending → preflight → (sw_install) → rebooting → post_install → validating → cleanup → reporting → pass/failed/rejected` |
| **Profile** | 어떤 스크립트를 어떤 순서로 실행할지 정의한 JSON (`checks/profiles/{name}.json`) |
| **preflight** | Phase: 드라이버/SW 없이 실행 가능한 HW 인식·OS 상태 점검 |
| **post_install** | Phase: 드라이버·SW 의존 점검 + Stress 테스트 |
| **collect** | Phase: dmesg·journalctl·XID 등 로그 수집. JobStatus에는 없음 (post_install 내부에서 실행) |
| **RMA** | 반품·재검수. SW 요구사항 없이 `preflight → post_install`만 수행 |
| **agent_zone** | Rule validator가 경계값으로 판정하는 구간. Verify Agent 호출 트리거 |
| **compact input** | 에이전트 호출 시 실패/애매 항목만 추려서 전달하는 축약 입력 |
| **q_inspect** | Celery 큐: Preflight·Post-install·Cleanup (concurrency 4) |
| **q_sw_install** | Celery 큐: SW 설치 실행 (concurrency 2, rate limit 대응) |
| **q_validate** | Celery 큐: Rule Validator + Verify Agent fallback (concurrency 2) |
| **q_report** | Celery 큐: PDF/XLSX 생성 (concurrency 2) |

## 판정 용어

| 용어 | 설명 |
|------|------|
| **PASS** | 모든 rule threshold 통과 |
| **FAIL** | 하나 이상의 rule threshold 위반 |
| **WARN** | 임계값 미만이나 agent_zone 내 경계값 |
| **claude_verdict** | Verify Agent가 반환한 최종 판정 |

## 에이전트 3종

| 에이전트 | 트리거 | 역할 | 담당 단계 |
|---------|--------|------|----------|
| **Inspect Agent** | SSH 실패, 스크립트 에러, JSON 파싱 에러 | 에러 진단 + 수정 액션 반환 | preflight, post_install, cleanup (q_inspect) |
| **Verify Agent** | agent_zone 해당 경계값, warn_count ≥ 3 | 경계값 종합 판단 | validating (q_validate) |
| **SW Planner Agent** | 비정형 SW 요구사항, SW 설치 실패 | 설치 계획 JSON 생성 | sw_install (q_sw_install) |

설치 실패 시 담당 에이전트: **큐 기준**으로 결정. SSH 실패는 어느 단계든 Inspect Agent. 상세 → `.claude/rules/agents.md`

## 임계값 (gpu_server 프로파일 기준)

| 항목 | FAIL 기준 | Agent Zone |
|------|-----------|------------|
| GPU 최고 온도 | > 87°C | > 75°C |
| CPU 최고 온도 | > 100°C | > 85°C |
| NCCL 2GPU NVLink busbw | < 30 GB/s | < 25 GB/s |
| NCCL 4GPU AllReduce busbw | < 5 GB/s | < 3 GB/s |
| sleep.target | masked 아님 | — |
| unattended-upgrades | 활성화 | — |

## 인프라

| 서비스 | 포트 | 역할 |
|--------|------|------|
| FastAPI | 8000 | REST API + WebSocket |
| Redis | 6379 | Celery 브로커 + pub/sub |
| PostgreSQL | 5432 | Job·결과·리포트 영속화 |
| Flower | 5555 | Celery 태스크 모니터링 |
| NFS | — | 결과 파일 저장 (`/srv/inspection/`) |
