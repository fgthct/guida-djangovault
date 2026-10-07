from django.contrib.auth.views import LoginView

from .forms import LoginForm


class AccessoView(LoginView):
    """Pagina di accesso. Chi è già autenticato viene mandato alla dashboard."""

    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True
