from django.conf import settings
from django.db import models
from django.urls import reverse


class Conversation(models.Model):
    """Una conversazione con l'assistente. Privata: la vede solo chi l'ha creata."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="proprietario", on_delete=models.CASCADE, related_name="conversations"
    )
    title = models.CharField("titolo", max_length=120)
    # Se valorizzato, le domande riguardano solo questo documento; altrimenti tutto ciò che l'utente può leggere.
    document = models.ForeignKey(
        "documents.Document",
        verbose_name="documento",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    # Se valorizzata (e senza documento), le domande riguardano solo i documenti di questa categoria.
    category = models.ForeignKey(
        "documents.Category",
        verbose_name="categoria",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="conversations",
    )
    created_at = models.DateTimeField("creata il", auto_now_add=True)
    updated_at = models.DateTimeField("aggiornata il", auto_now=True)

    class Meta:
        verbose_name = "conversazione"
        verbose_name_plural = "conversazioni"
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["owner", "-updated_at"], name="conv_owner_updated_idx")]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("assistant:detail", args=[self.pk])


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "Utente"
        ASSISTANT = "assistant", "Assistente"

    class Status(models.TextChoices):
        WAITING = "attesa", "In attesa"  # creato, la generazione non è ancora partita
        GENERATING = "generando", "In generazione"
        DONE = "ok", "Completato"
        ERROR = "errore", "Errore"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=Role)
    content = models.TextField("testo", blank=True)
    status = models.CharField(max_length=10, choices=Status, default=Status.DONE)
    error = models.CharField("errore", max_length=300, blank=True)
    # Passaggi usati per rispondere, numerati come le citazioni [1], [2]…: [{n, document_id, title, version, text}]
    sources = models.JSONField("fonti", default=list, blank=True)
    # Dove compaiono alla lettera i nomi/termini della domanda: [{term, total, documents: [{id, title}]}]
    occurrences = models.JSONField("occorrenze", default=list, blank=True)
    model = models.CharField(max_length=100, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["role", "created_at"], name="msg_role_created_idx")]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"

    @property
    def in_lavorazione(self) -> bool:
        return self.status in (self.Status.WAITING, self.Status.GENERATING)
