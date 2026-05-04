# 인프라 환경

## 검수 시스템 호스트

| 항목 | 값 |
|------|-----|
| 호스트명 | `cpu` |
| IP | `10.100.1.10`, `10.100.1.108` |
| 역할 | 검수 시스템 실행 + NFS 서버 |
| OS | Linux (CPU 전용 서버) |

이 서버에서 Docker Compose로 검수 시스템 전체 스택을 실행.  
동시에 NFS 서버 역할도 겸함 — 결과 파일을 동일 망의 다른 서버에서 마운트해 접근.

---

## 네트워크 구성

| 항목 | 값 |
|------|-----|
| 검수 망 | `10.100.1.0/24` |
| 대상 서버 | 동일 LAN, 배스천 없이 직접 SSH |
| NFS 클라이언트 범위 | `10.100.1.0/24` 전체 |

```
[cpu 서버 :10.100.1.10/108]
    │  Docker Compose (api/worker/redis/db)
    │  NFS 서버 (/srv/inspection/results)
    │
    ├─── SSH ──→ [대상 서버들 — 10.100.1.x]
    │
    └─── NFS ──→ [엔지니어 노트북, 보고서 서버 등 — 10.100.1.x]
```

---

## Docker Compose 스택

| 서비스 | 역할 | 포트 |
|--------|------|------|
| `api` | FastAPI — Job/Report API + WebSocket | `8000` |
| `worker_inspect` | Celery — preflight / post-install / cleanup | — |
| `worker_validate` | Celery — Rule Validator + Verify Agent | — |
| `worker_report` | Celery — PDF/XLSX 생성 | — |
| `worker_sw_install` | Celery — SW 설치 파이프라인 (v2 추가 예정) | — |
| `db` | PostgreSQL 16 | `5432` |
| `redis` | Redis 7.2 — Celery 브로커 + result backend | `6379` |
| `flower` | Celery 모니터링 UI | `5555` |

---

## 볼륨 / 스토리지

| Docker 볼륨 | 컨테이너 내 경로 | 실제 용도 |
|-------------|----------------|----------|
| `inspection_results` | `/srv/inspection/results` | 검수 결과 JSON + 리포트 PDF/XLSX |
| `inspection_logs` | `/srv/inspection/logs` | task별 JSONL 에러 로그 |
| `ssh_keys` | `/etc/inspection/ssh_keys` | SSH 키 (현재 미사용, 패스워드 방식) |
| `redis_data` | Redis 내부 | Celery 큐 + result backend |
| `pg_data` | PostgreSQL 내부 | Job/CheckResult/Report DB |
| `./checks` | `/srv/inspection/checks` (ro) | 검수 스크립트 바인드 마운트 |

---

## NFS 구성

```
# /etc/exports (cpu 서버)
/srv/inspection/results  10.100.1.0/24(rw,sync,no_subtree_check,root_squash)
```

- export 경로: `/srv/inspection/results` — 결과 파일(JSON, PDF, XLSX)만 공개
- `logs/`는 export 대상 외 — 민감 에러 로그 포함 가능성으로 내부 전용
- 클라이언트 마운트 예시:
  ```bash
  mount -t nfs 10.100.1.10:/srv/inspection/results /mnt/inspection
  ```

---

## 환경변수 (.env)

| 변수 | 설명 | 예시 값 |
|------|------|---------|
| `REDIS_URL` | Celery 브로커 | `redis://redis:6379/0` |
| `DATABASE_URL` | PostgreSQL (asyncpg) | `postgresql+asyncpg://inspector:changeme@db:5432/inspection` |
| `ANTHROPIC_API_KEY` | Claude API 키 | `sk-ant-...` |
| `NFS_BASE_PATH` | 결과·로그 루트 경로 | `/srv/inspection` |
| `SSH_KEY_DIR` | SSH 키 디렉토리 | `/etc/inspection/ssh_keys` (현재 미사용) |
| `CLAUDE_MODEL` | 사용 모델 | `claude-sonnet-4-6` |
| `CLAUDE_MAX_TOKENS` | 에이전트 최대 토큰 | `4096` |
| `WS_ENABLED` | WebSocket 활성화 | `true` |

---

## 대상 서버 접근

### 기본 계정

- 기본 계정: `deepgadget` / `deepgadget` (SSH 접속 및 sudo 모두 이 계정)
- 계정 정보는 job 제출 시 유저가 제공. 기본값과 다를 수 있음
- 배스천 없이 검수 시스템(10.100.1.10) → 대상 서버 직접 SSH

### OS 상태

| 케이스 | OS 상태 | 대표 시나리오 |
|--------|---------|--------------|
| 신규 출고 | Ubuntu 클린 설치, 드라이버 없음 | dg5W/dg5R 신제품 |
| RMA | 이전 SW 일부 잔존 가능 | 단종 제품 및 반품 재검수 |

Ubuntu 22.04 / 24.04가 절대 다수. Rocky/RHEL은 SW Planner Agent 처리 (sw-install.md 참조).

### SSH 인증 방식

- 검수 시스템 → 대상 서버: **패스워드 인증만 사용**
- `ssh_keys` 볼륨은 마운트되어 있으나 현재 미사용
- 패스워드는 job 제출 시 유저 입력 → `SecretStr` 처리 → SSH 접속 후 즉시 폐기, DB 미저장
- 상세 보안 처리 규칙 → `.claude/rules/security.md`
