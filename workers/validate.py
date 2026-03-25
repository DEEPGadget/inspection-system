from workers.app import app


@app.task(bind=True, queue="q_validate")
def validate_results(self, job_id: str):
    """Claude API로 검수 결과 판독 — TODO: 구현"""
    pass
