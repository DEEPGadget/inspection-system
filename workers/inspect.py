from workers.app import app


@app.task(bind=True, queue="q_inspect", max_retries=3)
def inspect_server(self, job_id: str, server_info: dict, check_items: list[str]):
    """검수 실행 — TODO: SSH 접속 + 스크립트 실행 구현"""
    pass
