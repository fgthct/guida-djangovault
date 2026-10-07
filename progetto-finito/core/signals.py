from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .services import audit


@receiver(user_logged_in)
def accesso(sender, request, user, **kwargs):
    audit.record("auth.login", user=user)


@receiver(user_logged_out)
def uscita(sender, request, user, **kwargs):
    if user is not None:
        audit.record("auth.logout", user=user)


@receiver(user_login_failed)
def accesso_fallito(sender, credentials, request=None, **kwargs):
    # Mai la password: solo il nome utente provato.
    audit.record("auth.login_failed", username=str(credentials.get("username", ""))[:150])
