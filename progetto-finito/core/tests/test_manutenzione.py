from datetime import timedelta

from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from core.models import AuditLog, Notification
from core.services import maintenance
from core.tasks import manutenzione_task


class ManutenzioneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.u = User.objects.create_user("mario", password="password-lunga-123")

    def notifica(self, giorni, letta):
        n = Notification.objects.create(recipient=self.u, kind="x", message="m")
        quando = timezone.now() - timedelta(days=giorni)
        Notification.objects.filter(pk=n.pk).update(created_at=quando, read_at=quando if letta else None)
        return n.pk

    def test_sessioni_scadute_eliminate_quelle_valide_restano(self):
        Session.objects.create(session_key="a", session_data="x", expire_date=timezone.now() - timedelta(days=1))
        Session.objects.create(session_key="b", session_data="x", expire_date=timezone.now() + timedelta(days=1))
        self.assertEqual(maintenance.pulisci()["sessioni"], 1)
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), ["b"])

    @override_settings(NOTIFICATION_RETENTION_DAYS=90)
    def test_solo_le_notifiche_lette_e_vecchie(self):
        vecchia_letta = self.notifica(120, True)
        vecchia_non_letta = self.notifica(120, False)
        recente_letta = self.notifica(5, True)
        self.assertEqual(maintenance.pulisci()["notifiche"], 1)
        self.assertFalse(Notification.objects.filter(pk=vecchia_letta).exists())
        self.assertTrue(Notification.objects.filter(pk__in=[vecchia_non_letta, recente_letta]).count() == 2)

    @override_settings(NOTIFICATION_RETENTION_DAYS=0)
    def test_zero_giorni_vuol_dire_per_sempre(self):
        self.notifica(1000, True)
        self.assertEqual(maintenance.pulisci()["notifiche"], 0)

    def voce(self, giorni):
        v = AuditLog.objects.create(username="x", action="a")
        AuditLog.objects.filter(pk=v.pk).update(timestamp=timezone.now() - timedelta(days=giorni))

    @override_settings(AUDIT_RETENTION_DAYS=0)
    def test_il_registro_per_default_non_si_tocca(self):
        self.voce(5000)
        self.assertEqual(maintenance.pulisci()["registro"], 0)
        self.assertEqual(AuditLog.objects.count(), 1)

    @override_settings(AUDIT_RETENTION_DAYS=365)
    def test_il_registro_oltre_il_periodo_viene_ripulito(self):
        self.voce(400)
        self.voce(10)
        self.assertEqual(maintenance.pulisci()["registro"], 1)
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_task_e_comando(self):
        from io import StringIO

        from django.core.management import call_command

        self.assertEqual(set(manutenzione_task()), {"sessioni", "notifiche", "registro"})
        out = StringIO()
        call_command("manutenzione", stdout=out)
        self.assertIn("sessioni: 0", out.getvalue())
