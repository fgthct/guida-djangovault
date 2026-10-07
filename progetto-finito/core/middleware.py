from django.conf import settings

from .services import audit


class RequestContextMiddleware:
    """Memorizza l'IP della richiesta per il registro delle operazioni.

    X-Forwarded-For si legge solo se `AUDIT_TRUST_X_FORWARDED_FOR` è attivo (cioè quando
    davanti c'è davvero un proxy fidato): altrimenti chiunque potrebbe falsificarlo.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ip = request.META.get("REMOTE_ADDR")
        if settings.AUDIT_TRUST_X_FORWARDED_FOR:
            inoltrato = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0]
            ip = inoltrato or ip
        audit.imposta_ip(audit.ip_valido(ip))
        try:
            return self.get_response(request)
        finally:
            audit.imposta_ip(None)
