# 보안 규칙

## 인증 방식 (사내 전용)

- 현재: password 방식 (`ServerAuth.method="password"`)
- `SecretStr`로 자동 마스킹 (repr/log에서 `**********` 출력)
- SSH 접속 후 `job_data`에서 password 즉시 제거, DB 미저장
- 재검사 시 유저 재입력 요청
- 추후 ssh_key 방식 확장 시 auth 스키마 backward-compatible 유지

## structlog 민감필드 마스킹

`config/logging.py`에서 프로세서 등록:
- 마스킹 대상 키: `password`, `secret`, `token`, `api_key`

## Bash 명령 보안

검수 스크립트에서 `shell=True` 사용 이유:
- 파이프/리다이렉트 필요한 시스템 명령 조합
- 대상 서버에서 1회성 실행 후 폐기되는 스크립트
- injection 입력 없음 (환경변수만 수신)

명령 injection 금지:
- 스크립트 인자는 환경변수로만 수신
- 유저 입력을 shell 명령에 직접 삽입 금지

## 금지 Bash 명령 (settings.json deny 목록)

- `rm -rf /`로 시작하는 명령
- `sudo rm`
- `DROP DATABASE`
- `docker system prune`
- `ssh *` (SSH는 asyncssh 라이브러리로만)
