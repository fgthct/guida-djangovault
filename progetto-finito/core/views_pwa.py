"""PWA: manifest, service worker e pagina «sei offline».

Manifest, service worker e pagina offline sono pubblici (senza accesso): il browser li chiede prima e fuori dalla
sessione, e non contengono nulla di riservato. `condividi` invece richiede l'accesso.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

TEMA = "#111318"
# Tipi che l'app accetta quando le si condivide qualcosa dal telefono (menu «Condividi»).
TIPI_CONDIVISIBILI = [
    "application/pdf", "text/plain", "text/markdown", "text/csv", "image/*",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text", "application/rtf",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.presentation",
]


def _senza_cache(risposta: HttpResponse) -> HttpResponse:
    risposta["Cache-Control"] = "no-cache"
    return risposta


@require_GET
def manifest(request: HttpRequest) -> JsonResponse:
    """Manifest dell'app installabile. Le icone sono file statici; la condivisione dal telefono (share target) arriva
    a `/condividi/`, che intercetta il service worker (vedi templates/pwa/sw.js)."""
    r = JsonResponse({
        "id": "/",
        "name": "DjangoVault",
        "short_name": "DV",
        "description": "Il tuo archivio di documenti, con ricerca e assistente.",
        "lang": "it",
        "dir": "ltr",
        "start_url": "/?origine=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": TEMA,
        "theme_color": TEMA,
        "categories": ["productivity", "utilities"],
        "icons": [
            {"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("img/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("img/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Scansiona", "short_name": "Scansiona", "url": reverse("documents:scan"),
             "icons": [{"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
            {"name": "Carica file", "short_name": "Carica", "url": reverse("documents:upload"),
             "icons": [{"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
            {"name": "Da smistare", "short_name": "Da smistare", "url": reverse("documents:smistamento"),
             "icons": [{"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
        ],
        "share_target": {
            "action": reverse("core:condividi"),
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {"title": "title", "text": "text", "url": "url", "files": [{"name": "file", "accept": TIPI_CONDIVISIBILI}]},
        },
    }, json_dumps_params={"ensure_ascii": False})
    r["Content-Type"] = "application/manifest+json"
    return _senza_cache(r)


def _versione_risorse() -> str:
    """Cambia quando cambia il service worker o i file dell'app che mette da parte: così il browser scarta la vecchia cache."""
    h = hashlib.sha1()
    for p in ("css/app.css", "js/app.js", "js/pwa.js"):
        trovato = finders.find(p)
        h.update(str(Path(trovato).stat().st_mtime_ns if trovato else 0).encode())
    h.update((Path(settings.BASE_DIR) / "templates" / "pwa" / "sw.js").read_bytes())
    return h.hexdigest()[:10]


@require_GET
def service_worker(request: HttpRequest) -> HttpResponse:
    """Il service worker deve stare alla radice: solo così controlla tutto il sito."""
    r = render(request, "pwa/sw.js", {"versione": _versione_risorse()}, content_type="text/javascript; charset=utf-8")
    r["Service-Worker-Allowed"] = "/"
    return _senza_cache(r)


@require_GET
def offline(request: HttpRequest) -> HttpResponse:
    return render(request, "pwa/offline.html")


@csrf_exempt  # non cambia nulla: risponde solo con un rinvio alla pagina di caricamento
@login_required
def condividi(request: HttpRequest) -> HttpResponse:
    """Normalmente il POST della condivisione lo prende il service worker, che passa i file alla pagina di caricamento.
    Se arriva qui (service worker non attivo, GET, ecc.) non si perde nulla: si manda alla pagina di caricamento."""
    return redirect("documents:upload")
