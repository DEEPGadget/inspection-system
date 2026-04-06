"""
SSH 접속 보안 헬퍼.

- wrap_password: Celery task 진입 시 str → SecretStr 즉시 변환
- secret_input: sudo -S 용 stdin 문자열 반환 (SecretStr에서만 추출)

사용 패턴:
    secret = wrap_password(sudo_password)   # task 진입 직후
    ok, out = await _apt_install(conn, pkgs, secret)
    # 이후 sudo_password(str)는 사용하지 않음
"""

from pydantic import SecretStr


def wrap_password(pw: str | None) -> SecretStr | None:
    """평문 str을 SecretStr로 감쌈. None은 None 그대로 반환."""
    return SecretStr(pw) if pw else None


def secret_input(secret: SecretStr | None) -> str:
    """sudo -S stdin용 문자열 반환. None이면 빈 문자열."""
    return f"{secret.get_secret_value()}\n" if secret else ""
