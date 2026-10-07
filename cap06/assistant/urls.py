from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("chiedi/", views.index, name="index"),
    path("chiedi/nuova/", views.new, name="new"),
    path("chiedi/messaggi/<int:pk>/", views.message, name="message"),
    path("chiedi/messaggi/<int:pk>/flusso/", views.stream, name="stream"),
    path("chiedi/messaggi/<int:pk>/fonti/<int:n>/", views.fonte, name="fonte"),
    path("chiedi/<int:pk>/", views.detail, name="detail"),
    path("chiedi/<int:pk>/domanda/", views.ask, name="ask"),
    path("chiedi/<int:pk>/elimina/", views.delete, name="delete"),
]
