"""WebSocket `/ws/`: avvisi «qualcosa è cambiato» per l'utente collegato (notifiche, stato dei documenti)."""

from __future__ import annotations

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from documents.models import Document

from . import realtime

MAX_BYTE_MESSAGGIO = 1024


class AvvisiConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        utente = self.scope.get("user")
        if utente is None or not utente.is_authenticated:
            await self.close(code=4401)  # non autenticato
            return
        self.utente = utente
        self.documenti: set[int] = set()
        await self.channel_layer.group_add(realtime.gruppo_utente(utente.pk), self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if not hasattr(self, "utente"):
            return
        await self.channel_layer.group_discard(realtime.gruppo_utente(self.utente.pk), self.channel_name)
        for d in self.documenti:
            await self.channel_layer.group_discard(realtime.gruppo_documento(d), self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Il solo messaggio ammesso è {"segui": [id, ...]}: documenti di cui si vuole sapere lo stato."""
        if not text_data or len(text_data) > MAX_BYTE_MESSAGGIO:
            return
        try:
            dati = json.loads(text_data)
        except ValueError:
            return
        richiesti = dati.get("segui") if isinstance(dati, dict) else None
        if not isinstance(richiesti, list):
            return
        ids = {i for i in richiesti if isinstance(i, int) and not isinstance(i, bool) and i > 0}
        ids = set(sorted(ids)[: settings.REALTIME_MAX_DOCUMENTS])
        ammessi = await self._visibili(ids - self.documenti)
        for d in ammessi:
            if len(self.documenti) >= settings.REALTIME_MAX_DOCUMENTS:
                break
            await self.channel_layer.group_add(realtime.gruppo_documento(d), self.channel_name)
            self.documenti.add(d)

    @database_sync_to_async
    def _visibili(self, ids: set[int]) -> set[int]:
        if not ids:
            return set()
        return set(Document.objects.visible_to(self.utente).filter(pk__in=ids).values_list("pk", flat=True))

    async def avviso(self, evento):
        await self.send(text_data=json.dumps({k: v for k, v in evento.items() if k != "type"}))
