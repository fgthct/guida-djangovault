from pathlib import Path

from django import forms
from django.conf import settings

from core.forms import TailwindFormMixin

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from .models import Category, Document, DocumentPermission


class DocumentForm(TailwindFormMixin, forms.ModelForm):
    """Metadati del documento (modifica)."""

    class Meta:
        model = Document
        fields = ["title", "category", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


def valida_file(f):
    """Controlli comuni a ogni caricamento: estensione, file vuoto, dimensione massima."""
    estensione = Path(f.name).suffix.lower().lstrip(".")
    if estensione not in settings.DOCUMENTS_ALLOWED_EXTENSIONS:
        raise forms.ValidationError(
            "Tipo di file non consentito. Ammessi: %(tipi)s.",
            params={"tipi": ", ".join(settings.DOCUMENTS_ALLOWED_EXTENSIONS)},
        )
    if f.size == 0:
        raise forms.ValidationError("Il file è vuoto.")
    massimo = settings.DOCUMENTS_MAX_UPLOAD_MB * 1024 * 1024
    if f.size > massimo:
        raise forms.ValidationError(
            "Il file supera il limite di %(mb)s MB.", params={"mb": settings.DOCUMENTS_MAX_UPLOAD_MB}
        )
    return f


class DocumentUploadForm(DocumentForm):
    """Creazione: metadati + primo file."""

    file = forms.FileField(label="File")
    ai_allowed = forms.BooleanField(
        label="Consenti all'assistente IA di leggere questo documento",
        required=False,
        initial=True,
        help_text="Per rispondere alle domande i passaggi pertinenti vengono inviati ad Anthropic; per proporre la categoria "
        "e un riassunto, l'inizio del testo. "
        "Puoi cambiarlo in qualsiasi momento dalla pagina del documento.",
    )

    field_order = ["title", "category", "description", "file", "ai_allowed"]

    def clean_file(self):
        return valida_file(self.cleaned_data["file"])


class VersionUploadForm(TailwindFormMixin, forms.Form):
    """Nuova versione di un documento esistente."""

    file = forms.FileField(label="Nuovo file")
    comment = forms.CharField(
        label="Nota (facoltativa)",
        max_length=255,
        required=False,
        help_text="Cosa è cambiato in questa versione.",
    )

    def clean_file(self):
        return valida_file(self.cleaned_data["file"])


class RestoreForm(TailwindFormMixin, forms.Form):
    comment = forms.CharField(label="Nota (facoltativa)", max_length=255, required=False)


class CategoryForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "Nuova categoria"})}

    def clean_name(self):
        nome = " ".join(self.cleaned_data["name"].split())
        if Category.objects.filter(name__iexact=nome).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Esiste già una categoria con questo nome.")
        return nome


class ShareForm(TailwindFormMixin, forms.Form):
    """Condivisione: un destinatario (utente o gruppo) e un livello."""

    destinatario = forms.ChoiceField(label="Utente o gruppo")
    livello = forms.TypedChoiceField(
        label="Livello",
        choices=DocumentPermission.Level.choices,
        coerce=int,
        initial=DocumentPermission.Level.READ,
    )

    def __init__(self, *args, document: Document, **kwargs):
        super().__init__(*args, **kwargs)
        self.document = document
        utenti = get_user_model().objects.filter(is_active=True).exclude(pk=document.owner_id).order_by("username")
        gruppi = Group.objects.order_by("name")
        self._utenti = {str(u.pk): u for u in utenti}
        self._gruppi = {str(g.pk): g for g in gruppi}
        self.fields["destinatario"].choices = [("", "Scegli…")] + [
            ("Utenti", [(f"u:{u.pk}", u.get_full_name() or u.username) for u in utenti]),
            ("Gruppi", [(f"g:{g.pk}", g.name) for g in gruppi]),
        ]

    def clean_destinatario(self):
        valore = self.cleaned_data["destinatario"]
        tipo, _, pk = valore.partition(":")
        if tipo == "u" and pk in self._utenti:
            self.cleaned_data["_utente"] = self._utenti[pk]
        elif tipo == "g" and pk in self._gruppi:
            self.cleaned_data["_gruppo"] = self._gruppi[pk]
        else:
            raise forms.ValidationError("Scelta non valida.")
        return valore

    @property
    def utente(self):
        return self.cleaned_data.get("_utente")

    @property
    def gruppo(self):
        return self.cleaned_data.get("_gruppo")


class TransitionForm(TailwindFormMixin, forms.Form):
    nota = forms.CharField(label="Nota", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, nota_obbligatoria: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if nota_obbligatoria:
            self.fields["nota"].required = True
            self.fields["nota"].label = "Motivo (obbligatorio)"
        else:
            self.fields["nota"].label = "Nota (facoltativa)"


class ReviewStartForm(forms.Form):
    """Avvio della revisione: righe (revisore + ruolo) nell'ordine scelto e modalità.

    Le righe arrivano come liste parallele `revisore` e `ruolo` (HTML semplice, nessuna libreria):
    le righe senza revisore si ignorano, quindi per «toglierne» una basta lasciarla vuota.
    """

    sequenziale = forms.BooleanField(
        label="Approvazione in sequenza",
        required=False,
        help_text="Spuntata: rispondono uno alla volta, nell'ordine delle righe. "
        "Non spuntata: tutti vengono avvisati subito e rispondono nell'ordine che vogliono.",
    )

    messaggio = forms.CharField(
        label="Messaggio per i revisori",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Facoltativo: cosa devono controllare o cosa ti serve da loro (es. «Controlla il capitolo 3»).",
    )

    def __init__(self, data=None, *, document: Document, iniziale=None):
        super().__init__(data, initial={"sequenziale": bool(iniziale and iniziale["sequenziale"])})
        self.document = document
        utenti = get_user_model().objects.filter(is_active=True).exclude(pk=document.owner_id).order_by("username")
        self.utenti = [(str(u.pk), u.get_full_name() or u.username) for u in utenti]
        self._per_id = {str(u.pk): u for u in utenti}
        self.revisori: list[tuple] = []
        if data is not None:
            ids, ruoli = data.getlist("revisore"), data.getlist("ruolo")
            ruoli += [""] * (len(ids) - len(ruoli))
            self.righe = [{"revisore": i, "ruolo": r} for i, r in zip(ids, ruoli)]
        elif iniziale:
            self.righe = iniziale["righe"]
        else:
            self.righe = []
        while len(self.righe) < 3:
            self.righe.append({"revisore": "", "ruolo": ""})

    def clean(self):
        cleaned = super().clean()
        self.revisori = []
        visti = set()
        for riga in self.righe:
            if not riga["revisore"]:
                continue
            utente = self._per_id.get(riga["revisore"])
            if utente is None:
                raise forms.ValidationError("Una delle persone scelte non è valida.")
            if utente.pk in visti:
                raise forms.ValidationError("Hai scelto due volte la stessa persona.")
            visti.add(utente.pk)
            self.revisori.append((utente, riga["ruolo"].strip()[:80]))
        if not self.revisori:
            raise forms.ValidationError("Scegli almeno una persona.")
        return cleaned
