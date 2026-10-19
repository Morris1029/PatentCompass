from celery import Celery
from celery.signals import worker_process_init

from app.config import get_settings

celery_app = Celery("patentcompass", broker=get_settings().redis_url)
celery_app.conf.update(task_serializer="json", accept_content=["json"], task_ignore_result=True,
                       task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                       broker_connection_retry_on_startup=True, broker_connection_timeout=2,
                       broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 2,
                                                 "visibility_timeout": 3600})


@worker_process_init.connect
def reset_connections(**kwargs):
    from app.db import engine
    engine.dispose(close=False)


@celery_app.task(name="patentcompass.execute_run")
def run_task(run_id: str):
    from app.jobs import execute_run
    execute_run(run_id)
