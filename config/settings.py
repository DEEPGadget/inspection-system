from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DB
    database_url: str = "postgresql+asyncpg://inspector:changeme@db:5432/inspection"
    redis_url: str = "redis://redis:6379/0"

    # NFS
    nfs_base_path: str = "/srv/inspection"

    # Claude API
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 4096

    # SSH
    ssh_key_dir: str = "/etc/inspection/ssh_keys"

    # WebSocket
    ws_enabled: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
