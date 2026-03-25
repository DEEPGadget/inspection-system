from config.settings import settings

broker_url = settings.redis_url
result_backend = settings.redis_url.replace("/0", "/1")

task_routes = {
    "workers.inspect.*": {"queue": "q_inspect"},
    "workers.validate.*": {"queue": "q_validate"},
    "workers.report.*": {"queue": "q_report"},
}

task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
task_track_started = True
task_acks_late = True
worker_prefetch_multiplier = 1
worker_max_tasks_per_child = 50

# stress test는 2시간까지 허용
task_soft_time_limit = 7200
task_time_limit = 7500
