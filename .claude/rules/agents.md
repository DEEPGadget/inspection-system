# 에이전트 호출 규칙

## 핵심 원칙: 토큰 0

- 정상 플로우에서 에이전트 미호출
- 시스템이 스크립트 직접 실행 → JSON 수집 (토큰 0)
- Rule validator가 threshold 비교 → 명확한 PASS/FAIL (토큰 0)
- 에이전트 호출은 세 가지 경우로 제한

## 에이전트 3종

### Inspect Agent
- **트리거**: SSH 실패, 스크립트 실행 에러 (exit code ≠ 0), JSON 파싱 에러
- **역할**: 에러 진단 + 수정 액션 JSON 반환 → 시스템이 실행, 복구 불가 시 FAIL 사유 반환
- **모델**: `claude-sonnet-4-20250514` / `max_tokens: 1024`
- **담당 단계**: preflight, post_install, cleanup (q_inspect 큐 내 모든 오류)

### Verify Agent
- **트리거**:
  - `validation.rules`의 `agent_zone` 구간 해당 경계값
  - `warn_count_threshold` 초과 (기본 3개)
- **역할**: 경계값 종합 판단, 복합 WARN 분석, `special_notes` 예외 처리
- **모델**: `claude-sonnet-4-20250514` / `max_tokens: 512`
- **담당 단계**: validating (q_validate 큐 내 판정 단계만)

### SW Planner Agent
- **트리거**: 비정형 SW 요구 해석 필요, 버전 호환 판정 필요, SW 설치 실패
- **역할**: 자유 형식 `.md` 요구사항 구조화, driver-cuda-torch 매트릭스 결정, 설치 실행계획 JSON 생성, 대체안/복구안 반환
- **모델**: `claude-sonnet-4-20250514` / `max_tokens: 1024`
- **담당 단계**: sw_install (q_sw_install 큐 내 모든 판단)

## 역할 경계 — 설치 실패 시 판단 기준

"설치 실패"는 발생 단계에 따라 담당 에이전트가 다름:

| 실패 상황 | 담당 에이전트 | 근거 |
|----------|-------------|------|
| SSH 접속 실패 (어느 단계든) | Inspect Agent | 연결 문제, SW 무관 |
| preflight 스크립트 실패 | Inspect Agent | q_inspect 단계 |
| post_install 스크립트 실패 | Inspect Agent | q_inspect 단계 |
| sw_install 단계 설치 실패 | SW Planner Agent | q_sw_install 단계, 설치 계획 재수립 필요 |
| sw_install 단계 SSH 실패 | Inspect Agent | 연결 문제이므로 예외적으로 Inspect |
| Rule Validator 경계값 | Verify Agent | q_validate 단계 |

**핵심 원칙**: 큐(q_inspect / q_sw_install / q_validate)가 담당 에이전트를 결정. SSH 실패는 어느 큐든 Inspect Agent.

## agent_gateway.py 역할

- 에이전트 호출 판단 로직
- compact input 구성 (실패/애매 항목만 전달)
- 호출 결과 파싱 → 시스템 액션 변환

## 재검사 플로우

- 실패 항목만 시스템이 직접 재실행
- rule validator 재판정 → 여전히 애매하면 verify agent 재호출

## 목표 토큰 사용량

- 정상 플로우: 0 tokens
- 전체 job 평균: ~400 tokens
