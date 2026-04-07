from celery import Celery

app = Celery(
    "inspection",
    include=["workers.inspect", "workers.sw_install", "workers.validate", "workers.report"],
)
app.config_from_object("config.celeryconfig")
