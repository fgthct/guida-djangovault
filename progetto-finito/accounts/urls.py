from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import AccessoView

app_name = "accounts"

urlpatterns = [
    path("login/", AccessoView.as_view(), name="login"),
    # Il logout accetta solo POST (protezione CSRF): nei template è un piccolo form.
    path("logout/", LogoutView.as_view(), name="logout"),
]
