from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Utente dell'applicazione.

    Modello personalizzato fin dall'inizio: cambiarlo a progetto avviato è molto
    più costoso. Per ora non aggiunge campi.
    """

    class Meta:
        verbose_name = "utente"
        verbose_name_plural = "utenti"
