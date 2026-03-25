from celery import Celery

app = Celery("inspection")
app.config_from_object("config.celeryconfig")
app.autodiscover_tasks(["workers"])
