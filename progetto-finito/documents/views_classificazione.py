"""Pagina «Da smistare»: le proposte di categoria da rivedere e quelle applicate in automatico (annullabili)."""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.views import is_htmx

from . import permissions
from .models import Category, ClassificationSuggestion, Document
from .services import classificazione

S = ClassificationSuggestion.Status
RECENTI_GIORNI = 30


def _proposta(request: HttpRequest, pk: int) -> ClassificationSuggestion:
    """Una proposta su un documento che l'utente può modificare; altrimenti 404/403 (non rivela l'esistenza)."""
    sug = get_object_or_404(ClassificationSuggestion.objects.select_related("document", "category"), pk=pk)
    if not Document.objects.visible_to(request.user).filter(pk=sug.document_id).exists():
        raise PermissionDenied
    if not permissions.can_edit(request.user, sug.document):
        raise PermissionDenied
    return sug


def _riga(request: HttpRequest, sug: ClassificationSuggestion) -> HttpResponse:
    """Dopo un'azione: con HTMX la riga sparisce o cambia, senza HTMX si torna alla pagina."""
    if is_htmx(request):
        if sug.status in (S.PENDING, S.AUTO):
            return render(request, "documents/_proposta.html", {"p": sug, "categorie": Category.objects.order_by("name")})
        return HttpResponse("")
    return redirect("documents:smistamento")


@login_required
@require_http_methods(["GET"])
def smistamento(request: HttpRequest) -> HttpResponse:
    da_rivedere = list(classificazione.da_smistare(request.user).order_by("-confidence", "-updated_at")[:200])
    recenti = list(
        ClassificationSuggestion.objects.filter(
            status=S.AUTO, document__owner=request.user, decided_at__gte=timezone.now() - timedelta(days=RECENTI_GIORNI)
        ).select_related("document", "category").order_by("-decided_at")[:50]
    )
    return render(request, "documents/smistamento.html", {
        "proposte": da_rivedere,
        "automatiche": recenti,
        "in_attesa": ClassificationSuggestion.objects.filter(status=S.REQUESTED, document__owner=request.user).count(),
        "categorie": Category.objects.order_by("name"),
        "soglia": round(settings.CLASSIFICAZIONE_SOGLIA_AUTO * 100),
        "recenti_giorni": RECENTI_GIORNI,
    })


@login_required
@require_POST
def proposta_accetta(request: HttpRequest, pk: int) -> HttpResponse:
    sug = _proposta(request, pk)
    if sug.status != S.PENDING:
        return _riga(request, sug)
    scelta = request.POST.get("categoria", "")
    categoria = Category.objects.filter(pk=int(scelta)).first() if scelta.isdigit() else None
    try:
        classificazione.applica_proposta(sug, user=request.user, categoria=categoria)
    except ValueError:
        messages.error(request, "Scegli una categoria.")
        return redirect("documents:smistamento")
    sug.refresh_from_db()
    return _riga(request, sug)


@login_required
@require_POST
def proposta_rifiuta(request: HttpRequest, pk: int) -> HttpResponse:
    sug = _proposta(request, pk)
    if sug.status == S.PENDING:
        classificazione.rifiuta_proposta(sug, user=request.user)
    return _riga(request, sug)


@login_required
@require_POST
def proposta_annulla(request: HttpRequest, pk: int) -> HttpResponse:
    sug = _proposta(request, pk)
    if sug.status == S.AUTO and not classificazione.annulla_applicazione(sug, user=request.user):
        messages.warning(request, "La categoria è stata cambiata nel frattempo: non la tocco.")
    sug.refresh_from_db()
    return _riga(request, sug)


@login_required
@require_POST
def proposte_accetta_tutte(request: HttpRequest) -> HttpResponse:
    """Accetta in blocco le proposte con una categoria già esistente e fiducia almeno pari al valore indicato."""
    try:
        minimo = max(0.0, min(1.0, float(request.POST.get("minimo", "0.6"))))
    except ValueError:
        minimo = 0.6
    n = 0
    for sug in classificazione.da_smistare(request.user).filter(category__isnull=False, confidence__gte=minimo):
        if permissions.can_edit(request.user, sug.document):
            classificazione.applica_proposta(sug, user=request.user)
            n += 1
    messages.success(request, f"Accettate {n} proposte." if n else "Nessuna proposta con questa fiducia.")
    return redirect("documents:smistamento")


@login_required
@require_POST
def document_classifica(request: HttpRequest, pk: int) -> HttpResponse:
    """Pulsante «Suggerisci categoria» nel documento."""
    documento = get_object_or_404(Document.objects.visible_to(request.user), pk=pk)
    if not permissions.can_edit(request.user, documento):
        raise PermissionDenied
    if documento.category_id:
        messages.info(request, "Il documento ha già una categoria.")
        return redirect(documento)
    classificazione.richiedi(documento)
    v = documento.current_version
    if v is not None and v.indexed_model:
        classificazione._invia(documento.pk)
        messages.success(request, "Richiesta inviata: la proposta comparirà in «Da smistare» fra pochi istanti.")
    else:
        messages.success(request, "Il documento è ancora in elaborazione: la proposta arriverà appena finisce.")
    return redirect(reverse("documents:detail", args=[documento.pk]))
