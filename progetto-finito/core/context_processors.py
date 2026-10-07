def notifiche(request):
    """Numero di notifiche non lette, per il badge nell'intestazione."""
    utente = getattr(request, "user", None)
    if not utente or not utente.is_authenticated:
        return {}
    return {"notifiche_non_lette": utente.notifications.filter(read_at__isnull=True).count()}
