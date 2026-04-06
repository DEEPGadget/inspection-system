# 검수 리포트 규칙

## 출력 형식

| 형식 | 생성 주체 | 저장 경로 |
|------|----------|----------|
| PDF | `workers/report.py` (Jinja2) | `/srv/inspection/results/{job_id}/report.pdf` |
| XLSX | `workers/report.py` (Jinja2) | `/srv/inspection/results/{job_id}/report.xlsx` |

`workers/report.py`는 `q_report` 큐에서 실행. 항상 실행 (pipeline 성공/실패 무관).  
리포트 생성 실패 시 → `job.status = "report_failed"` (검수 결과 자체는 유효, 상태 덮어쓰기 금지).

---

## 리포트 구성 섹션

### 1. 헤더 (표지)

| 항목 | 출처 |
|------|------|
| Job ID | `jobs.id` |
| 검수 일시 | `jobs.created_at` ~ `jobs.updated_at` |
| 대상 서버 | `jobs.target_host` |
| 제품 프로파일 | `jobs.product_profile` |
| 최종 판정 | `jobs.status` → PASS / FAILED / REJECTED 로 표시 |
| SW 요구사항 유무 | `jobs.sw_requirements` null 여부 |

최종 판정 표시 규칙:
- `pass` → **PASS**
- `failed` → **FAILED**
- `rejected` → **REJECTED** *(에이전트 검토 후 불합격)*
- `report_failed` → 리포트 생성 실패이므로 이 상태로는 리포트가 존재하지 않음

---

### 2. H/W 수동 검수 결과 (Phase 1)

Phase 1은 GUI 직접 입력 데이터. 시스템 자동화 범위 밖.

| 표시 항목 | 설명 |
|----------|------|
| 8항목 체크리스트 | GUI 입력값 그대로 표시 |
| 검수자 확인란 | 서명/확인 날인 공간 |

데이터 출처: `jobs` 테이블의 hw_manual_checks 필드 (또는 별도 입력 데이터).

---

### 3. SW 자동 검수 결과

Phase별로 구분하여 표시.

#### 3a. Preflight 결과

| 컬럼 | 내용 |
|------|------|
| 스크립트명 | `check_results.check_name` |
| 판정 | PASS / FAIL / WARN |
| 상세 | `check_results.detail` (key=val 형식 파싱) |
| 에이전트 개입 여부 | Inspect Agent 호출 시 표시 |

#### 3b. SW 설치 결과 (sw_requirements 있는 경우만)

| 컬럼 | 내용 |
|------|------|
| 설치 항목 | sw_requirements.md 파싱 결과 |
| 설치 상태 | 성공 / 실패 / 건너뜀(의존성) |
| 에이전트 개입 여부 | SW Planner Agent 호출 시 표시 |

sw_requirements 없는 경우: "SW 설치 단계 미실행 (RMA / 재검수)" 문구 표시.

#### 3c. Post-install 결과

Preflight 결과와 동일한 형식. stress 테스트 항목은 시간·온도 추이 포함.

---

### 4. Rule Validator 판정 요약

| 항목 | 표시 |
|------|------|
| 전체 판정 | PASS / FAIL |
| FAIL 항목 | check명 + metric + 측정값 + 기준값 |
| WARN 항목 | check명 + metric + 측정값 + agent_zone 기준값 |
| warn_count | WARN 항목 총 수 |

---

### 5. 에이전트 판정 (해당 시에만)

#### Verify Agent (REJECTED 시 필수)

`rejected` 판정 시 반드시 포함:

| 항목 | 내용 |
|------|------|
| 판정 | REJECTED |
| 트리거 항목 | agent_zone 해당 항목 목록 |
| `claude_verdict` | Verify Agent 원문 판정 텍스트 |
| 판정 근거 | Agent가 반환한 사유 요약 |

`failed` vs `rejected` 반드시 구분. `rejected`는 "에이전트가 검토해서 내린 불합격"임을 명시.

#### Inspect Agent / SW Planner Agent (개입 시)

| 항목 | 내용 |
|------|------|
| 호출 사유 | 트리거 조건 (SSH 실패, 설치 실패 등) |
| 에이전트 액션 | 반환된 수정 액션 요약 |
| 결과 | 복구 성공 / 복구 실패 (FAILED 사유) |

---

### 6. 시스템 설정 결과 (sys-config 적용 시)

SW Planner Agent가 적용한 시스템 설정 항목 표시:
- GRUB 파라미터 적용 여부
- CPU 거버너 설정 여부
- GPU 영구 모드 설정 여부
- 자동 업데이트 비활성화 여부

---

### 7. 로그 첨부 안내

리포트 본문에 로그 직접 삽입 금지. 위치 안내만 표시.

```
스크립트 결과: /srv/inspection/results/{job_id}/inspect_raw/  (스크립트별 JSON)
태스크 로그:   /srv/inspection/logs/{job_id}/                  (JSONL 이벤트 로그, 내부 전용)
```

---

## 판정 표시 규칙

| DB 상태 | 리포트 표시 | 색상 |
|--------|------------|------|
| `pass` | PASS | 초록 |
| `failed` | FAILED | 빨강 |
| `rejected` | REJECTED | 주황 (에이전트 검토 불합격) |
| 개별 check `pass` | PASS | 초록 |
| 개별 check `fail` | FAIL | 빨강 |
| 개별 check `warn` | WARN | 노랑 |

XLSX: 조건부 서식으로 색상 적용. PDF: CSS 클래스로 색상 적용.

---

## 민감정보 처리

리포트에 포함 금지:
- `sudo_password`, `password` 류 일체
- `ANTHROPIC_API_KEY` 등 API 키
- SSH 자격증명

포함 허용:
- 서버 IP/호스트명 (디버깅 필수)
- SW 요구사항 원문 (계정 패스워드 제외한 부분)

계정 생성 항목이 sw_requirements에 있는 경우: 계정명만 표시, 패스워드 `***` 처리.
