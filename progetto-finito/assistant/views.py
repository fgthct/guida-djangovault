import json
import logging
import re

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.views import is_htmx
from documents import permissions
from documents.models import Category, Document
from documents.services import pagine

from . import rendering, retrieval, services
from .models import Conversation, Message

logger = logging.getLogger(__name__)


def _conversazione(request: HttpRequest, pk: int) -> Conversation:
    """Le conversazioni sono private: quella di un altro risponde 404, anche a un superutente."""
    return get_object_or_404(Conversation.objects.select_related("document"), pk=pk, owner=request.user)


def _prepara(utente, messaggi: list[Message]) -> list[Message]:
    """Aggiunge a ogni messaggio il testo HTML e le fonti da mostrare.

    Le fonti restano salvate nella conversazione, ma si mostrano solo se l'utente vede ancora il documento: dopo una
    revoca dell'accesso non deve restare leggibile il suo testo.
    """
    ids = {f["document_id"] for m in messaggi for f in (m.sources or [])}
    ids |= {d["id"] for m in messaggi for o in (m.occurrences or []) for d in o["documents"]}
    visibili = set(Document.objects.visible_to(utente).filter(pk__in=ids).values_list("pk", flat=True)) if ids else set()
    for m in messaggi:
        fonti = []
        for f in m.sources or []:
            if f["document_id"] in visibili:
                fonti.append({
                    **f, "url": reverse("documents:detail", args=[f["document_id"]]), "visibile": True,
                    "apri": reverse("assistant:fonte", args=[m.pk, f["n"]]),
                })
            else:
                fonti.append({"n": f["n"], "title": "Documento non più accessibile", "version": "", "text": "", "visibile": False})
        m.fonti_mostrate = fonti
        m.occorrenze_mostrate = []
        for o in m.occurrences or []:
            docs = [{"title": d["title"], "url": reverse("documents:detail", args=[d["id"]])} for d in o["documents"] if d["id"] in visibili]
            if docs:
                m.occorrenze_mostrate.append({"term": o["term"], "docs": docs, "altri": max(o["total"] - len(o["documents"]), 0)})
        if m.role == Message.Role.ASSISTANT and fonti:
            citate = {int(n) for n in re.findall(r"\[(\d{1,2})\]", m.content)}
            m.fonti_citate = [f for f in fonti if f["n"] in citate]
            m.fonti_altre = [f for f in fonti if f["n"] not in citate]
        if m.role == Message.Role.ASSISTANT and m.status in (Message.Status.DONE, Message.Status.ERROR):
            m.html = rendering.risposta_html(m.content, m.pk, {f["n"] for f in fonti})
    return messaggi


def _contesto_base():
    return {"configurato": services.configurato()}


@login_required
@require_GET
def index(request: HttpRequest) -> HttpResponse:
    contesto = _contesto_base() | {
        "conversazioni": Conversation.objects.filter(owner=request.user).select_related("document")[:30],
        "n_documenti": retrieval.documenti_interrogabili(request.user).count(),
        "documento": None,
        "categorie": Category.objects.all(),
    }
    return render(request, "assistant/index.html", contesto)


@login_required
@require_http_methods(["GET", "POST"])
def new(request: HttpRequest) -> HttpResponse:
    """Nuova conversazione, su tutto l'archivio o (con ?documento=N) su un solo documento."""
    sorgente = request.POST if request.method == "POST" else request.GET
    documento = None
    if sorgente.get("documento", "").isdigit():
        documento = get_object_or_404(Document.objects.visible_to(request.user), pk=int(sorgente["documento"]))
    if request.method == "GET":
        if documento is not None and not services.documento_interrogabile(request.user, documento):
            flash.error(request, "Per chiedere a questo documento serve il consenso all'assistente IA.")
            return redirect(documento)
        return render(request, "assistant/nuova.html", _contesto_base() | {"documento": documento, "categorie": Category.objects.all()})
    if not services.configurato():
        flash.error(request, "L'assistente non è configurato.")
        return redirect("assistant:index")
    categoria = None
    if documento is None and request.POST.get("categoria", "").isdigit():
        categoria = Category.objects.filter(pk=int(request.POST["categoria"])).first()
    try:
        conversazione = services.nuova_conversazione(
            utente=request.user, domanda=request.POST.get("domanda", ""), documento=documento, categoria=categoria
        )
    except services.DomandaNonValida as errore:
        return render(
            request, "assistant/nuova.html", _contesto_base() | {"documento": documento, "errore": str(errore),
                                                                  "categorie": Category.objects.all(), "categoria_scelta": categoria,
                                                                  "domanda": request.POST.get("domanda", "")}, status=422,
        )
    return redirect(conversazione)


@login_required
@require_GET
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    conversazione = _conversazione(request, pk)
    messaggi = _prepara(request.user, list(conversazione.messages.all()))
    return render(
        request, "assistant/detail.html",
        _contesto_base() | {"conversazione": conversazione, "messaggi": messaggi,
                            "limite": settings.AI_MAX_QUESTION_CHARS},
    )


@login_required
@require_POST
def ask(request: HttpRequest, pk: int) -> HttpResponse:
    conversazione = _conversazione(request, pk)
    if not services.configurato():
        return HttpResponse("L'assistente non è configurato.", status=503)
    try:
        coppia = services.fai_domanda(conversazione=conversazione, domanda=request.POST.get("domanda", ""))
    except services.DomandaNonValida as errore:
        if not is_htmx(request):
            flash.error(request, str(errore))
            return redirect(conversazione)
        r = render(request, "assistant/_errore.html", {"errore": str(errore)}, status=422)
        r["HX-Retarget"] = "#errore-domanda"
        r["HX-Reswap"] = "innerHTML"
        return r
    if not is_htmx(request):
        return redirect(conversazione)
    return render(request, "assistant/_messaggi.html", {"messaggi": _prepara(request.user, list(coppia))})


@login_required
@require_GET
def message(request: HttpRequest, pk: int) -> HttpResponse:
    """Frammento di un messaggio nel suo stato attuale (lo chiede il JavaScript a fine flusso, e il polling)."""
    m = get_object_or_404(Message.objects.select_related("conversation"), pk=pk, conversation__owner=request.user)
    if m.role != Message.Role.ASSISTANT:
        raise Http404
    services._scadute()
    m.refresh_from_db()
    return render(request, "assistant/_messaggi.html", {"messaggi": _prepara(request.user, [m])})


def _evento(nome: str, dati) -> str:
    return f"event: {nome}\ndata: {json.dumps(dati, ensure_ascii=False)}\n\n"


async def _asincrono(iteratore):
    """Espone un generatore sincrono come asincrono: ogni passo gira in un thread, senza bloccare il server."""
    fine = object()
    try:
        while True:
            elemento = await sync_to_async(next)(iteratore, fine)
            if elemento is fine:
                return
            yield elemento
    finally:
        await sync_to_async(iteratore.close)()  # browser chiuso a metà: il generatore può ripulire


@login_required
@require_GET
def stream(request: HttpRequest, pk: int) -> StreamingHttpResponse:
    """Flusso SSE della risposta. La generazione parte qui e si salva a fine lavoro."""
    m = get_object_or_404(Message, pk=pk, conversation__owner=request.user, role=Message.Role.ASSISTANT)

    def eventi():
        for tipo, valore in services.genera(m.pk, request.user):
            yield _evento(tipo, valore)

    # Sotto ASGI (Daphne) un iteratore sincrono verrebbe consumato per intero prima di inviare qualcosa: il flusso
    # arriverebbe tutto insieme alla fine. Un iteratore asincrono invece parte subito, un pezzo alla volta.
    risposta = StreamingHttpResponse(_asincrono(eventi()), content_type="text/event-stream")
    risposta["Cache-Control"] = "no-cache"
    risposta["X-Accel-Buffering"] = "no"  # Nginx (Fase 10) non deve accumulare il flusso
    return risposta


@login_required
@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    conversazione = _conversazione(request, pk)
    conversazione.delete()
    flash.success(request, "Conversazione eliminata.")
    return redirect("assistant:index")


@login_required
@require_http_methods(["GET"])
def fonte(request: HttpRequest, pk: int, n: int) -> HttpResponse:
    """Apre il documento di una fonte della risposta, alla pagina del PDF in cui si trova il passaggio (se la si trova)."""
    msg = get_object_or_404(Message, pk=pk, conversation__owner=request.user, role=Message.Role.ASSISTANT)
    f = next((f for f in (msg.sources or []) if f.get("n") == n), None)
    if f is None:
        raise Http404("Fonte non trovata")
    documento = get_object_or_404(Document.objects.visible_to(request.user), pk=f["document_id"])
    versione = documento.versions.filter(version_number=f["version"]).first()
    if versione is None or not permissions.can_download(request.user, documento):
        return redirect(documento)
    pagina = pagine.pagina_del_passaggio(versione, f.get("text", ""))
    nome = versione.original_name.lower()
    if not nome.endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return redirect(documento)
    url = reverse("documents:version_open", args=[documento.pk, versione.version_number])
    return redirect(f"{url}#page={pagina}" if pagina else url)
