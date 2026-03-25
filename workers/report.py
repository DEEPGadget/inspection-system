from workers.app import app


@app.task(bind=True, queue="q_report")
def generate_report(self, job_id: str):
    """리포트 생성 (PDF/XLSX) — TODO: 구현"""
    pass
