from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.urls import reverse

from accounts.models import User
from core.models import AuditLog, Notification
from documents.models import Document, DocumentPermission, ReviewRound, ReviewStep
from documents.services import (
    DocumentoBloccato,
    TransizioneNonValida,
    add_version,
    change_status,
    decide_review,
    restore_version,
    share_document,
    start_review,
    transizioni_disponibili,
    update_document,
    withdraw_review,
)

from .base import DocumentTestCase

S = Document.Status
L = DocumentPermission.Level
R = ReviewRound.Outcome


class WFBase(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(owner=self.mario, titolo="Contratto")
        # luisa: modifica; staff: sola lettura; cto: nessun accesso (lo riceverà dalla revisione)
        DocumentPermission.objects.create(document=self.doc, user=self.luisa, level=L.WRITE)
        DocumentPermission.objects.create(document=self.doc, user=self.staff, level=L.READ)
        self.cto = User.objects.create_user("cto", password="password-lunga-123")
        self.estraneo = User.objects.create_user("estraneo", password="password-lunga-123")

    def fresco(self):
        return Document.objects.get(pk=self.doc.pk)

    def avvia(self, *persone, sequenziale=False, user=None):
        persone = persone or (self.staff,)
        return start_review(
            user=user or self.mario, document=self.fresco(),
            reviewers=[(p, "") for p in persone], sequential=sequenziale,
        )

    def approva(self, user, nota=""):
        return decide_review(user=user, document=self.fresco(), approve=True, note=nota)

    def rimanda(self, user, nota="Da rifare"):
        return decide_review(user=user, document=self.fresco(), approve=False, note=nota)

    def vai(self, user, a, nota=""):
        return change_status(user=user, document=self.fresco(), to_status=a, note=nota)

    def stato(self):
        return self.fresco().status


class AvvioTests(WFBase):
    def test_solo_il_proprietario_avvia(self):
        for u in (self.luisa, self.staff, self.estraneo):
            with self.assertRaises(PermissionDenied):
                self.avvia(self.cto, user=u)
        self.assertEqual(self.stato(), S.DRAFT)

    def test_avvio_porta_in_revisione_e_registra_il_giro(self):
        giro = self.avvia(self.staff, self.cto, sequenziale=True)
        self.assertEqual(self.stato(), S.IN_REVIEW)
        self.assertEqual((giro.number, giro.sequential, giro.outcome), (1, True, R.PENDING))
        self.assertEqual(giro.version_id, self.doc.current_version_id)
        self.assertEqual([p.reviewer.username for p in giro.steps.all()], ["staff", "cto"])

    def test_chi_non_ha_accesso_riceve_la_lettura(self):
        self.avvia(self.cto)
        self.assertEqual(DocumentPermission.objects.get(user=self.cto, document=self.doc).level, L.READ)
        self.assertTrue(Document.objects.visible_to(self.cto).filter(pk=self.doc.pk).exists())
        voce = AuditLog.objects.get(action="share.granted", object_id=self.doc.pk, metadata__motivo="revisione")
        self.assertEqual(voce.metadata["destinatario"], "cto")

    def test_non_abbassa_un_accesso_piu_alto(self):
        self.avvia(self.luisa)
        self.assertEqual(DocumentPermission.objects.get(user=self.luisa, document=self.doc).level, L.WRITE)

    def test_validazioni(self):
        inattivo = User.objects.create_user("off", password="password-lunga-123", is_active=False)
        casi = [
            [],
            [(self.mario, "")],
            [(self.staff, ""), (self.staff, "bis")],
            [(inattivo, "")],
            [(User.objects.create_user(f"u{i}", password="password-lunga-123"), "") for i in range(11)],
        ]
        for revisori in casi:
            with self.assertRaises(TransizioneNonValida):
                start_review(user=self.mario, document=self.fresco(), reviewers=revisori, sequential=False)
        self.assertEqual(self.stato(), S.DRAFT)
        self.assertFalse(ReviewRound.objects.exists())

    def test_non_si_avvia_due_volte_ne_fuori_bozza(self):
        self.avvia()
        with self.assertRaises(TransizioneNonValida):
            self.avvia(self.cto)
        self.assertEqual(ReviewRound.objects.count(), 1)

    def test_il_superutente_non_avvia_il_giro_di_un_altro(self):
        with self.assertRaises(PermissionDenied):
            self.avvia(user=self.admin)


class DecisioneTests(WFBase):
    def test_tutti_approvano_il_documento_e_approvato(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        self.assertEqual(self.stato(), S.IN_REVIEW)
        self.approva(self.cto)
        self.assertEqual(self.stato(), S.APPROVED)
        self.assertEqual(ReviewRound.objects.get().outcome, R.APPROVED)
        self.assertIsNotNone(ReviewRound.objects.get().closed_at)

    def test_rimandare_chiude_il_giro_e_riporta_in_bozza(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        self.rimanda(self.cto, "Manca l'allegato B")
        self.assertEqual(self.stato(), S.DRAFT)
        giro = ReviewRound.objects.get()
        self.assertEqual((giro.outcome, giro.closing_note), (R.REJECTED, "Manca l'allegato B"))
        self.assertEqual(giro.steps.get(reviewer=self.staff).decision, ReviewStep.Decision.APPROVED)  # storico intatto
        update_document(user=self.mario, document=self.fresco(), title="Rivisto")  # di nuovo modificabile

    def test_rimandare_richiede_la_nota(self):
        self.avvia()
        for nota in ("", "   "):
            with self.assertRaises(TransizioneNonValida):
                self.rimanda(self.staff, nota)
        self.assertEqual(self.stato(), S.IN_REVIEW)

    def test_chi_non_e_revisore_non_decide_ne_il_proprietario_ne_il_superutente(self):
        self.avvia()
        for u in (self.mario, self.luisa, self.admin, self.estraneo):
            with self.assertRaises(PermissionDenied):
                self.approva(u)
        self.assertEqual(ReviewStep.objects.filter(decision=ReviewStep.Decision.PENDING).count(), 1)

    def test_non_si_risponde_due_volte(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        with self.assertRaises(TransizioneNonValida):
            self.approva(self.staff)

    def test_nessun_giro_in_corso(self):
        with self.assertRaises(TransizioneNonValida):
            self.approva(self.staff)

    def test_modalita_libera_risponde_chi_vuole(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.cto)  # il secondo in elenco può agire per primo
        self.approva(self.staff)
        self.assertEqual(self.stato(), S.APPROVED)

    def test_modalita_sequenziale_rispetta_l_ordine(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        with self.assertRaises(TransizioneNonValida):
            self.approva(self.cto)  # non è il suo turno
        with self.assertRaises(TransizioneNonValida):
            self.rimanda(self.cto)  # nemmeno per rimandare
        self.assertEqual(self.stato(), S.IN_REVIEW)
        self.approva(self.staff)
        self.approva(self.cto)
        self.assertEqual(self.stato(), S.APPROVED)

    def test_attesi_da(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        attesi = lambda u: list(ReviewStep.objects.attesi_da(u))
        self.assertEqual(len(attesi(self.staff)), 1)
        self.assertEqual(attesi(self.cto), [])
        self.approva(self.staff)
        self.assertEqual(attesi(self.staff), [])
        self.assertEqual(len(attesi(self.cto)), 1)
        self.approva(self.cto)
        self.assertEqual(attesi(self.cto), [])

    def test_attesi_da_in_modalita_libera(self):
        self.avvia(self.staff, self.cto)
        self.assertEqual(ReviewStep.objects.attesi_da(self.cto).count(), 1)
        self.assertEqual(ReviewStep.objects.attesi_da(self.staff).count(), 1)

    def test_una_sola_revisione_in_corso_per_documento(self):
        from django.db import IntegrityError, transaction

        self.avvia()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewRound.objects.create(document=self.doc, number=2, sequential=False)


class MessaggioTests(WFBase):
    def test_il_messaggio_arriva_ai_revisori_ed_e_nel_registro(self):
        start_review(
            user=self.mario, document=self.fresco(), reviewers=[(self.staff, ""), (self.cto, "")],
            sequential=False, message="  Controlla il capitolo 3  ",
        )
        giro = ReviewRound.objects.get()
        self.assertEqual(giro.message, "Controlla il capitolo 3")
        for n in Notification.objects.filter(kind="review.requested"):
            self.assertIn("Messaggio: Controlla il capitolo 3", n.message)
        self.assertEqual(AuditLog.objects.get(action="review.started").metadata["messaggio"], "Controlla il capitolo 3")

    def test_messaggio_lungo_accorciato_nella_notifica_ma_intero_nel_giro(self):
        lungo = "x" * 900
        start_review(user=self.mario, document=self.fresco(), reviewers=[(self.staff, "")], sequential=False, message=lungo)
        self.assertEqual(len(ReviewRound.objects.get().message), 900)
        self.assertLessEqual(len(Notification.objects.get().message), 300)

    def test_senza_messaggio_tutto_come_prima(self):
        self.avvia()
        self.assertEqual(ReviewRound.objects.get().message, "")
        self.assertNotIn("Messaggio", Notification.objects.get().message)

    def test_dalla_pagina_e_visibile_a_chi_deve_decidere(self):
        self.client.force_login(self.mario)
        self.client.post(
            reverse("documents:review_start", args=[self.doc.pk]),
            {"revisore": [self.staff.pk], "messaggio": "Se avete qualcosa da aggiungere, scrivetelo <b>qui</b>"},
        )
        self.client.force_login(self.staff)
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, "Se avete qualcosa da aggiungere")
        self.assertContains(r, "&lt;b&gt;qui&lt;/b&gt;")  # sfuggito, non interpretato
        r = self.client.get(reverse("documents:review_decide", args=[self.doc.pk, "approva"]))
        self.assertContains(r, "Messaggio del proprietario")

    def test_il_form_ha_il_campo_e_rifiuta_testi_troppo_lunghi(self):
        self.client.force_login(self.mario)
        url = reverse("documents:review_start", args=[self.doc.pk])
        self.assertContains(self.client.get(url), 'name="messaggio"')
        r = self.client.post(url, {"revisore": [self.staff.pk], "messaggio": "y" * 1001})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.stato(), S.DRAFT)


class RitiroERiinvioTests(WFBase):
    def test_il_proprietario_ritira_con_motivo(self):
        self.avvia(self.staff, self.cto)
        with self.assertRaises(TransizioneNonValida):
            withdraw_review(user=self.mario, document=self.fresco(), note=" ")
        for u in (self.luisa, self.staff, self.admin):
            with self.assertRaises(PermissionDenied):
                withdraw_review(user=u, document=self.fresco(), note="x")
        withdraw_review(user=self.mario, document=self.fresco(), note="Cambio di piano")
        self.assertEqual(self.stato(), S.DRAFT)
        self.assertEqual(ReviewRound.objects.get().outcome, R.WITHDRAWN)
        with self.assertRaises(TransizioneNonValida):
            withdraw_review(user=self.mario, document=self.fresco(), note="di nuovo")

    def test_dopo_un_rifiuto_nuovo_giro_con_approvazioni_azzerate(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        self.rimanda(self.cto)
        self.avvia(self.staff, self.cto)
        giro2 = ReviewRound.objects.get(number=2)
        self.assertEqual({p.decision for p in giro2.steps.all()}, {ReviewStep.Decision.PENDING})
        self.approva(self.cto)
        self.assertEqual(self.stato(), S.IN_REVIEW)  # l'approvazione di staff del giro 1 non vale più
        self.approva(self.staff)
        self.assertEqual(self.stato(), S.APPROVED)

    def test_nuova_versione_tra_un_giro_e_l_altro(self):
        self.avvia()
        self.rimanda(self.staff)
        add_version(user=self.mario, document=self.fresco(), uploaded_file=self.file(contenuto=b"v2"))
        giro = self.avvia()
        self.assertEqual(giro.version.version_number, 2)


class TransizioniTests(WFBase):
    def test_non_si_passa_a_in_revisione_ne_approvato_a_mano(self):
        for a in (S.IN_REVIEW, S.APPROVED):
            with self.assertRaises(TransizioneNonValida):
                self.vai(self.mario, a)
        self.assertEqual(self.stato(), S.DRAFT)

    def test_solo_admin_archivia_e_riapre(self):
        self.avvia()
        self.approva(self.staff)
        for a in (S.ARCHIVED, S.DRAFT):
            with self.assertRaises(PermissionDenied):
                self.vai(self.luisa, a, "x")
        with self.assertRaises(TransizioneNonValida):
            self.vai(self.mario, S.DRAFT)  # riaprire richiede il motivo
        self.vai(self.mario, S.DRAFT, "Serve una correzione")
        self.assertEqual(self.stato(), S.DRAFT)

    def test_archiviare_e_riaprire(self):
        self.vai(self.mario, S.ARCHIVED)
        self.vai(self.mario, S.DRAFT, "serve ancora")

    def test_bozza_puo_essere_archiviata_solo_da_admin(self):
        with self.assertRaises(PermissionDenied):
            self.vai(self.luisa, S.ARCHIVED)

    def test_dal_in_revisione_nessuna_transizione_manuale(self):
        self.avvia()
        for a in (S.DRAFT, S.APPROVED, S.ARCHIVED):
            with self.assertRaises(TransizioneNonValida):
                self.vai(self.mario, a, "x")

    def test_transizioni_disponibili_per_livello(self):
        def nomi(u):
            return {t.a for t in transizioni_disponibili(u, self.fresco())}

        self.assertEqual(nomi(self.luisa), set())
        self.assertEqual(nomi(self.mario), {S.ARCHIVED})
        self.avvia()
        self.assertEqual(nomi(self.mario), set())
        self.approva(self.staff)
        self.assertEqual(nomi(self.mario), {S.ARCHIVED, S.DRAFT})


class BloccoTests(WFBase):
    def test_in_revisione_blocca_modifiche_versioni_e_ripristini(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"))
        v1 = self.doc.versions.get(version_number=1)
        self.avvia()
        with self.assertRaises(DocumentoBloccato):
            update_document(user=self.mario, document=self.doc, title="Nuovo")
        with self.assertRaises(DocumentoBloccato):
            add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"tre"))
        with self.assertRaises(DocumentoBloccato):
            restore_version(user=self.mario, document=self.doc, version=v1)
        self.assertEqual(self.doc.versions.count(), 2)
        self.assertEqual(self.fresco().title, "Contratto")

    def test_approvato_e_archiviato_sono_bloccati(self):
        self.avvia()
        self.approva(self.staff)
        with self.assertRaises(DocumentoBloccato):
            update_document(user=self.mario, document=self.doc, title="x")
        self.vai(self.mario, S.ARCHIVED)
        with self.assertRaises(DocumentoBloccato):
            add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"z"))

    def test_la_condivisione_non_e_bloccata(self):
        self.avvia()
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=self.estraneo)


class ViewWorkflowTests(WFBase):
    def test_form_di_avvio_solo_proprietario(self):
        url = reverse("documents:review_start", args=[self.doc.pk])
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"revisore": [self.staff.pk]}).status_code, 403)
        self.client.force_login(self.estraneo)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.stato(), S.DRAFT)

    def test_il_menu_non_offre_il_proprietario_e_mostra_gli_altri(self):
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:review_start", args=[self.doc.pk]))
        self.assertContains(r, 'value="%s"' % self.cto.pk)
        self.assertNotContains(r, 'value="%s"' % self.mario.pk)
        self.assertContains(r, "Approvazione in sequenza")

    def test_avvio_dalla_pagina(self):
        self.client.force_login(self.mario)
        r = self.client.post(
            reverse("documents:review_start", args=[self.doc.pk]),
            {"revisore": [self.cto.pk, "", self.staff.pk], "ruolo": ["CTO", "", "Acquisti"], "sequenziale": "on"},
            follow=True,
        )
        self.assertContains(r, "Revisione avviata")
        giro = ReviewRound.objects.get()
        self.assertTrue(giro.sequential)
        self.assertEqual([(p.reviewer.username, p.label) for p in giro.steps.all()], [("cto", "CTO"), ("staff", "Acquisti")])
        self.assertContains(r, "Di turno")

    def test_avvio_senza_nessuno_da_422_e_nessun_danno(self):
        self.client.force_login(self.mario)
        r = self.client.post(reverse("documents:review_start", args=[self.doc.pk]), {"revisore": ["", "", ""]})
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Scegli almeno una persona", status_code=422)
        r = self.client.post(reverse("documents:review_start", args=[self.doc.pk]), {"revisore": ["99999"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.stato(), S.DRAFT)

    def test_riga_htmx(self):
        self.client.force_login(self.mario)
        url = reverse("documents:review_row", args=[self.doc.pk])
        r = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertContains(r, 'name="revisore"')
        self.assertNotContains(r, "<html")
        self.assertEqual(self.client.get(url).status_code, 302)  # senza HTMX torna al form
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.get(url, HTTP_HX_REQUEST="true").status_code, 403)

    def test_precompila_dal_giro_precedente(self):
        self.avvia(self.cto, self.staff, sequenziale=True)
        self.rimanda(self.cto)
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:review_start", args=[self.doc.pk]))
        self.assertContains(r, f'<option value="{self.cto.pk}" selected>')
        self.assertContains(r, " checked")

    def test_pulsanti_per_ruolo_e_turno(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        ok = reverse("documents:review_decide", args=[self.doc.pk, "approva"])
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(self.doc.get_absolute_url()), ok)
        self.client.force_login(self.cto)
        r = self.client.get(self.doc.get_absolute_url())
        self.assertNotContains(r, ok)
        self.assertContains(r, "non è ancora il tuo turno")
        self.client.force_login(self.mario)
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, reverse("documents:review_withdraw", args=[self.doc.pk]))
        self.assertNotContains(r, ok)
        self.assertNotContains(r, reverse("documents:edit", args=[self.doc.pk]))
        self.assertContains(r, "è bloccato")

    def test_commento_direttamente_dal_pannello(self):
        self.avvia(self.staff)
        self.client.force_login(self.staff)
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, 'name="nota"')
        self.assertContains(r, "La tua risposta")
        # approvare con un commento dal pannello
        r = self.client.post(reverse("documents:review_decide", args=[self.doc.pk, "approva"]), {"nota": "Va bene così"}, follow=True)
        self.assertContains(r, "Hai approvato")
        passo = ReviewStep.objects.get()
        self.assertEqual((passo.decision, passo.note), (ReviewStep.Decision.APPROVED, "Va bene così"))
        self.assertContains(r, "Va bene così")  # il commento resta visibile nell'elenco

    def test_il_riquadro_di_risposta_solo_a_chi_tocca(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        self.client.force_login(self.cto)
        self.assertNotContains(self.client.get(self.doc.get_absolute_url()), "La tua risposta")
        self.client.force_login(self.mario)
        self.assertNotContains(self.client.get(self.doc.get_absolute_url()), "La tua risposta")

    def test_decisione_dalle_pagine(self):
        self.avvia(self.staff)
        self.client.force_login(self.staff)
        url = reverse("documents:review_decide", args=[self.doc.pk, "rimanda"])
        self.assertContains(self.client.get(url), "Rimanda")
        r = self.client.post(url, {"nota": ""})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.stato(), S.IN_REVIEW)
        r = self.client.post(url, {"nota": "Sistemare il capitolo 3"}, follow=True)
        self.assertContains(r, "Hai rimandato")
        self.assertEqual(self.stato(), S.DRAFT)
        # il giro è chiuso: un secondo clic avvisa senza errore
        r = self.client.post(url, {"nota": "ancora"}, follow=True)
        self.assertContains(r, "nessuna revisione in corso")

    def test_approva_dalla_pagina_e_nota_facoltativa(self):
        self.avvia(self.staff)
        self.client.force_login(self.staff)
        r = self.client.post(reverse("documents:review_decide", args=[self.doc.pk, "approva"]), {"nota": ""}, follow=True)
        self.assertContains(r, "Hai approvato")
        self.assertEqual(self.stato(), S.APPROVED)

    def test_403_404_e_esiti_inventati(self):
        self.avvia(self.staff)
        url = reverse("documents:review_decide", args=[self.doc.pk, "approva"])
        for u in (self.mario, self.luisa, self.admin):
            self.client.force_login(u)
            self.assertEqual(self.client.get(url).status_code, 403, u.username)
            self.assertEqual(self.client.post(url).status_code, 403, u.username)
        self.client.force_login(self.estraneo)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("documents:review_decide", args=[self.doc.pk, "boh"])).status_code, 404)
        self.assertEqual(self.stato(), S.IN_REVIEW)

    def test_ritiro_dalla_pagina(self):
        self.avvia()
        url = reverse("documents:review_withdraw", args=[self.doc.pk])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(url, {"nota": ""}).status_code, 422)
        r = self.client.post(url, {"nota": "ripensamento"}, follow=True)
        self.assertContains(r, "Revisione ritirata")
        self.assertEqual(self.stato(), S.DRAFT)

    def test_get_non_cambia_nulla(self):
        self.avvia()
        self.client.force_login(self.staff)
        self.client.get(reverse("documents:review_decide", args=[self.doc.pk, "approva"]))
        self.assertEqual(ReviewStep.objects.filter(decision=ReviewStep.Decision.PENDING).count(), 1)

    def test_transizioni_manuali_di_revisione_non_esistono_piu(self):
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:transition", args=[self.doc.pk, S.IN_REVIEW]), follow=True)
        self.assertContains(r, "stato del documento è cambiato")

    def test_viste_di_modifica_rimandano_al_dettaglio_se_bloccato(self):
        self.avvia()
        self.client.force_login(self.mario)
        for nome, args in [("edit", [self.doc.pk]), ("version_create", [self.doc.pk]), ("version_restore", [self.doc.pk, 1])]:
            r = self.client.get(reverse(f"documents:{nome}", args=args), follow=True)
            self.assertContains(r, "non si può modificare")
        self.client.post(reverse("documents:edit", args=[self.doc.pk]), {"title": "hack"}, follow=True)
        self.assertEqual(self.fresco().title, "Contratto")

    def test_filtro_per_stato_e_dashboard(self):
        self.nuovo_documento(owner=self.mario, titolo="Altro doc", uploaded_file=self.file(contenuto=b"a"))
        self.avvia(self.staff, self.cto, sequenziale=True)
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:list"), {"stato": "in_review"})
        self.assertContains(r, "Contratto")
        self.assertNotContains(r, "Altro doc")
        self.assertEqual(self.client.get(reverse("documents:list"), {"stato": "boh"}).status_code, 200)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("core:dashboard")), "Tocca a te (1)")
        self.client.force_login(self.cto)  # sequenza: non è ancora il suo turno
        self.assertNotContains(self.client.get(reverse("core:dashboard")), "Tocca a te")
        self.approva(self.staff)
        self.assertContains(self.client.get(reverse("core:dashboard")), "Tocca a te (1)")
        self.client.force_login(self.luisa)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), "Tocca a te")

    def test_storico_dei_giri_nel_dettaglio(self):
        self.avvia(self.staff)
        self.rimanda(self.staff, "Motivo uno")
        self.avvia(self.cto)
        self.client.force_login(self.mario)
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, "Giri precedenti (1)")
        self.assertContains(r, "giro 2")


class AuditTests(WFBase):
    def azioni(self, **filtri):
        return list(AuditLog.objects.filter(**filtri).values_list("action", flat=True).order_by("id"))

    def test_creazione_registrata(self):
        voce = AuditLog.objects.get(action="document.created")
        self.assertEqual((voce.user, voce.username, voce.object_type, voce.object_id), (self.mario, "mario", "document", self.doc.pk))
        self.assertEqual(voce.metadata["file"], "relazione.pdf")

    def test_cambio_di_stato_con_dettagli(self):
        self.avvia()
        v = AuditLog.objects.get(action="document.status_changed")
        self.assertEqual(v.metadata, {"da": "draft", "a": "in_review", "nota": ""})

    def test_operazioni_di_documento(self):
        update_document(user=self.mario, document=self.doc, title="Nuovo titolo")
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"), comment="c")
        restore_version(user=self.mario, document=self.doc, version=self.doc.versions.get(version_number=1))
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=self.admin)
        elenco = self.azioni(object_id=self.doc.pk)
        for a in ("document.updated", "version.added", "version.restored", "share.granted"):
            self.assertIn(a, elenco)
        mod = AuditLog.objects.get(action="document.updated").metadata["modifiche"]
        self.assertEqual(mod, {"titolo": ["Contratto", "Nuovo titolo"]})

    def test_nessuna_modifica_nessuna_voce(self):
        update_document(user=self.mario, document=self.doc, title="Contratto")
        self.assertNotIn("document.updated", self.azioni())

    def test_eliminazione_resta_nel_registro_anche_dopo(self):
        from documents.services import delete_document

        pk = self.doc.pk
        delete_document(user=self.mario, document=self.doc)
        v = AuditLog.objects.get(action="document.deleted")
        self.assertEqual((v.object_id, v.object_repr), (pk, "Contratto"))

    def test_download_registrato(self):
        self.client.force_login(self.luisa)
        self.client.get(reverse("documents:download", args=[self.doc.pk]))
        self.client.get(reverse("documents:version_download", args=[self.doc.pk, 1]))
        v = AuditLog.objects.filter(action="document.downloaded")
        self.assertEqual(v.count(), 2)
        self.assertEqual(v.first().username, "luisa")

    def test_download_negato_non_viene_registrato(self):
        self.client.force_login(self.estraneo)
        self.client.get(reverse("documents:download", args=[self.doc.pk]))
        self.assertFalse(AuditLog.objects.filter(action="document.downloaded").exists())

    def test_accessi(self):
        self.client.post(reverse("accounts:login"), {"username": "mario", "password": "password-lunga-123"})
        self.client.post(reverse("accounts:logout"))
        self.client.post(reverse("accounts:login"), {"username": "mario", "password": "SBAGLIATA-segreta"})
        self.assertEqual(self.azioni(action__startswith="auth."), ["auth.login", "auth.logout", "auth.login_failed"])
        fallito = AuditLog.objects.get(action="auth.login_failed")
        self.assertEqual(fallito.username, "mario")
        self.assertNotIn("SBAGLIATA", str(fallito.metadata) + fallito.username)

    def test_ip_dalla_richiesta_e_xff_ignorato_di_default(self):
        self.client.force_login(self.luisa)
        self.client.get(
            reverse("documents:download", args=[self.doc.pk]), REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="6.6.6.6"
        )
        self.assertEqual(AuditLog.objects.get(action="document.downloaded").ip_address, "10.1.2.3")

    @override_settings(AUDIT_TRUST_X_FORWARDED_FOR=True)
    def test_xff_solo_se_fidato_e_solo_se_valido(self):
        self.client.force_login(self.luisa)
        self.client.get(reverse("documents:download", args=[self.doc.pk]), HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1")
        self.client.get(reverse("documents:download", args=[self.doc.pk]), REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="non-un-ip")
        ip = list(AuditLog.objects.filter(action="document.downloaded").values_list("ip_address", flat=True).order_by("id"))
        self.assertEqual(ip, ["203.0.113.7", None])

    def test_storico_del_documento_solo_admin(self):
        url = reverse("documents:history", args=[self.doc.pk])
        self.client.force_login(self.luisa)  # modifica, non amministra
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.mario)
        r = self.client.get(url)
        self.assertContains(r, "Documento creato")
        self.assertContains(r, "relazione.pdf")

    def test_registro_solo_superutenti_e_filtro(self):
        self.avvia()
        url = reverse("core:registro")
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        r = self.client.get(url)
        self.assertContains(r, "Cambio di stato")
        r = self.client.get(url, {"gruppo": "share."})
        self.assertNotContains(r, "Cambio di stato")
        self.assertEqual(self.client.get(url, {"gruppo": "boh", "page": "zz"}).status_code, 200)

    def test_il_registro_non_ha_modifiche_nell_admin(self):
        from django.contrib import admin

        from core.models import AuditLog as M

        self.assertIn(M, admin.site._registry)
        ma = admin.site._registry[M]
        self.client.force_login(self.admin)
        self.assertFalse(ma.has_add_permission(None))
        self.assertFalse(ma.has_change_permission(None))
        self.assertFalse(ma.has_delete_permission(None))



class NotificheTests(WFBase):
    def testi(self, **filtri):
        return list(Notification.objects.filter(**filtri).values_list("recipient__username", flat=True))

    def test_modalita_libera_avvisa_tutti_subito(self):
        self.avvia(self.staff, self.cto)
        self.assertEqual(sorted(self.testi(kind="review.requested")), ["cto", "staff"])

    def test_modalita_sequenziale_avvisa_solo_chi_e_di_turno_poi_il_successivo(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        self.assertEqual(self.testi(kind="review.requested"), ["staff"])
        self.approva(self.staff)
        self.assertEqual(sorted(self.testi(kind="review.requested")), ["cto", "staff"])
        n = Notification.objects.get(kind="review.requested", recipient=self.cto)
        self.assertIn("tocca a te", n.message)
        self.assertEqual(n.url, self.doc.get_absolute_url())

    def test_approvazione_parziale_in_modalita_libera_avvisa_il_proprietario(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        n = Notification.objects.get(kind="review.progress")
        self.assertEqual(n.recipient, self.mario)
        self.assertIn("ha approvato «Contratto»", n.message)

    def test_dopo_la_risposta_la_richiesta_non_resta_da_leggere(self):
        self.avvia(self.staff, self.cto)
        self.assertEqual(Notification.objects.filter(recipient=self.staff, read_at__isnull=True).count(), 1)
        self.approva(self.staff)
        self.assertEqual(Notification.objects.filter(recipient=self.staff, read_at__isnull=True).count(), 0)
        # l'altro revisore non ha ancora risposto: la sua richiesta resta
        self.assertEqual(Notification.objects.filter(recipient=self.cto, kind="review.requested", read_at__isnull=True).count(), 1)

    def test_giro_chiuso_spegne_le_richieste_pendenti_ma_non_gli_esiti(self):
        self.avvia(self.staff, self.cto)
        self.rimanda(self.staff)
        self.assertFalse(Notification.objects.filter(kind="review.requested", read_at__isnull=True).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.cto, kind="review.closed", read_at__isnull=True).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.mario, kind="review.closed", read_at__isnull=True).exists())

    def test_ritiro_spegne_le_richieste(self):
        self.avvia(self.staff)
        withdraw_review(user=self.mario, document=self.fresco(), note="Stop")
        self.assertFalse(Notification.objects.filter(kind="review.requested", read_at__isnull=True).exists())

    def test_chiusura_positiva_avvisa_proprietario_e_partecipanti(self):
        self.avvia(self.staff, self.cto)
        self.approva(self.staff)
        self.approva(self.cto)
        self.assertEqual(sorted(self.testi(kind="review.closed")), ["mario", "staff"])  # non chi ha agito

    def test_rifiuto_avvisa_tutti_con_la_nota(self):
        self.avvia(self.staff, self.cto)
        self.rimanda(self.staff, "Manca l'allegato")
        destinatari = sorted(self.testi(kind="review.closed"))
        self.assertEqual(destinatari, ["cto", "mario"])
        for n in Notification.objects.filter(kind="review.closed"):
            self.assertIn("Manca l'allegato", n.message)
            self.assertIn("rimandato", n.message)

    def test_ritiro_avvisa_i_revisori(self):
        self.avvia(self.staff, self.cto)
        withdraw_review(user=self.mario, document=self.fresco(), note="Stop")
        self.assertEqual(sorted(self.testi(kind="review.closed")), ["cto", "staff"])

    def test_condivisione_avvisa_il_destinatario_o_il_gruppo(self):
        nuovo = User.objects.create_user("nuovo", password="password-lunga-123")
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=nuovo)
        self.assertEqual(Notification.objects.get(recipient=nuovo).kind, "document.shared")
        g = Group.objects.create(name="Legale")
        m1 = User.objects.create_user("m1", password="password-lunga-123")
        m2 = User.objects.create_user("m2", password="password-lunga-123", is_active=False)
        m1.groups.add(g)
        m2.groups.add(g)
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_group=g)
        self.assertTrue(Notification.objects.filter(recipient=m1).exists())
        self.assertFalse(Notification.objects.filter(recipient=m2).exists())  # inattivo

    def test_modifica_dell_accesso_avvisa_ma_la_riconferma_no(self):
        nuovo = User.objects.create_user("nuovo", password="password-lunga-123")
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=nuovo)
        share_document(user=self.mario, document=self.doc, level=L.READ, grantee_user=nuovo)  # identico: niente
        self.assertEqual(Notification.objects.filter(recipient=nuovo).count(), 1)
        self.assertEqual(AuditLog.objects.filter(action__startswith="share.", object_id=self.doc.pk).count(), 1)
        share_document(user=self.mario, document=self.doc, level=L.WRITE, grantee_user=nuovo)
        ultima = Notification.objects.filter(recipient=nuovo).first()
        self.assertEqual(Notification.objects.filter(recipient=nuovo).count(), 2)
        self.assertIn("ha modificato il tuo accesso", ultima.message)

    def test_badge_pagina_e_lettura(self):
        self.avvia()
        n = Notification.objects.get(recipient=self.staff)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("core:dashboard")), 'aria-label="Notifiche: 1 non letta"')
        self.assertContains(self.client.get(reverse("core:notifiche")), "ti chiede di rivedere")
        r = self.client.post(reverse("core:notifica_apri", args=[n.pk]))
        self.assertRedirects(r, self.doc.get_absolute_url(), fetch_redirect_response=False)
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), "non lett")

    def test_contatore_htmx(self):
        self.avvia()
        self.client.force_login(self.staff)
        r = self.client.get(reverse("core:notifiche_contatore"), HTTP_HX_REQUEST="true")
        self.assertContains(r, "notifiche-badge")
        self.assertNotContains(r, "<html")

    def test_non_si_aprono_le_notifiche_altrui(self):
        self.avvia()
        n = Notification.objects.get(recipient=self.staff)
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.post(reverse("core:notifica_apri", args=[n.pk])).status_code, 404)
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

    def test_indirizzo_esterno_non_viene_seguito(self):
        for url in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)"):
            n = Notification.objects.create(recipient=self.luisa, kind="x", message="m", url=url)
            self.client.force_login(self.luisa)
            r = self.client.post(reverse("core:notifica_apri", args=[n.pk]))
            self.assertRedirects(r, reverse("core:notifiche"), fetch_redirect_response=False)

    def test_segna_tutte_come_lette(self):
        self.avvia()
        self.client.force_login(self.staff)
        self.client.post(reverse("core:notifiche_segna_lette"))
        self.assertFalse(Notification.objects.filter(recipient=self.staff, read_at__isnull=True).exists())

    def test_get_sulle_azioni_di_scrittura_non_ammesso(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("core:notifiche_segna_lette")).status_code, 405)
        self.assertEqual(self.client.get(reverse("core:notifica_apri", args=[1])).status_code, 405)


class AuditRevisioneTests(WFBase):
    def test_il_giro_lascia_le_sue_tracce(self):
        self.avvia(self.staff, self.cto, sequenziale=True)
        self.approva(self.staff)
        self.rimanda(self.cto, "No")
        azioni = list(AuditLog.objects.filter(action__startswith="review.").values_list("action", flat=True).order_by("id"))
        self.assertEqual(azioni, ["review.started", "review.approved", "review.rejected"])
        v = AuditLog.objects.get(action="review.started")
        self.assertEqual(v.metadata["revisori"], ["staff", "cto"])
        self.assertTrue(v.metadata["sequenziale"])
        stati = [(a.metadata["da"], a.metadata["a"]) for a in AuditLog.objects.filter(action="document.status_changed").order_by("id")]
        self.assertEqual(stati, [("draft", "in_review"), ("in_review", "draft")])

    def test_registro_leggibile_nella_pagina_attivita(self):
        self.avvia(self.staff)
        self.rimanda(self.staff, "Motivo X")
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:history", args=[self.doc.pk]))
        self.assertContains(r, "Revisione avviata")
        self.assertContains(r, "Motivo X")
