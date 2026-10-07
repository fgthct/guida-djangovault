"""Tag `{% asset 'css/app.css' %}`: URL statico con `?v=<mtime>` contro la cache del browser."""

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()
_cache: dict[str, str] = {}


def _versione(percorso: str) -> str:
    trovato = finders.find(percorso)
    if not trovato:
        return "0"
    from pathlib import Path

    return str(int(Path(trovato).stat().st_mtime))


@register.simple_tag
def asset(percorso: str) -> str:
    if settings.DEBUG or percorso not in _cache:
        _cache[percorso] = f"{static(percorso)}?v={_versione(percorso)}"
    return _cache[percorso]
