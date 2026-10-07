import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import models
from django.contrib.auth.models import Group
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from pgvector.django import HnswIndex, VectorField
from django.db.models import Q
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone


class Category(models.Model):
    name = models.CharField("nome", max_length=80)
    # Una riga che dice cosa ci va dentro. La legge la classificazione automatica (Claude Haiku): conoscere solo il nome
    # e qualche titolo non basta a distinguere, per esempio, «Guide server e deploy» da «Prodotti (Morpheo…)».
    description = models.CharField("descrizione", max_length=300, blank=True)

    class Meta:
        verbose_name = "categoria"
        verbose_name_plural = "categorie"
        ordering = ["name"]
        constraints = [
            # Il nome è unico senza distinguere maiuscole/minuscole ("Fatture" = "fatture").
            models.UniqueConstraint(Lower("name"), name="uniq_category_name_ci"),
        ]

    def __str__(self) -> str:
        return self.name


class DocumentQuerySet(models.QuerySet):
    def visible_to(self, user) -> "DocumentQuerySet":
        """Documenti che `user` può vedere. Va usato SEMPRE al posto di `objects.all()` nelle view."""
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_superuser:
            return self
        # Proprietario, oppure destinatario di una condivisione (diretta o tramite un suo gruppo).
        condivisi = DocumentPermission.objects.filter(Q(user=user) | Q(group__user=user)).values("document_id")
        return self.filter(Q(owner=user) | Q(pk__in=condivisi))


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Bozza"
        IN_REVIEW = "in_review", "In revisione"
        APPROVED = "approved", "Approvato"
        ARCHIVED = "archived", "Archiviato"

    title = models.CharField("titolo", max_length=200)
    description = models.TextField("descrizione", blank=True)
    category = models.ForeignKey(
        Category,
        verbose_name="categoria",
        null=True,
        blank=True,
        on_delete=models.PROTECT,  # non si cancella una categoria ancora in uso
        related_name="documents",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="proprietario",
        on_delete=models.PROTECT,
        related_name="documents",
    )
    # Le transizioni di stato saranno operazioni di dominio (Fase 5): il campo non è nei form.
    status = models.CharField("stato", max_length=20, choices=Status, default=Status.DRAFT)
    created_at = models.DateTimeField("creato il", auto_now_add=True)
    updated_at = models.DateTimeField("aggiornato il", auto_now=True)
    # Privacy: solo i documenti con questo consenso possono essere letti dall'assistente IA (i passaggi pertinenti
    # vengono inviati all'API di Anthropic). Falso per impostazione predefinita; lo decide chi amministra il documento.
    ai_allowed = models.BooleanField("consenti all'assistente IA", default=False)
    # Indice di ricerca full-text di titolo (peso A) e descrizione (peso B), mantenuto da PostgreSQL
    # (colonna generata): non si scrive mai a mano e non può andare fuori sincrono.
    search_vector = models.GeneratedField(
        expression=SearchVector("title", weight="A", config="italian_unaccent")
        + SearchVector("description", weight="B", config="italian_unaccent"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    current_version = models.ForeignKey(
        "DocumentVersion",
        verbose_name="versione corrente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = DocumentQuerySet.as_manager()

    class Meta:
        verbose_name = "documento"
        verbose_name_plural = "documenti"
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["-updated_at"], name="document_updated_idx"),
            GinIndex(fields=["search_vector"], name="document_search_gin"),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("documents:detail", args=[self.pk])


IMMAGINI = {"png", "jpg", "jpeg", "gif", "webp"}


def percorso_file(instance: "DocumentVersion", filename: str) -> str:
    """Percorso di archiviazione: nome casuale, mai il nome scelto dall'utente.

    Il nome originale resta in `original_name`. Evita collisioni, path traversal
    e nomi che rivelano il contenuto.
    """
    estensione = Path(filename).suffix.lower()[:10]
    nome = uuid.uuid4().hex
    return f"documenti/{nome[:2]}/{nome}{estensione}"


def percorso_anteprima(instance: "DocumentVersion", filename: str) -> str:
    """Anteprima generata dal sistema: sempre PNG con nome casuale, in una cartella a parte."""
    nome = uuid.uuid4().hex
    return f"anteprime/{nome[:2]}/{nome}.png"


class DocumentVersion(models.Model):
    """Una versione di un documento. Un file non viene mai sovrascritto."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField("numero versione")
    file = models.FileField("file", upload_to=percorso_file, max_length=255)
    original_name = models.CharField("nome originale", max_length=255)
    content_type = models.CharField("tipo", max_length=100, blank=True)
    size = models.PositiveBigIntegerField("dimensione (byte)")
    checksum = models.CharField("SHA-256", max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="creata da"
    )
    created_at = models.DateTimeField("creata il", auto_now_add=True)
    comment = models.CharField("nota", max_length=255, blank=True)
    # Se la versione nasce da un ripristino, indica da quale versione è stata copiata.
    restored_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="ripristinata da"
    )

    class Processing(models.TextChoices):
        PENDING = "pending", "In coda"
        RUNNING = "running", "In corso"
        DONE = "done", "Completata"
        SKIPPED = "skipped", "Non applicabile"  # formato senza testo da estrarre
        FAILED = "failed", "Errore"

    # Risultato dell'elaborazione asincrona (Fase 6, vedi services/processing.py).
    extracted_text = models.TextField("testo estratto", blank=True)
    # Indice full-text del testo estratto (colonna generata da PostgreSQL, si aggiorna da sola).
    search_vector = models.GeneratedField(
        expression=SearchVector("extracted_text", weight="C", config="italian_unaccent"),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    processing_status = models.CharField(
        "elaborazione", max_length=10, choices=Processing, default=Processing.PENDING
    )
    processing_message = models.CharField("esito elaborazione", max_length=500, blank=True)
    processing_attempts = models.PositiveSmallIntegerField("tentativi", default=0)
    # Ultimo movimento (messa in coda, inizio): serve a riconoscere le elaborazioni interrotte.
    processing_updated_at = models.DateTimeField("ultimo movimento elaborazione", null=True, blank=True)
    processed_at = models.DateTimeField("elaborata il", null=True, blank=True)
    page_count = models.PositiveIntegerField("pagine", null=True, blank=True)
    ocr_used = models.BooleanField("OCR usato", default=False)
    text_truncated = models.BooleanField("testo troncato", default=False)
    preview = models.FileField("anteprima", upload_to=percorso_anteprima, max_length=255, blank=True)
    # Indicizzazione per significato (Fase 6b, vedi services/indexing.py): con quale modello è stata indicizzata
    # (vuoto = mai) e quando; `index_attempted_at` evita di riprovare di continuo se l'indicizzazione non riesce.
    indexed_model = models.CharField("modello di indicizzazione", max_length=100, blank=True)
    indexed_at = models.DateTimeField("indicizzata il", null=True, blank=True)
    index_attempted_at = models.DateTimeField("ultimo tentativo di indicizzazione", null=True, blank=True)
    # Un solo vettore per tutta la versione (media dei passaggi): serve a trovare i documenti simili e a proporre la
    # categoria (services/classificazione.py). Si ricalcola a ogni indicizzazione.
    doc_vector = VectorField("vettore del documento", dimensions=384, null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "versione"
        verbose_name_plural = "versioni"
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "version_number"], name="uniq_document_version"),
        ]
        indexes = [
            models.Index(fields=["checksum"], name="version_checksum_idx"),
            models.Index(fields=["processing_status", "processing_updated_at"], name="version_processing_idx"),
            GinIndex(fields=["search_vector"], name="version_search_gin"),
        ]

    def __str__(self) -> str:
        return f"{self.document} v{self.version_number}"

    @property
    def elaborazione_in_corso(self) -> bool:
        """True finché l'elaborazione non ha un esito: la pagina continua a chiedere aggiornamenti."""
        return self.processing_status in (self.Processing.PENDING, self.Processing.RUNNING)

    # Stato dell'indice per significato (solo per versioni con testo già pronto).
    INDICE_PRONTO, INDICE_VUOTO, INDICE_ATTESA, INDICE_CORSO, INDICE_FALLITO = "pronto", "vuoto", "attesa", "corso", "fallito"
    INDICE_TENTATIVO_RECENTE = timedelta(minutes=10)

    @property
    def stato_indice(self) -> str | None:
        """None se il testo non è ancora pronto. Altrimenti pronto / vuoto (nessun testo) / attesa / corso / fallito."""
        if self.processing_status != self.Processing.DONE:
            return None
        if self.indexed_model == settings.EMBEDDING_MODEL:
            return self.INDICE_PRONTO if self.passaggi_indicizzati else self.INDICE_VUOTO
        if self.index_attempted_at is None:
            return self.INDICE_ATTESA
        if timezone.now() - self.index_attempted_at < self.INDICE_TENTATIVO_RECENTE:
            return self.INDICE_CORSO
        return self.INDICE_FALLITO

    @property
    def passaggi_indicizzati(self) -> int:
        """Numero di passaggi (usa `n_passaggi` se la query l'ha già calcolato, per non fare una query a riga)."""
        n = getattr(self, "n_passaggi", None)
        return n if n is not None else self.chunks.count()

    @property
    def indicizzazione_in_corso(self) -> bool:
        return self.stato_indice in (self.INDICE_ATTESA, self.INDICE_CORSO)

    @property
    def anteprima_e_pagina(self) -> bool:
        """Anteprima di una pagina di documento (sfondo chiaro): in tema scuro la si mostra invertita."""
        return Path(self.original_name).suffix.lower().lstrip(".") not in IMMAGINI


class DocumentChunk(models.Model):
    """Un passaggio del testo di una versione, con il suo vettore (ricerca per significato)."""

    version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField("posizione")
    text = models.TextField("testo")
    embedding = VectorField(dimensions=384)

    class Meta:
        verbose_name = "passaggio"
        verbose_name_plural = "passaggi"
        ordering = ["version_id", "position"]
        constraints = [models.UniqueConstraint(fields=["version", "position"], name="uniq_chunk_position")]
        indexes = [
            # Similarità del coseno (i vettori sono normalizzati): HNSW dà ricerche veloci anche con molti passaggi.
            HnswIndex(
                name="chunk_embedding_hnsw", fields=["embedding"], m=16, ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version} · passaggio {self.position}"


class ClassificationSuggestion(models.Model):
    """Proposta di classificazione di un documento (categoria, riassunto, parole chiave).

    Una sola per documento. `REQUESTED` = va classificato appena l'indicizzazione è finita; `PENDING` = c'è una
    proposta da rivedere (coda «Da smistare»); `AUTO` = applicata da sola perché la fiducia era alta (si può
    annullare); `ACCEPTED`/`REJECTED`/`UNDONE` = decisioni di una persona; `NONE` = nessuna proposta utile.
    Nessun documento cambia categoria in silenzio: ogni applicazione lascia una traccia nel registro attività.
    """

    class Status(models.TextChoices):
        REQUESTED = "richiesta", "In attesa di classificazione"
        PENDING = "proposta", "Da rivedere"
        AUTO = "automatica", "Applicata in automatico"
        ACCEPTED = "accettata", "Accettata"
        REJECTED = "rifiutata", "Rifiutata"
        UNDONE = "annullata", "Annullata"
        NONE = "nessuna", "Nessuna proposta"

    document = models.OneToOneField(Document, on_delete=models.CASCADE, related_name="suggestion")
    status = models.CharField("stato", max_length=12, choices=Status, default=Status.REQUESTED)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # La categoria proposta dal sistema, che non cambia mai: `category` diventa quella scelta da chi accetta.
    # Confrontandole si misura quanto la classificazione indovina davvero (`classifica --valuta`).
    proposed_category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    new_category_name = models.CharField("nuova categoria proposta", max_length=80, blank=True)
    confidence = models.FloatField("fiducia", default=0)
    source = models.CharField("origine", max_length=40, blank=True)  # «vicini», «ia», «vicini+ia»
    reason = models.CharField("motivo", max_length=500, blank=True)
    summary = models.TextField("riassunto", blank=True)
    keywords = models.JSONField("parole chiave", default=list, blank=True)
    neighbors = models.JSONField("documenti simili", default=list, blank=True)  # [{id, titolo, categoria, sim}]
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "proposta di classificazione"
        verbose_name_plural = "proposte di classificazione"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return f"{self.document} → {self.nome_categoria or '—'} ({self.get_status_display()})"

    @property
    def nome_categoria(self) -> str:
        return self.category.name if self.category_id else self.new_category_name

    @property
    def fiducia_percento(self) -> int:
        return round(self.confidence * 100)


class DocumentPermission(models.Model):
    """Condivisione di un documento con un utente OPPURE con un gruppo, a un certo livello.

    Il proprietario non ha bisogno di una riga: è sempre amministratore.
    """

    class Level(models.IntegerChoices):
        READ = 10, "Lettura"
        WRITE = 20, "Modifica"
        ADMIN = 30, "Amministrazione"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="permissions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    level = models.PositiveSmallIntegerField("livello", choices=Level, default=Level.READ)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="concesso da"
    )
    created_at = models.DateTimeField("concesso il", auto_now_add=True)

    class Meta:
        verbose_name = "condivisione"
        verbose_name_plural = "condivisioni"
        ordering = ["group_id", "user_id", "id"]
        constraints = [
            # Esattamente uno tra utente e gruppo.
            models.CheckConstraint(
                condition=(Q(user__isnull=False) & Q(group__isnull=True))
                | (Q(user__isnull=True) & Q(group__isnull=False)),
                name="permission_user_xor_group",
            ),
            models.UniqueConstraint(
                fields=["document", "user"], condition=Q(user__isnull=False), name="uniq_permission_user"
            ),
            models.UniqueConstraint(
                fields=["document", "group"], condition=Q(group__isnull=False), name="uniq_permission_group"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document} → {self.user or self.group}: {self.get_level_display()}"

    @property
    def grantee(self):
        return self.user or self.group


class ReviewRound(models.Model):
    """Un giro di revisione: il proprietario sceglie i revisori e se devono rispondere in sequenza."""

    class Outcome(models.TextChoices):
        PENDING = "pending", "In corso"
        APPROVED = "approved", "Approvato"
        REJECTED = "rejected", "Rimandato"
        WITHDRAWN = "withdrawn", "Ritirato"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="review_rounds")
    number = models.PositiveIntegerField("giro")
    sequential = models.BooleanField("in sequenza", default=False)
    # La versione rivista: le approvazioni valgono per questo file e non per un altro.
    version = models.ForeignKey(DocumentVersion, null=True, on_delete=models.SET_NULL, related_name="+")
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="avviato da"
    )
    started_at = models.DateTimeField("avviato il", auto_now_add=True)
    closed_at = models.DateTimeField("chiuso il", null=True, blank=True)
    outcome = models.CharField("esito", max_length=20, choices=Outcome, default=Outcome.PENDING)
    message = models.TextField("messaggio ai revisori", max_length=1000, blank=True)
    closing_note = models.CharField("nota di chiusura", max_length=500, blank=True)

    class Meta:
        verbose_name = "giro di revisione"
        verbose_name_plural = "giri di revisione"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "number"], name="uniq_review_round_number"),
            # Al massimo un giro in corso per documento.
            models.UniqueConstraint(
                fields=["document"], condition=Q(outcome="pending"), name="uniq_review_round_pending"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document} · giro {self.number}"

    def prossimo_passo(self):
        """Il passo di turno in un giro in sequenza (il primo non ancora deciso)."""
        return self.steps.filter(decision=ReviewStep.Decision.PENDING).order_by("position").first()


class ReviewStepQuerySet(models.QuerySet):
    def attesi_da(self, user) -> "ReviewStepQuerySet":
        """Passi su cui `user` deve rispondere adesso (giro in corso; in sequenza solo se è il suo turno)."""
        inattesa = self.model.objects.filter(
            round=models.OuterRef("round"),
            decision=ReviewStep.Decision.PENDING,
            position__lt=models.OuterRef("position"),
        )
        return (
            self.filter(
                reviewer=user,
                decision=ReviewStep.Decision.PENDING,
                round__outcome=ReviewRound.Outcome.PENDING,
            )
            .annotate(ha_precedenti=models.Exists(inattesa))
            .filter(Q(round__sequential=False) | Q(ha_precedenti=False))
        )


class ReviewStep(models.Model):
    """Una persona chiamata a rispondere in un giro di revisione."""

    class Decision(models.TextChoices):
        PENDING = "pending", "In attesa"
        APPROVED = "approved", "Approvato"
        REJECTED = "rejected", "Rimandato"

    round = models.ForeignKey(ReviewRound, on_delete=models.CASCADE, related_name="steps")
    position = models.PositiveSmallIntegerField("posizione")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_steps")
    label = models.CharField("ruolo", max_length=80, blank=True)
    decision = models.CharField("decisione", max_length=20, choices=Decision, default=Decision.PENDING)
    decided_at = models.DateTimeField("deciso il", null=True, blank=True)
    note = models.CharField("nota", max_length=500, blank=True)

    objects = ReviewStepQuerySet.as_manager()

    class Meta:
        verbose_name = "passo di revisione"
        verbose_name_plural = "passi di revisione"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["round", "position"], name="uniq_review_step_position"),
            models.UniqueConstraint(fields=["round", "reviewer"], name="uniq_review_step_reviewer"),
        ]

    def __str__(self) -> str:
        return f"{self.round} · {self.reviewer}"


class SavedSearch(models.Model):
    """Una ricerca dell'elenco documenti (testo + filtri) salvata con un nome, per riaprirla con un clic. È personale."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_searches")
    name = models.CharField("nome", max_length=60)
    query = models.CharField("filtri", max_length=500)  # query string già ripulita: solo i parametri noti
    created_at = models.DateTimeField("creata il", auto_now_add=True)

    class Meta:
        verbose_name = "ricerca salvata"
        verbose_name_plural = "ricerche salvate"
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="uniq_saved_search_name")]

    def __str__(self) -> str:
        return self.name

    @property
    def link(self) -> str:
        return "/documenti/?" + self.query if self.query else "/documenti/"
