"""WebSocket /ws/: accesso, origine, avvisi solo a chi ne ha diritto."""

import shutil
import tempfile
from unittest import mock

from channels.testing import WebsocketCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings

from accounts.models import User
from config.asgi import application
from core import realtime
from core.consumers import AvvisiConsumer
from core.services.notifications import notify
from documents.models import DocumentVersion
from documents.services import create_document

MEDIA = tempfile.mkdtemp(prefix="dv-test-media-rt-")


@override_settings(MEDIA_ROOT=MEDIA, ALLOWED_HOSTS=["testserver", "localhost"])
class RealtimeTests(TransactionTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.mario = User.objects.create_user("mario", password="password-lunga-123")
        self.luisa = User.objects.create_user("luisa", password="password-lunga-123")

    def doc(self, owner):
        return create_document(
            owner=owner, title="Doc", uploaded_file=SimpleUploadedFile("a.txt", b"testo")
        ).document

    async def collega(self, utente):
        c = WebsocketCommunicator(AvvisiConsumer.as_asgi(), "/ws/")
        c.scope["user"] = utente
        ok, _ = await c.connect()
        self.assertTrue(ok)
        return c

    async def test_anonimo_rifiutato(self):
        c = WebsocketCommunicator(application, "/ws/", headers=[(b"origin", b"http://localhost")])
        ok, _ = await c.connect()
        self.assertFalse(ok)

    async def test_origine_estranea_rifiutata(self):
        c = WebsocketCommunicator(application, "/ws/", headers=[(b"origin", b"http://sito-malevolo.example")])
        ok, _ = await c.connect()
        self.assertFalse(ok)

    async def test_notifica_solo_al_destinatario(self):
        from asgiref.sync import sync_to_async

        c_mario, c_luisa = await self.collega(self.mario), await self.collega(self.luisa)
        await sync_to_async(notify)([self.mario], kind="x", message="ciao")
        self.assertEqual(await c_mario.receive_json_from(timeout=2), {"t": "notifiche"})
        self.assertTrue(await c_luisa.receive_nothing(timeout=0.3))
        await c_mario.disconnect()
        await c_luisa.disconnect()

    async def test_stato_versione_solo_per_documenti_visibili(self):
        from asgiref.sync import sync_to_async

        mio = await sync_to_async(self.doc)(self.mario)
        altrui = await sync_to_async(self.doc)(self.luisa)
        c = await self.collega(self.mario)
        await c.send_json_to({"segui": [mio.pk, altrui.pk]})
        await c.receive_nothing(timeout=0.3)

        def cambia(documento):
            v = DocumentVersion.objects.get(pk=documento.current_version_id)
            v.processing_message = "x"
            v.save(update_fields=["processing_message"])

        await sync_to_async(cambia)(altrui)
        self.assertTrue(await c.receive_nothing(timeout=0.3))  # non lo vede: nessun avviso
        await sync_to_async(cambia)(mio)
        self.assertEqual(await c.receive_json_from(timeout=2), {"t": "versione", "doc": mio.pk})
        await c.disconnect()

    async def test_il_battito_di_attivita_non_avvisa(self):
        from asgiref.sync import sync_to_async

        mio = await sync_to_async(self.doc)(self.mario)
        c = await self.collega(self.mario)
        await c.send_json_to({"segui": [mio.pk]})
        await c.receive_nothing(timeout=0.3)

        def batti():
            v = DocumentVersion.objects.get(pk=mio.current_version_id)
            v.save(update_fields=["processing_updated_at"])

        await sync_to_async(batti)()
        self.assertTrue(await c.receive_nothing(timeout=0.3))
        await c.disconnect()

    async def test_messaggi_malformati_ignorati(self):
        c = await self.collega(self.mario)
        for testo in ["non json", "[]", '{"segui": "x"}', '{"segui": [true, -1, "a"]}', "x" * 5000]:
            await c.send_to(text_data=testo)
        self.assertTrue(await c.receive_nothing(timeout=0.3))
        await c.disconnect()

    @override_settings(REALTIME_MAX_DOCUMENTS=2)
    async def test_limite_di_documenti_seguiti(self):
        from asgiref.sync import sync_to_async

        docs = [await sync_to_async(self.doc)(self.mario) for _ in range(3)]
        c = await self.collega(self.mario)
        await c.send_json_to({"segui": [d.pk for d in docs]})
        await c.receive_nothing(timeout=0.3)

        def cambia(d):
            v = DocumentVersion.objects.get(pk=d.current_version_id)
            v.save(update_fields=["processing_message"])

        for d in docs:
            await sync_to_async(cambia)(d)
        ricevuti = 0
        while not await c.receive_nothing(timeout=0.3):
            await c.receive_json_from()
            ricevuti += 1
        self.assertEqual(ricevuti, 2)
        await c.disconnect()

    def test_un_errore_del_livello_dei_canali_non_rompe_l_operazione(self):
        with mock.patch("core.realtime.get_channel_layer", side_effect=RuntimeError("redis giù")):
            n = notify([self.mario], kind="x", message="ciao")
        self.assertEqual(n, 1)
        self.assertEqual(self.mario.notifications.count(), 1)

    def test_nomi_dei_gruppi(self):
        self.assertEqual(realtime.gruppo_utente(3), "utente_3")
        self.assertEqual(realtime.gruppo_documento(7), "documento_7")
