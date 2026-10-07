import logging
import os
import tempfile
import zipfile

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, ProtectedError, Q
from django.http import QueryDict
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_http_methods, require_POST

from core.services import audit
from core.views import is_htmx

from . import permissions
from .embeddings import ModelloNonDisponibile
from .forms import CategoryForm, DocumentForm, DocumentUploadForm, RestoreForm, ReviewStartForm, ShareForm, TransitionForm, VersionUploadForm
from .models import Category, ClassificationSuggestion, Document, DocumentPermission, DocumentVersion, ReviewRound, ReviewStep, SavedSearch
from .services import workflow
from .services import ai_consent, indexing, ingestione, processing, search, simili, upload_web
from .services import (
    CondivisioneNonValida,
    DocumentoBloccato,
    TransizioneNonValida,
    VersioneNonValida,
    add_version,
    change_status,
    decide_review,
    start_review,
    withdraw_review,
    create_document,
    delete_document,
    restore_version,
    revoke_share,
    share_document,
    update_document,
)

logger = logging.getLogger(__name__)
PER_PAGINA = 50
PER_PAGINA_SCELTE = (25, 50, 100)
IMMAGINI_REGEX = r"\.(png|jpe?g|gif|webp)$"


def dati_post(request: HttpRequest):
    """Dati del form se la richiesta è un POST (anche vuoto: un POST vuoto è un form da validare)."""
    return request.POST if request.method == "POST" else None


def _se_bloccato(request: HttpRequest, documento: Document):
    """Se il documento non è in bozza avvisa e rimanda al dettaglio (il servizio lo impedisce comunque)."""
    try:
        workflow.assicura_modificabile(documento)
    except DocumentoBloccato as errore:
        messages.error(request, str(errore))
        return redirect(documento)
    return None


def _documento(request: HttpRequest, pk: int, *, con_testo: bool = False) -> Document:
    """Carica un documento visibile all'utente; in caso contrario 404 (non rivela che esiste).

    Il testo estratto può pesare megabyte: lo si carica solo dove serve (`con_testo`).
    """
    qs = Document.objects.visible_to(request.user).select_related("category", "owner", "current_version")
    if not con_testo:
        qs = qs.defer("current_version__extracted_text")
    return get_object_or_404(qs, pk=pk)


# --------------------------------------------------------------------------
# Elenco e ricerca
# --------------------------------------------------------------------------

@login_required
@require_http_methods(["GET"])
def document_list(request: HttpRequest) -> HttpResponse:
    q = search.normalizza(request.GET.get("q", ""))
    categoria = request.GET.get("categoria", "")
    scope = request.GET.get("scope", "")
    stato = request.GET.get("stato", "")
    modo = request.GET.get("modo", "")
    if modo not in search.MODI:
        modo = search.MODO_PREDEFINITO

    qs = (
        Document.objects.visible_to(request.user)
        .select_related("category", "owner", "current_version")
        .defer("current_version__extracted_text")
    )
    tipo = request.GET.get("tipo", "")
    if tipo == "documenti":
        qs = qs.exclude(current_version__original_name__iregex=IMMAGINI_REGEX)
    elif tipo == "immagini":
        qs = qs.filter(current_version__original_name__iregex=IMMAGINI_REGEX)
    if categoria.isdigit():
        qs = qs.filter(category_id=int(categoria))
    elif categoria == "nessuna":
        qs = qs.filter(category__isnull=True)
    if stato in Document.Status.values:
        qs = qs.filter(status=stato)
    if scope == "miei":
        qs = qs.filter(owner=request.user)
    elif scope == "condivisi":
        qs = qs.exclude(owner=request.user)

    # I filtri sono già applicati: la ricerca lavora solo su ciò che l'utente può vedere E ha chiesto di vedere.
    avviso = ""
    passaggi: dict[int, str] = {}
    non_indicizzati = 0
    if q:
        if modo == "parole":
            qs = search.cerca_parole(qs, q)
        else:
            try:
                semantici = search.cerca_significato(qs, q)
            except ModelloNonDisponibile:
                logger.exception("Modello di ricerca per significato non disponibile")
                avviso = "La ricerca per significato non è al momento disponibile: mostro i risultati per parole."
                modo = "parole"
                qs = search.cerca_parole(qs, q)
            else:
                passaggi = {r.document_id: r.testo for r in semantici}
                if modo == "significato":
                    ordine = [r.document_id for r in semantici]
                else:
                    ordine = search.fondi(search.candidati_parole(qs, q), semantici)
                non_indicizzati = search.non_indicizzati(qs)
                qs = search.in_ordine(qs, ordine)

    per = int(request.GET["per"]) if request.GET.get("per", "").isdigit() and int(request.GET["per"]) in PER_PAGINA_SCELTE else PER_PAGINA
    pagina = Paginator(qs, per).get_page(request.GET.get("page"))
    in_elaborazione = Document.objects.visible_to(request.user).filter(
        current_version__processing_status__in=[DocumentVersion.Processing.PENDING, DocumentVersion.Processing.RUNNING]
    ).count()
    permissions.annota_livelli(request.user, pagina.object_list)
    if q:
        documenti_pagina = list(pagina.object_list)
        search.aggiungi_estratti(documenti_pagina, q)
        for d in documenti_pagina:
            if not d.estratto and d.pk in passaggi:
                d.estratto = search.estratto_semantico(passaggi[d.pk])
    # Per i link di paginazione: gli stessi filtri senza `page`.
    filtri = request.GET.copy()
    filtri.pop("page", None)
    senza_per = filtri.copy()
    senza_per.pop("per", None)
    def _link(**cambia):
        """Query string con questi filtri cambiati (e senza pagina): per le schede delle categorie e del tipo."""
        q_ = filtri.copy()
        for k, v in cambia.items():
            if v:
                q_[k] = v
            else:
                q_.pop(k, None)
        return "?" + q_.urlencode() if q_ else "?"

    base = Document.objects.visible_to(request.user)
    per_categoria = dict(base.order_by().values_list("category_id").annotate(n=Count("pk")))
    schede = [{"nome": "Tutte", "n": sum(per_categoria.values()), "link": _link(categoria=""), "attiva": not categoria}]
    for c in Category.objects.all():
        if per_categoria.get(c.pk):
            schede.append({"nome": c.name, "n": per_categoria[c.pk], "link": _link(categoria=str(c.pk)), "attiva": categoria == str(c.pk)})
    if per_categoria.get(None):
        schede.append({"nome": "Senza categoria", "n": per_categoria[None], "link": _link(categoria="nessuna"), "attiva": categoria == "nessuna"})
    tipi = [("", "Tutti"), ("documenti", "Senza immagini"), ("immagini", "Solo immagini")]
    contesto = {
        "schede": schede,
        "tipo": tipo,
        "tipi": [{"nome": nome, "link": _link(tipo=valore), "attivo": tipo == valore} for valore, nome in tipi],
        "filtri_senza_per": senza_per.urlencode(),
        "pagina": pagina,
        "q": q,
        "modo": modo,
        "modi": search.MODI,
        "avviso": avviso,
        "non_indicizzati": non_indicizzati,
        "per": per,
        "per_scelte": PER_PAGINA_SCELTE,
        "in_elaborazione": in_elaborazione,
        "da_smistare": ClassificationSuggestion.objects.filter(status=ClassificationSuggestion.Status.PENDING, document__owner=request.user).count(),
        "categoria": categoria,
        "scope": scope,
        "stato": stato,
        "stati": Document.Status.choices,
        "categorie": Category.objects.all(),
        "ricerche_salvate": list(SavedSearch.objects.filter(owner=request.user)),
        "filtri": filtri.urlencode(),
    }
    # HTMX riceve solo il frammento; il ripristino della cronologia del browser vuole la pagina intera.
    solo_frammento = is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
    risposta = render(request, "documents/list.html#risultati" if solo_frammento else "documents/list.html", contesto)
    patch_vary_headers(risposta, ["HX-Request", "HX-History-Restore-Request"])
    return risposta


FILTRI_SALVABILI = ("q", "categoria", "scope", "stato", "modo", "tipo")
MASSIMO_RICERCHE = 20


def _query_pulita(grezza: str) -> str:
    """Tiene solo i filtri dell'elenco, senza pagina e senza valori vuoti (non si salva «a pagina 3»)."""
    dati = QueryDict(grezza.lstrip("?"), mutable=False)
    pulita = QueryDict(mutable=True)
    for chiave in FILTRI_SALVABILI:
        valore = dati.get(chiave, "").strip()
        if valore:
            pulita[chiave] = valore[:200]
    return pulita.urlencode()


@login_required
@require_POST
def ricerca_salva(request: HttpRequest) -> HttpResponse:
    nome = " ".join(request.POST.get("nome", "").split())[:60]
    query = _query_pulita(request.POST.get("filtri", ""))
    if not nome:
        messages.error(request, "Dai un nome alla ricerca.")
    elif not query:
        messages.error(request, "Non c'è nessun filtro da salvare: scrivi qualcosa o scegli una categoria.")
    else:
        proprie = SavedSearch.objects.filter(owner=request.user)
        esistente = proprie.filter(name__iexact=nome).first()
        if esistente is None and proprie.count() >= MASSIMO_RICERCHE:
            messages.error(request, f"Puoi salvare al massimo {MASSIMO_RICERCHE} ricerche: eliminane una.")
        elif esistente is not None:
            esistente.query = query
            esistente.save(update_fields=["query"])
            messages.success(request, f"Ricerca «{esistente.name}» aggiornata.")
        else:
            SavedSearch.objects.create(owner=request.user, name=nome, query=query)
            messages.success(request, f"Ricerca «{nome}» salvata.")
    return redirect(reverse("documents:list") + ("?" + query if query else ""))


@login_required
@require_POST
def ricerca_elimina(request: HttpRequest, pk: int) -> HttpResponse:
    ricerca = get_object_or_404(SavedSearch, pk=pk, owner=request.user)
    ricerca.delete()
    messages.success(request, f"Ricerca «{ricerca.name}» eliminata.")
    return redirect("documents:list")


RICERCA_RAPIDA_MIN_SIGNIFICATO = 3  # sotto questa lunghezza la ricerca per significato non ha senso e costa


@login_required
@require_http_methods(["GET"])
def ricerca_rapida(request: HttpRequest) -> JsonResponse:
    """Per la palette Ctrl+K: i documenti visibili più pertinenti, per parole e per significato fusi insieme (al massimo 8).

    Se scrivi poco, o il modello di ricerca per significato non è disponibile, si ripiega in silenzio sulle sole parole.
    Senza testo si mostrano gli ultimi documenti toccati.
    """
    q = search.normalizza(request.GET.get("q", ""))[:100]
    qs = Document.objects.visible_to(request.user).select_related("category").defer("current_version__extracted_text")
    if len(q) < 2:
        qs = qs.order_by("-updated_at", "-id")
    else:
        semantici = []
        if len(q) >= RICERCA_RAPIDA_MIN_SIGNIFICATO:
            try:
                semantici = search.cerca_significato(qs, q)
            except Exception:  # ripiego silenzioso: la palette deve restare veloce e non rompersi mai
                logger.warning("Ctrl+K: ricerca per significato non disponibile, uso le parole", exc_info=True)
        if semantici:
            qs = search.in_ordine(qs, search.fondi(search.candidati_parole(qs, q), semantici))
        else:
            qs = search.cerca_parole(qs, q)
    risultati = [
        {"id": d.pk, "titolo": d.title, "categoria": d.category.name if d.category else "", "url": d.get_absolute_url()}
        for d in qs[:8]
    ]
    return JsonResponse({"q": q, "risultati": risultati})


# --------------------------------------------------------------------------
# Dettaglio, creazione, modifica, eliminazione
# --------------------------------------------------------------------------

@login_required
@require_http_methods(["GET"])
def document_detail(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk, con_testo=True)
    versioni = documento.versions.select_related("created_by", "restored_from").annotate(n_passaggi=Count("chunks")).defer(
        "extracted_text", "restored_from__extracted_text"  # testo da megabyte: la cronologia non lo usa
    )
    bozza = documento.status == Document.Status.DRAFT
    giri = list(documento.review_rounds.prefetch_related("steps__reviewer")[:6])
    giro = giri[0] if giri else None
    in_corso = giro is not None and giro.outcome == ReviewRound.Outcome.PENDING
    mio_passo = mio_turno = False
    if in_corso:
        turno = giro.prossimo_passo()
        for p in giro.steps.all():
            p.stato, p.tono = _stato_passo(p, giro, turno)
            if p.reviewer_id == request.user.id and p.decision == ReviewStep.Decision.PENDING:
                mio_passo = True
                mio_turno = not giro.sequential or turno.pk == p.pk
    elif giro is not None:
        for p in giro.steps.all():
            p.stato, p.tono = _stato_passo(p, giro, None)
    nessuna_proposta = None
    if not documento.category_id:
        nessuna_proposta = ClassificationSuggestion.objects.filter(
            document=documento, status=ClassificationSuggestion.Status.NONE
        ).values_list("reason", flat=True).first()
    return render(
        request,
        "documents/detail.html",
        {
            "documento": documento,
            "nessuna_proposta": nessuna_proposta,
            "simili": simili.documenti_simili(request.user, documento),
            "versione": documento.current_version,
            "versioni": versioni,
            "livello": permissions.livello(request.user, documento),
            "puo_modificare": bozza and permissions.can_edit(request.user, documento),
            "puo_rielaborare": permissions.can_reprocess(request.user, documento),
            "bloccato": not bozza and permissions.can_edit(request.user, documento),
            "puo_amministrare": permissions.can_share(request.user, documento),
            "assistente_attivo": bool(settings.ANTHROPIC_API_KEY),
            "transizioni": workflow.transizioni_disponibili(request.user, documento),
            "giro": giro,
            "giro_in_corso": in_corso,
            "giri_precedenti": giri[1:],
            "mio_passo": mio_passo,
            "mio_turno": mio_turno,
            "puo_inviare": bozza and permissions.can_submit_review(request.user, documento),
            "puo_ritirare": in_corso and permissions.can_submit_review(request.user, documento),
        },
    )


def _stato_passo(passo: ReviewStep, giro: ReviewRound, turno) -> tuple[str, str]:
    """Testo e tono del badge di un passo nel giro mostrato."""
    if passo.decision == ReviewStep.Decision.APPROVED:
        return "Approvato", "ok"
    if passo.decision == ReviewStep.Decision.REJECTED:
        return "Rimandato", "error"
    if giro.outcome != ReviewRound.Outcome.PENDING:
        return "Non risposto", "neutral"
    if giro.sequential and turno is not None and turno.pk != passo.pk:
        return "Attende il turno", "neutral"
    return ("Di turno" if giro.sequential else "In attesa"), "warning"


@login_required
@require_http_methods(["GET", "POST"])
def document_create(request: HttpRequest) -> HttpResponse:
    form = DocumentUploadForm(dati_post(request), request.FILES if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        risultato = create_document(
            owner=request.user,
            title=form.cleaned_data["title"],
            description=form.cleaned_data["description"],
            category=form.cleaned_data["category"],
            uploaded_file=form.cleaned_data["file"],
            ai_allowed=form.cleaned_data["ai_allowed"],
        )
        messages.success(request, "Documento caricato.")
        if risultato.duplicates:
            titoli = ", ".join(f"«{d.title}»" for d in risultato.duplicates)
            messages.warning(request, f"Attenzione: un file identico esiste già in {titoli}.")
        return redirect(risultato.document)
    stato = 422 if request.method == "POST" else 200
    return render(request, "documents/form.html", {"form": form, "nuovo": True}, status=stato)


@login_required
@require_http_methods(["GET"])
def document_upload(request: HttpRequest) -> HttpResponse:
    """Pagina «Carica più file»: trascinamento di file e cartelle, avanzamento per file (il lavoro lo fa static/js/carica.js)."""
    return render(request, "documents/upload.html", {
        "categorie": Category.objects.order_by("name"),
        "estensioni": ", ".join(settings.DOCUMENTS_ALLOWED_EXTENSIONS),
        "estensioni_lista": ",".join(settings.DOCUMENTS_ALLOWED_EXTENSIONS),
        "massimo_mb": settings.DOCUMENTS_MAX_UPLOAD_MB,
        "posta_attiva": ingestione.posta_configurata(),
        "posta_indirizzo": settings.INGESTIONE_IMAP_UTENTE,
    })


@login_required
def document_scan(request: HttpRequest) -> HttpResponse:
    """Scansione con la fotocamera: foto delle pagine → un PDF, che poi passa dal caricamento normale (il lavoro lo fa
    static/js/scansiona.js; il testo lo ricava l'OCR sul server)."""
    return render(request, "documents/scan.html", {
        "categorie": Category.objects.order_by("name"),
        "massimo_mb": settings.DOCUMENTS_MAX_UPLOAD_MB,
    })


@login_required
@require_POST
def document_upload_file(request: HttpRequest) -> JsonResponse:
    """Riceve UN file e risponde in JSON con l'esito (importato / presente / saltato / errore)."""
    caricato = request.FILES.get("file")
    if caricato is None:
        return JsonResponse({"stato": "errore", "dettaglio": "Nessun file ricevuto."}, status=400)
    esito = upload_web.carica_file(
        uploaded=caricato,
        owner=request.user,
        scelta_categoria=request.POST.get("categoria", upload_web.CAT_NESSUNA),
        percorso_relativo=request.POST.get("percorso", ""),
        ai_allowed=request.POST.get("ai_allowed") == "1",
    )
    return JsonResponse(esito)


@login_required
@require_http_methods(["GET", "POST"])
def document_edit(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_edit(request.user, documento):
        raise PermissionDenied
    if (bloccato := _se_bloccato(request, documento)) is not None:
        return bloccato
    form = DocumentForm(dati_post(request), instance=documento)
    if request.method == "POST" and form.is_valid():
        try:
            update_document(
                user=request.user,
                document=documento,
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
                category=form.cleaned_data["category"],
            )
        except DocumentoBloccato as errore:
            messages.error(request, str(errore))
            return redirect(documento)
        messages.success(request, "Modifiche salvate.")
        return redirect(documento)
    stato = 422 if request.method == "POST" else 200
    return render(request, "documents/form.html", {"form": form, "documento": documento, "nuovo": False}, status=stato)


@login_required
@require_POST
def document_ai_consent(request: HttpRequest, pk: int) -> HttpResponse:
    """Consenso all'assistente IA: lo decide chi amministra il documento, in qualunque stato."""
    documento = _documento(request, pk)
    if not permissions.can_share(request.user, documento):
        raise PermissionDenied
    consentito = request.POST.get("consenti") == "1"
    if ai_consent.set_ai_consent(user=request.user, document=documento, allowed=consentito):
        messages.success(
            request,
            "L'assistente IA può ora leggere questo documento." if consentito else "L'assistente IA non leggerà più questo documento.",
        )
    return redirect(documento)


@login_required
@require_http_methods(["GET", "POST", "DELETE"])
def document_delete(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_delete(request.user, documento):
        raise PermissionDenied
    if request.method == "GET":
        return render(request, "documents/confirm_delete.html", {"documento": documento})
    delete_document(user=request.user, document=documento)
    if request.method == "DELETE" and is_htmx(request):
        # HTMX toglie la riga dall'elenco (hx-swap="outerHTML" con risposta vuota).
        return HttpResponse("")
    messages.success(request, "Documento eliminato.")
    return redirect("documents:list")


# --------------------------------------------------------------------------
# Download protetto
# --------------------------------------------------------------------------

def _risposta_file(request: HttpRequest, documento: Document, versione, *, in_pagina: bool = False) -> HttpResponse:
    try:
        handle = versione.file.open("rb")
    except FileNotFoundError:
        logger.error("File mancante su disco: documento %s, versione %s", documento.pk, versione.pk)
        raise Http404("File non trovato") from None
    risposta = FileResponse(
        handle,
        as_attachment=not in_pagina,
        filename=versione.original_name,
        content_type=versione.content_type or "application/octet-stream",
    )
    risposta["X-Content-Type-Options"] = "nosniff"
    risposta["Cache-Control"] = "private, no-store"
    audit.record(
        "document.opened" if in_pagina else "document.downloaded", user=request.user, obj=documento,
        versione=versione.version_number, file=versione.original_name,
    )
    return risposta


@login_required
@require_http_methods(["GET"])
def document_download(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_download(request.user, documento):
        raise PermissionDenied
    if documento.current_version is None:
        raise Http404("Nessun file")
    return _risposta_file(request, documento, documento.current_version)


# --------------------------------------------------------------------------
# Azioni su più documenti (selezione multipla nell'elenco)
# --------------------------------------------------------------------------

MASSIMO_SELEZIONE = 100


def _selezione(request: HttpRequest) -> list[Document]:
    """I documenti scelti nell'elenco, solo se visibili all'utente (gli altri sono ignorati come se non esistessero)."""
    ids = [int(x) for x in request.POST.getlist("scelti") if x.isdigit()][:MASSIMO_SELEZIONE]
    qs = Document.objects.visible_to(request.user).select_related("category", "current_version").defer("current_version__extracted_text")
    return list(qs.filter(pk__in=ids).order_by("pk"))


@login_required
@require_POST
def azione_multipla(request: HttpRequest) -> HttpResponse:
    """Assegna una categoria o scarica uno ZIP. Niente eliminazioni di massa: troppo facili da fare per sbaglio."""
    ritorno = request.POST.get("ritorno", "")
    destinazione = ritorno if url_has_allowed_host_and_scheme(ritorno, allowed_hosts={request.get_host()}, require_https=request.is_secure()) and ritorno.startswith("/") else reverse("documents:list")
    documenti = _selezione(request)
    if not documenti:
        messages.error(request, "Non hai scelto nessun documento.")
        return redirect(destinazione)
    azione = request.POST.get("azione", "")

    if azione == "categoria":
        scelta = request.POST.get("categoria", "")
        categoria = None
        if scelta.isdigit():
            categoria = Category.objects.filter(pk=int(scelta)).first()
            if categoria is None:
                messages.error(request, "Categoria non trovata.")
                return redirect(destinazione)
        elif scelta != "nessuna":
            messages.error(request, "Scegli una categoria.")
            return redirect(destinazione)
        fatti = saltati = 0
        for d in documenti:
            if d.category_id == (categoria.pk if categoria else None):
                continue
            try:
                update_document(user=request.user, document=d, title=d.title, description=d.description, category=categoria)
                fatti += 1
            except (PermissionDenied, DocumentoBloccato):
                saltati += 1
        testo = f"{fatti} document{'o' if fatti == 1 else 'i'} {'spostato' if fatti == 1 else 'spostati'} in «{categoria.name if categoria else 'Senza categoria'}»."
        if saltati:
            testo += f" {saltati} non modificabili (non in bozza o senza permesso)."
        (messages.success if fatti else messages.warning)(request, testo)
        return redirect(destinazione)

    if azione == "zip":
        scaricabili = [d for d in documenti if d.current_version is not None and permissions.can_download(request.user, d)]
        if not scaricabili:
            messages.error(request, "Nessuno dei documenti scelti si può scaricare.")
            return redirect(destinazione)
        archivio = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        with zipfile.ZipFile(archivio, "w", zipfile.ZIP_DEFLATED) as z:
            for d in scaricabili:
                v = d.current_version
                try:
                    with v.file.open("rb") as f:
                        # Il numero del documento davanti: due file con lo stesso nome non si sovrascrivono.
                        z.writestr(f"{d.pk}-{os.path.basename(v.original_name) or 'file'}", f.read())
                except FileNotFoundError:
                    logger.error("File mancante su disco: documento %s, versione %s", d.pk, v.pk)
                    continue
                audit.record("document.downloaded", user=request.user, obj=d, versione=v.version_number, file=v.original_name, zip=True)
        archivio.seek(0)
        risposta = FileResponse(archivio, as_attachment=True, filename="documenti.zip", content_type="application/zip")
        risposta["X-Content-Type-Options"] = "nosniff"
        risposta["Cache-Control"] = "private, no-store"
        return risposta

    messages.error(request, "Azione non riconosciuta.")
    return redirect(destinazione)


# --------------------------------------------------------------------------
# Versioni
# --------------------------------------------------------------------------

def _versione(documento: Document, numero: int):
    return get_object_or_404(
        documento.versions.select_related("created_by").annotate(n_passaggi=Count("chunks")), version_number=numero
    )


@login_required
@require_http_methods(["GET"])
def version_download(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_download(request.user, documento):
        raise PermissionDenied
    return _risposta_file(request, documento, _versione(documento, numero))


@login_required
@require_http_methods(["GET"])
def version_open(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    """Apre la versione nel browser (PDF e immagini; il resto si scarica). `#page=N` lo gestisce il lettore PDF."""
    documento = _documento(request, pk)
    if not permissions.can_download(request.user, documento):
        raise PermissionDenied
    versione = _versione(documento, numero)
    nome = versione.original_name.lower()
    apribile = nome.endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"))
    risposta = _risposta_file(request, documento, versione, in_pagina=apribile)
    if apribile:
        # Il lettore PDF del browser ha bisogno di object-src: la CSP predefinita lo vieta. Qui è una risposta con solo il file.
        risposta["Content-Security-Policy"] = (
            "default-src 'none'; object-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        risposta["Content-Disposition"] = risposta["Content-Disposition"].replace("attachment", "inline", 1)
    return risposta


def _contesto_elaborazione(request: HttpRequest, documento: Document, versione) -> dict:
    return {"v": versione, "documento": documento, "puo_rielaborare": permissions.can_reprocess(request.user, documento)}


@login_required
@require_http_methods(["GET"])
def version_processing(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    """Frammento HTMX con lo stato dell'elaborazione di una versione (interrogato finché non è finita)."""
    documento = _documento(request, pk)
    versione = _versione(documento, numero)
    if not is_htmx(request):
        return redirect(documento)
    risposta = render(request, "components/elaborazione.html", _contesto_elaborazione(request, documento, versione))
    if not versione.elaborazione_in_corso and request.GET.get("fase") != "indice":
        # Elaborazione finita mentre la pagina era aperta: si ricarica la pagina per mostrare anteprima e testo
        # estratto (il polling di questa versione si è già fermato, quindi il ricaricamento avviene una volta sola).
        risposta["HX-Refresh"] = "true"
    return risposta


@login_required
@require_POST
def version_reprocess(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_reprocess(request.user, documento):
        raise PermissionDenied
    versione = _versione(documento, numero)
    try:
        versione = processing.richiedi_rielaborazione(user=request.user, version=versione)
    except processing.ElaborazioneInCorso as errore:
        if is_htmx(request):
            versione.refresh_from_db()
            return render(request, "components/elaborazione.html", _contesto_elaborazione(request, documento, versione), status=409)
        messages.error(request, str(errore))
        return redirect(documento)
    if is_htmx(request):
        return render(request, "components/elaborazione.html", _contesto_elaborazione(request, documento, versione))
    messages.success(request, f"Versione {versione.version_number} rimessa in coda.")
    return redirect(documento)


@login_required
@require_POST
def version_reindex(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_reprocess(request.user, documento):
        raise PermissionDenied
    versione = _versione(documento, numero)
    try:
        indexing.richiedi_indicizzazione(user=request.user, version=versione)
    except indexing.IndicizzazioneNonPossibile as errore:
        if is_htmx(request):
            return render(request, "components/elaborazione.html", _contesto_elaborazione(request, documento, _versione(documento, numero)), status=409)
        messages.error(request, str(errore))
        return redirect(documento)
    versione = _versione(documento, numero)
    if is_htmx(request):
        return render(request, "components/elaborazione.html", _contesto_elaborazione(request, documento, versione))
    messages.success(request, f"Versione {versione.version_number}: indicizzazione rimessa in coda.")
    return redirect(documento)


@login_required
@require_http_methods(["GET"])
def version_preview(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    """Anteprima PNG di una versione: passa dal controllo dei permessi come il download del file."""
    documento = _documento(request, pk)
    if not permissions.can_download(request.user, documento):
        raise PermissionDenied
    versione = _versione(documento, numero)
    if not versione.preview:
        raise Http404("Nessuna anteprima")
    risposta = FileResponse(versione.preview.open("rb"), content_type="image/png")
    risposta["Cache-Control"] = "private, max-age=3600"  # l'URL cambia quando l'anteprima viene rigenerata
    return risposta


@login_required
@require_http_methods(["GET", "POST"])
def version_create(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_add_version(request.user, documento):
        raise PermissionDenied
    if (bloccato := _se_bloccato(request, documento)) is not None:
        return bloccato
    form = VersionUploadForm(dati_post(request), request.FILES if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            nuova = add_version(
                user=request.user,
                document=documento,
                uploaded_file=form.cleaned_data["file"],
                comment=form.cleaned_data["comment"],
            )
        except DocumentoBloccato as errore:
            messages.error(request, str(errore))
            return redirect(documento)
        except VersioneNonValida as errore:
            form.add_error("file", str(errore))
        else:
            messages.success(request, f"Caricata la versione {nuova.version_number}.")
            return redirect(documento)
    stato = 422 if request.method == "POST" else 200
    return render(request, "documents/version_form.html", {"form": form, "documento": documento}, status=stato)


@login_required
@require_http_methods(["GET", "POST"])
def version_restore(request: HttpRequest, pk: int, numero: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_restore_version(request.user, documento):
        raise PermissionDenied
    if (bloccato := _se_bloccato(request, documento)) is not None:
        return bloccato
    versione = _versione(documento, numero)
    form = RestoreForm(dati_post(request))
    if request.method == "POST" and form.is_valid():
        try:
            nuova = restore_version(
                user=request.user, document=documento, version=versione, comment=form.cleaned_data["comment"]
            )
        except (VersioneNonValida, DocumentoBloccato) as errore:
            messages.error(request, str(errore))
            return redirect(documento)
        messages.success(
            request, f"Versione {versione.version_number} ripristinata come nuova versione {nuova.version_number}."
        )
        return redirect(documento)
    return render(
        request,
        "documents/version_restore.html",
        {"form": form, "documento": documento, "versione": versione},
        status=422 if request.method == "POST" else 200,
    )


# --------------------------------------------------------------------------
# Revisione a più persone
# --------------------------------------------------------------------------

def _solo_proprietario(request: HttpRequest, documento: Document) -> None:
    if not permissions.can_submit_review(request.user, documento):
        raise PermissionDenied


def _iniziale_da_ultimo_giro(documento: Document):
    """Riproposte le stesse persone (e la modalità) del giro precedente."""
    ultimo = documento.review_rounds.prefetch_related("steps").first()
    if ultimo is None:
        return None
    return {
        "sequenziale": ultimo.sequential,
        "righe": [{"revisore": str(p.reviewer_id), "ruolo": p.label} for p in ultimo.steps.all()],
    }


@login_required
@require_http_methods(["GET", "POST"])
def review_start(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    _solo_proprietario(request, documento)
    if documento.status != Document.Status.DRAFT:
        messages.error(request, "La revisione si avvia solo da Bozza.")
        return redirect(documento)
    form = ReviewStartForm(
        dati_post(request), document=documento, iniziale=_iniziale_da_ultimo_giro(documento)
    )
    if request.method == "POST" and form.is_valid():
        try:
            start_review(
                user=request.user, document=documento, reviewers=form.revisori,
                sequential=form.cleaned_data["sequenziale"],
                message=form.cleaned_data["messaggio"],
            )
        except TransizioneNonValida as errore:
            form.add_error(None, str(errore))
        else:
            messages.success(request, "Revisione avviata: i revisori sono stati avvisati.")
            return redirect(documento)
    return render(
        request, "documents/review_start.html", {"form": form, "documento": documento},
        status=422 if request.method == "POST" else 200,
    )


@login_required
@require_http_methods(["GET"])
def review_row(request: HttpRequest, pk: int) -> HttpResponse:
    """Una riga vuota in più (HTMX: «Aggiungi una persona»)."""
    documento = _documento(request, pk)
    _solo_proprietario(request, documento)
    if not is_htmx(request):
        return redirect("documents:review_start", pk=pk)
    form = ReviewStartForm(document=documento)
    return render(request, "documents/review_start.html#riga", {"r": {"revisore": "", "ruolo": ""}, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def review_decide(request: HttpRequest, pk: int, esito: str) -> HttpResponse:
    documento = _documento(request, pk)
    if esito not in ("approva", "rimanda"):
        raise Http404
    giro = documento.review_rounds.filter(outcome=ReviewRound.Outcome.PENDING).first()
    passo = giro.steps.filter(reviewer=request.user).first() if giro else None
    if giro is None or passo is None:
        if giro is None:
            messages.error(request, "Non c'è nessuna revisione in corso: potrebbe essere già stata chiusa.")
            return redirect(documento)
        raise PermissionDenied
    approva = esito == "approva"
    form = TransitionForm(dati_post(request), nota_obbligatoria=not approva)
    if request.method == "POST" and form.is_valid():
        try:
            decide_review(user=request.user, document=documento, approve=approva, note=form.cleaned_data["nota"])
        except TransizioneNonValida as errore:
            messages.error(request, str(errore))
        else:
            messages.success(request, "Hai approvato." if approva else "Hai rimandato il documento al proprietario.")
        return redirect(documento)
    return render(
        request, "documents/review_decide.html",
        {"documento": documento, "form": form, "approva": approva, "giro": giro, "passo": passo},
        status=422 if request.method == "POST" else 200,
    )


@login_required
@require_http_methods(["GET", "POST"])
def review_withdraw(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    _solo_proprietario(request, documento)
    form = TransitionForm(dati_post(request), nota_obbligatoria=True)
    if request.method == "POST" and form.is_valid():
        try:
            withdraw_review(user=request.user, document=documento, note=form.cleaned_data["nota"])
        except TransizioneNonValida as errore:
            messages.error(request, str(errore))
        else:
            messages.success(request, "Revisione ritirata: il documento è tornato in Bozza.")
        return redirect(documento)
    return render(
        request, "documents/review_withdraw.html", {"documento": documento, "form": form},
        status=422 if request.method == "POST" else 200,
    )


# --------------------------------------------------------------------------
# Workflow e storico
# --------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def document_transition(request: HttpRequest, pk: int, stato: str) -> HttpResponse:
    documento = _documento(request, pk)
    t = workflow.transizione(documento.status, stato)
    if t is None:
        messages.error(
            request, "Questa operazione non è più possibile: lo stato del documento è cambiato. Controlla e riprova."
        )
        return redirect(documento)
    if not workflow.consentita(t, request.user, documento):
        raise PermissionDenied
    form = TransitionForm(dati_post(request), nota_obbligatoria=t.nota_obbligatoria)
    if request.method == "POST" and form.is_valid():
        try:
            change_status(user=request.user, document=documento, to_status=stato, note=form.cleaned_data["nota"])
        except TransizioneNonValida as errore:
            messages.error(request, str(errore))
        else:
            messages.success(request, f"«{documento.title}»: {t.etichetta.lower()} eseguito.")
        return redirect(documento)
    return render(
        request,
        "documents/transition.html",
        {"documento": documento, "transizione": t, "form": form},
        status=422 if request.method == "POST" else 200,
    )


@login_required
@require_http_methods(["GET"])
def document_history(request: HttpRequest, pk: int) -> HttpResponse:
    from core.models import AuditLog

    documento = _documento(request, pk)
    if not permissions.can_share(request.user, documento):  # solo chi amministra il documento
        raise PermissionDenied
    voci = AuditLog.objects.filter(object_type="document", object_id=documento.pk)
    pagina = Paginator(voci, 50).get_page(request.GET.get("page"))
    audit_annota(pagina)
    return render(request, "documents/history.html", {"documento": documento, "pagina": pagina})


def audit_annota(voci) -> None:
    """Aggiunge testo leggibile ed etichetta a ogni voce del registro da mostrare."""
    for v in voci:
        v.etichetta = audit.ETICHETTE.get(v.action, v.action)
        v.testo = audit.descrivi(v)


# --------------------------------------------------------------------------
# Condivisione
# --------------------------------------------------------------------------

def _contesto_condivisione(documento: Document, form: ShareForm) -> dict:
    return {
        "documento": documento,
        "form": form,
        "condivisioni": documento.permissions.select_related("user", "group"),
    }


@login_required
@require_http_methods(["GET", "POST"])
def document_share(request: HttpRequest, pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_share(request.user, documento):
        raise PermissionDenied
    form = ShareForm(dati_post(request), document=documento)
    stato = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                _, creata = share_document(
                    user=request.user,
                    document=documento,
                    level=form.cleaned_data["livello"],
                    grantee_user=form.utente,
                    grantee_group=form.gruppo,
                )
            except CondivisioneNonValida as errore:
                form.add_error("destinatario", str(errore))
                stato = 422
            else:
                if not is_htmx(request):
                    messages.success(request, "Condivisione salvata.")
                    return redirect("documents:share", pk=documento.pk)
                form = ShareForm(document=documento)
        else:
            stato = 422
    contesto = _contesto_condivisione(documento, form)
    if is_htmx(request) and request.method == "POST":
        risposta = render(request, "documents/share.html#pannello", contesto, status=stato)
    else:
        risposta = render(request, "documents/share.html", contesto, status=stato)
    patch_vary_headers(risposta, ["HX-Request"])
    return risposta


@login_required
@require_POST
def share_revoke(request: HttpRequest, pk: int, perm_pk: int) -> HttpResponse:
    documento = _documento(request, pk)
    if not permissions.can_share(request.user, documento):
        raise PermissionDenied
    permesso = get_object_or_404(DocumentPermission, pk=perm_pk, document=documento)
    revoke_share(user=request.user, document=documento, permission=permesso)
    # Se ha tolto l'accesso a se stesso non può più restare su questa pagina.
    ancora = Document.objects.visible_to(request.user).filter(pk=documento.pk).exists()
    if is_htmx(request):
        if not ancora:
            risposta = HttpResponse("")
            risposta["HX-Redirect"] = reverse("documents:list")
            return risposta
        return HttpResponse("")
    messages.success(request, "Accesso revocato.")
    return redirect("documents:share", pk=documento.pk) if ancora else redirect("documents:list")


# --------------------------------------------------------------------------
# Categorie (solo staff)
# --------------------------------------------------------------------------

def _solo_staff(request: HttpRequest) -> None:
    if not request.user.is_staff:
        raise PermissionDenied


@login_required
@require_http_methods(["GET", "POST"])
def category_list(request: HttpRequest) -> HttpResponse:
    _solo_staff(request)
    form = CategoryForm(dati_post(request))
    stato = 200
    if request.method == "POST":
        if form.is_valid():
            form.save()
            form = CategoryForm()
            if not is_htmx(request):
                messages.success(request, "Categoria creata.")
                return redirect("documents:categories")
        else:
            stato = 422
    contesto = {"form": form, "categorie": Category.objects.annotate(n=Count("documents"))}
    if is_htmx(request) and request.method == "POST":
        risposta = render(request, "documents/categories.html#pannello", contesto, status=stato)
    else:
        risposta = render(request, "documents/categories.html", contesto, status=stato)
    patch_vary_headers(risposta, ["HX-Request"])
    return risposta


@login_required
@require_POST
def category_describe(request: HttpRequest, pk: int) -> HttpResponse:
    """Una riga che dice cosa va nella categoria: la legge la classificazione automatica."""
    _solo_staff(request)
    categoria = get_object_or_404(Category, pk=pk)
    categoria.description = " ".join(request.POST.get("descrizione", "").split())[:300]
    categoria.save(update_fields=["description"])
    messages.success(request, f"Descrizione di «{categoria.name}» salvata.")
    return redirect("documents:categories")


@login_required
@require_POST
def category_delete(request: HttpRequest, pk: int) -> HttpResponse:
    _solo_staff(request)
    categoria = get_object_or_404(Category, pk=pk)
    try:
        categoria.delete()
    except ProtectedError:
        testo = f"«{categoria.name}» è usata da dei documenti: spostali prima di eliminarla."
        if is_htmx(request):
            risposta = render(request, "components/alert.html", {"tag": "error", "testo": testo}, status=409)
            risposta["HX-Retarget"] = "#avvisi"
            risposta["HX-Reswap"] = "innerHTML"
            return risposta
        messages.error(request, testo)
        return redirect("documents:categories")
    if is_htmx(request):
        return HttpResponse("")
    messages.success(request, "Categoria eliminata.")
    return redirect("documents:categories")
