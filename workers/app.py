from celery import Celery

app = Celery(
    "inspection",
    # TODO(C-5): workers.sw_install 구현 시 include에 추가
    include=["workers.inspect", "workers.validate", "workers.report"],
)
app.config_from_object("config.celeryconfig")
