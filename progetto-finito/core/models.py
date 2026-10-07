from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Registro delle operazioni importanti. Si scrive, non si modifica né si cancella."""

    timestamp = models.DateTimeField("quando", auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="utente"
    )
    # Copia del nome: resta leggibile se l'utente viene eliminato e serve per gli accessi falliti.
    username = models.CharField("nome utente", max_length=150, blank=True)
    action = models.CharField("azione", max_length=50)
    object_type = models.CharField("tipo oggetto", max_length=50, blank=True)
    object_id = models.PositiveBigIntegerField("id oggetto", null=True, blank=True)
    object_repr = models.CharField("oggetto", max_length=200, blank=True)
    ip_address = models.GenericIPAddressField("indirizzo IP", null=True, blank=True)
    metadata = models.JSONField("dettagli", default=dict, blank=True)

    class Meta:
        verbose_name = "voce del registro"
        verbose_name_plural = "registro delle operazioni"
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["object_type", "object_id", "-timestamp"], name="audit_object_idx"),
            models.Index(fields=["action", "-timestamp"], name="audit_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.username} {self.action}"


class Notification(models.Model):
    """Notifica persistente per un utente. `url` è dove porta il clic."""

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField("tipo", max_length=40)
    message = models.CharField("messaggio", max_length=300)
    url = models.CharField("indirizzo", max_length=300, blank=True)
    created_at = models.DateTimeField("creata il", auto_now_add=True)
    read_at = models.DateTimeField("letta il", null=True, blank=True)

    class Meta:
        verbose_name = "notifica"
        verbose_name_plural = "notifiche"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"], name="notification_inbox_idx")]

    def __str__(self) -> str:
        return f"{self.recipient}: {self.message}"
