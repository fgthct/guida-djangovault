from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.utils import timezone
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from documents.models import Document, ReviewStep

from .models import AuditLog, Notification
from .services import audit
from .services.health import controlla_servizi


def is_htmx(request: HttpRequest) -> bool:
    """True se la richiesta arriva da HTMX (header HX-Request)."""
    return request.headers.get("HX-Request") == "true"


@login_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    visibili = Document.objects.visible_to(request.user)
    return render(
        request,
        "core/dashboard.html",
        {
            "totale_documenti": visibili.count(),
            "recenti": visibili.select_related("category")[:5],
            "da_rivedere": ReviewStep.objects.attesi_da(request.user).select_related("round__document__owner")[:5],
            "n_da_rivedere": ReviewStep.objects.attesi_da(request.user).count(),
        },
    )


@login_required
@require_GET
def stato_servizi(request: HttpRequest) -> HttpResponse:
    """Frammento HTMX con lo stato dei servizi (partial `servizi` della dashboard)."""
    if not is_htmx(request):
        return redirect("core:dashboard")
    return render(request, "core/dashboard.html#servizi", {"servizi": controlla_servizi()})


@require_GET
def healthz(request: HttpRequest) -> HttpResponse:
    """Controllo di vita per Docker/Nginx: non richiede login e non espone dettagli."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        return HttpResponse("db non raggiungibile", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


# --------------------------------------------------------------------------
# Notifiche
# --------------------------------------------------------------------------

@login_required
@require_GET
def notifiche(request: HttpRequest) -> HttpResponse:
    pagina = Paginator(request.user.notifications.all(), 30).get_page(request.GET.get("page"))
    return render(request, "core/notifiche.html", {"pagina": pagina})


@login_required
@require_GET
def notifiche_contatore(request: HttpRequest) -> HttpResponse:
    """Badge dell'intestazione (HTMX lo aggiorna ogni 30 secondi)."""
    n = request.user.notifications.filter(read_at__isnull=True).count()
    return render(request, "components/notifiche_badge.html", {"notifiche_non_lette": n})


@login_required
@require_POST
def notifica_apri(request: HttpRequest, pk: int) -> HttpResponse:
    """Segna la notifica come letta e porta alla pagina collegata (solo le proprie: altrimenti 404)."""
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    # L'indirizzo è creato dal server, ma per sicurezza accettiamo solo percorsi interni.
    if n.url.startswith("/") and not n.url.startswith("//"):
        return redirect(n.url)
    return redirect("core:notifiche")


@login_required
@require_POST
def notifiche_segna_lette(request: HttpRequest) -> HttpResponse:
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    messages.success(request, "Tutte le notifiche sono state segnate come lette.")
    return redirect("core:notifiche")


# --------------------------------------------------------------------------
# Registro delle operazioni (solo superutenti)
# --------------------------------------------------------------------------

GRUPPI_AZIONI = [
    ("", "Tutte"),
    ("auth.", "Accessi"),
    ("document.", "Documenti"),
    ("version.", "Versioni"),
    ("share.", "Condivisioni"),
]


@login_required
@require_GET
def registro(request: HttpRequest) -> HttpResponse:
    if not request.user.is_superuser:
        raise PermissionDenied
    gruppo = request.GET.get("gruppo", "")
    voci = AuditLog.objects.select_related("user")
    if gruppo in {g for g, _ in GRUPPI_AZIONI if g}:
        voci = voci.filter(action__startswith=gruppo)
    pagina = Paginator(voci, 50).get_page(request.GET.get("page"))
    for v in pagina:
        v.etichetta = audit.ETICHETTE.get(v.action, v.action)
        v.testo = audit.descrivi(v)
    return render(request, "core/registro.html", {"pagina": pagina, "gruppo": gruppo, "gruppi": GRUPPI_AZIONI})
