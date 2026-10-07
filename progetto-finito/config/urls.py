from django.contrib import admin
from django.urls import include, path

from django.conf import settings

# Nessuna rotta per i file media: i documenti passeranno da view autenticate.
urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("documents.urls")),
    path("", include("assistant.urls")),
    path("", include("core.urls")),
]
