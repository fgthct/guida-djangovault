from celery import shared_task

from .services import maintenance


@shared_task(name="core.manutenzione")
def manutenzione_task() -> dict[str, int]:
    return maintenance.pulisci()
