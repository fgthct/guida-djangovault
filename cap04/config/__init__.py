# Importa l'app Celery all'avvio, così i task con @shared_task la trovano.
from .celery import app as celery_app

__all__ = ("celery_app",)
