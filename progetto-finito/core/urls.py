from django.urls import path

from . import views, views_pwa

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("stato-servizi/", views.stato_servizi, name="stato_servizi"),
    path("notifiche/", views.notifiche, name="notifiche"),
    path("notifiche/contatore/", views.notifiche_contatore, name="notifiche_contatore"),
    path("notifiche/segna-lette/", views.notifiche_segna_lette, name="notifiche_segna_lette"),
    path("notifiche/<int:pk>/apri/", views.notifica_apri, name="notifica_apri"),
    path("registro/", views.registro, name="registro"),
    path("healthz/", views.healthz, name="healthz"),
    path("manifest.webmanifest", views_pwa.manifest, name="manifest"),
    path("sw.js", views_pwa.service_worker, name="service_worker"),
    path("offline/", views_pwa.offline, name="offline"),
    # Lo intercetta il service worker (condivisione dal telefono); senza di esso si torna alla pagina di caricamento.
    path("condividi/", views_pwa.condividi, name="condividi"),
]
