from celery import Celery

from config.logging import configure_logging

configure_logging()

app = Celery(
    "inspection",
    include=["workers.inspect", "workers.sw_install", "workers.validate", "workers.report"],
)
app.config_from_object("config.celeryconfig")
