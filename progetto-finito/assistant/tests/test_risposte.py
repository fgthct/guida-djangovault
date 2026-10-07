"""Generazione delle risposte (servizi), rendering e client Anthropic."""

from datetime import timedelta

import httpx
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from assistant import rendering, services
from assistant.llm import AssistenteNonDisponibile, AnthropicLLM, Piano, leggi_piano
from assistant.models import Conversation, Message
from core.models import AuditLog

from .base import AssistenteBase
from .fakes import FakeLLM


class GenerazioneTests(AssistenteBase):
    def setUp(self):
        super().setUp()
        self.doc("Contratto", "Il contratto di locazione scade il 30 giugno.")

    def nuova(self, domanda="Quando scade la locazione?", utente=None, documento=None):
        c = services.nuova_conversazione(utente=utente or self.mario, domanda=domanda, documento=documento)
        return c, c.messages.get(role="assistant")

    def esegui(self, msg, utente=None):
        return list(services.genera(msg.pk, utente or self.mario))

    def test_risposta_completa(self):
        c, msg = self.nuova()
        eventi = self.esegui(msg)
        self.assertEqual(eventi[0], ("stato", "Cerco nei documenti…"))
        self.assertEqual(eventi[-1], ("fine", None))
        msg.refresh_from_db()
        self.assertEqual(msg.status, "ok")
        self.assertIn("30 giugno [1]", msg.content)
        self.assertEqual(msg.sources[0]["title"], "Contratto")
        self.assertEqual((msg.input_tokens, msg.output_tokens), (1200, 80))

    def test_il_modello_riceve_le_fonti_e_la_domanda(self):
        c, msg = self.nuova()
        self.esegui(msg)
        r = self.chiamate("risposta")[0]
        self.assertIn("locazione scade il 30 giugno", r["messages"][-1]["content"])
        self.assertIn("Domanda: Quando scade la locazione?", r["messages"][-1]["content"])
        self.assertEqual(r["system"][:20], "Sei l'assistente di ")

    def test_registro_con_documenti_usati_e_senza_testo_della_domanda(self):
        c, msg = self.nuova("Domanda riservata sul contratto di locazione")
        self.esegui(msg)
        voce = AuditLog.objects.get(action="ai.question")
        self.assertEqual(voce.user, self.mario)
        self.assertEqual(voce.metadata["documenti"], ["Contratto"])
        self.assertNotIn("riservata", str(voce.metadata))

    def test_senza_passaggi_non_si_chiama_il_modello(self):
        c, msg = self.nuova("ricetta della carbonara")
        self.esegui(msg)
        msg.refresh_from_db()
        self.assertEqual(msg.status, "ok")
        self.assertIn("Non ho trovato", msg.content)
        self.assertEqual(self.chiamate("risposta"), [])
        self.assertFalse(AuditLog.objects.filter(action="ai.question").exists())

    def test_nessun_documento_consentito_lo_dice(self):
        d = self.mario.documents.get()
        d.ai_allowed = False
        d.save()
        c, msg = self.nuova()
        self.esegui(msg)
        msg.refresh_from_db()
        self.assertIn("nessun documento", msg.content.lower())

    def test_errore_del_servizio_si_salva_e_si_mostra(self):
        FakeLLM.errore = "Troppe richieste in questo momento: riprova fra poco."
        c, msg = self.nuova()
        eventi = self.esegui(msg)
        self.assertEqual(eventi[-1], ("errore", FakeLLM.errore))
        msg.refresh_from_db()
        self.assertEqual((msg.status, msg.error), ("errore", FakeLLM.errore))

    def test_errore_imprevisto_non_mostra_i_dettagli(self):
        c, msg = self.nuova()
        FakeLLM.risposta = None
        original = FakeLLM.stream
        FakeLLM.stream = lambda self, **kw: (_ for _ in ()).throw(RuntimeError("segreto interno"))
        try:
            with self.assertLogs("assistant.services", "ERROR"):
                eventi = self.esegui(msg)
        finally:
            FakeLLM.stream = original
        self.assertNotIn("segreto", str(eventi))
        msg.refresh_from_db()
        self.assertEqual(msg.status, "errore")

    def test_una_sola_generazione_per_messaggio(self):
        c, msg = self.nuova()
        self.esegui(msg)
        self.assertEqual(self.esegui(msg), [("altrove", None)])  # già finito
        self.assertEqual(len(self.chiamate("risposta")), 1)

    def test_non_si_genera_la_risposta_di_un_altro(self):
        c, msg = self.nuova()
        self.assertEqual(self.esegui(msg, utente=self.luisa), [("altrove", None)])
        msg.refresh_from_db()
        self.assertEqual(msg.status, "attesa")

    def test_chiusura_della_connessione_salva_l_interruzione(self):
        c, msg = self.nuova()
        gen = services.genera(msg.pk, self.mario)
        next(gen)
        gen.close()
        msg.refresh_from_db()
        self.assertEqual(msg.status, "errore")

    def test_risposta_troncata_lo_segnala(self):
        FakeLLM.stop = "max_tokens"
        c, msg = self.nuova()
        self.esegui(msg)
        msg.refresh_from_db()
        self.assertIn("interrotta perché troppo lunga", msg.content)

    def test_domanda_successiva_porta_la_storia_senza_citazioni(self):
        c, msg = self.nuova()
        self.esegui(msg)
        _, msg2 = services.fai_domanda(conversazione=c, domanda="E il canone?")
        FakeLLM.chiamate.clear()
        FakeLLM.varianti_da_dare = ["canone del contratto di locazione"]  # la riformulazione risolve «E il canone?»
        self.esegui(msg2)
        r = self.chiamate("risposta")[0]["messages"]
        self.assertEqual([m["role"] for m in r], ["user", "assistant", "user"])
        self.assertNotIn("[1]", r[1]["content"])
        self.assertEqual(self.chiamate("piano")[0]["storia"][0]["content"], "Quando scade la locazione?")

    def test_domanda_su_documento_che_perde_il_consenso(self):
        d = self.mario.documents.get()
        c, msg = self.nuova(documento=d)
        d.ai_allowed = False
        d.save()
        eventi = self.esegui(msg)
        self.assertEqual(eventi[-1][0], "errore")
        self.assertEqual(self.chiamate("risposta"), [])

    def test_senza_chiave_niente_chiamate(self):
        c, msg = self.nuova()
        with override_settings(ANTHROPIC_API_KEY=""):
            eventi = self.esegui(msg)
        self.assertEqual(eventi[-1][0], "errore")
        self.assertEqual(self.chiamate("piano"), [])

    def test_generazione_orfana_scade(self):
        c, msg = self.nuova()
        Message.objects.filter(pk=msg.pk).update(status="generando", started_at=timezone.now() - timedelta(minutes=10))
        services._scadute()
        msg.refresh_from_db()
        self.assertEqual(msg.status, "errore")


class LimitiTests(AssistenteBase):
    def test_domanda_vuota_o_troppo_lunga(self):
        for testo in ["", "   ", "x" * 1001]:
            with self.assertRaises(services.DomandaNonValida):
                services.nuova_conversazione(utente=self.mario, domanda=testo)

    def test_limite_orario(self):
        with override_settings(AI_MAX_QUESTIONS_PER_HOUR=2):
            services.nuova_conversazione(utente=self.mario, domanda="uno")
            services.nuova_conversazione(utente=self.mario, domanda="due")
            with self.assertRaises(services.DomandaNonValida):
                services.nuova_conversazione(utente=self.mario, domanda="tre")
            services.nuova_conversazione(utente=self.luisa, domanda="altro utente")  # il limite è per utente

    def test_una_domanda_alla_volta(self):
        c = services.nuova_conversazione(utente=self.mario, domanda="uno")
        with self.assertRaises(services.DomandaNonValida):
            services.fai_domanda(conversazione=c, domanda="due")

    def test_documento_non_consentito_non_si_puo_interrogare(self):
        d = self.doc("Privato", "testo", consenso=False)
        with self.assertRaises(services.DomandaNonValida):
            services.nuova_conversazione(utente=self.mario, domanda="?", documento=d)


class RenderingTests(SimpleTestCase):
    def test_html_del_modello_viene_neutralizzato(self):
        h = str(rendering.risposta_html('<script>alert(1)</script> **ok** <img src=x onerror=y>', 1, set()))
        self.assertNotIn("<script", h)
        self.assertNotIn("<img", h)
        self.assertIn("<strong>ok</strong>", h)

    def test_citazioni_solo_se_la_fonte_esiste(self):
        h = str(rendering.risposta_html("Vero [1] falso [7]", 5, {1}))
        self.assertIn('href="#fonte-5-1"', h)
        self.assertNotIn("fonte-5-7", h)

    def test_elenchi_e_paragrafi(self):
        h = str(rendering.risposta_html("Intro\n\n- uno\n- due\n\nFine", 1, set()))
        self.assertEqual(h, "<p>Intro</p><ul><li>uno</li><li>due</li></ul><p>Fine</p>")

    def test_link_markdown_non_diventano_link(self):
        self.assertNotIn("<a ", str(rendering.risposta_html("[clic](javascript:alert(1))", 1, set())))

    def test_testo_vuoto(self):
        self.assertEqual(str(rendering.risposta_html("", 1, set())), "")


class ClientAnthropicTests(SimpleTestCase):
    def test_leggi_piano(self):
        self.assertEqual(leggi_piano('Ecco: {"query": ["a", "b", "a", "c", "d"]} fine').query, ["a", "b", "c"])
        self.assertEqual(leggi_piano("non json").query, [])
        self.assertEqual(leggi_piano('{"query": "stringa"}').query, [])
        self.assertEqual(leggi_piano('{"query": [1, null, "ok"]}').query, ["ok"])

    @override_settings(ANTHROPIC_API_KEY="k", AI_QUERY_MODEL="")
    def test_senza_modello_di_riformulazione_non_chiama_l_api(self):
        self.assertEqual(AnthropicLLM().piano("domanda", []), Piano())

    @override_settings(ANTHROPIC_API_KEY="k")
    def test_errori_dell_api_diventano_messaggi_per_l_utente(self):
        import anthropic

        richiesta = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        casi = [
            (anthropic.APIConnectionError(request=richiesta), "raggiungere"),
            (anthropic.AuthenticationError("no", response=httpx.Response(401, request=richiesta), body=None), "chiave"),
            (anthropic.RateLimitError("no", response=httpx.Response(429, request=richiesta), body=None), "Troppe richieste"),
            (anthropic.InternalServerError("no", response=httpx.Response(529, request=richiesta), body=None), "sovraccarico"),
            (anthropic.BadRequestError("credit balance is too low", response=httpx.Response(400, request=richiesta), body=None), "rifiutato"),
        ]
        for errore, atteso in casi:
            llm = AnthropicLLM()

            class Finto:
                def stream(self, **kw):
                    raise errore

            llm._client.messages = Finto()
            with self.subTest(errore=type(errore).__name__):
                with self.assertLogs("assistant.llm"):
                    with self.assertRaises(AssistenteNonDisponibile) as ctx:
                        list(llm.stream(system="s", messages=[]))
                self.assertIn(atteso, str(ctx.exception))
                self.assertNotIn("credit", str(ctx.exception))

    @override_settings(ANTHROPIC_API_KEY="k")
    def test_riformulazione_fallita_non_blocca(self):
        llm = AnthropicLLM()

        class Finto:
            def create(self, **kw):
                raise RuntimeError("giù")

        llm._client.messages = Finto()
        with self.assertLogs("assistant.llm", "WARNING"):
            self.assertEqual(llm.piano("domanda", []), Piano())


class OccorrenzeTests(AssistenteBase):
    def test_le_occorrenze_sono_salvate_e_nell_audit(self):
        self.doc("Libro Uno", "Marco Ferrante accettò il lavoro in Israele.")
        self.doc("Libro Due", "Marco Ferrante incontra un amico.")
        c = services.nuova_conversazione(utente=self.mario, domanda="In quali libri appare Marco Ferrante?")
        risposta = c.messages.get(role="assistant")
        list(services.genera(risposta.pk, self.mario))
        risposta.refresh_from_db()
        self.assertEqual(risposta.status, Message.Status.DONE)
        self.assertEqual(risposta.occurrences[0]["term"], "Marco Ferrante")
        self.assertEqual(risposta.occurrences[0]["total"], 2)
        self.assertEqual({d["title"] for d in risposta.occurrences[0]["documents"]}, {"Libro Uno", "Libro Due"})
        prompt = self.chiamate("risposta")[0]["messages"][-1]["content"]
        self.assertIn("<indice>", prompt)
        log = AuditLog.objects.filter(action="ai.question").latest("pk")
        self.assertIn("Libro Due", str(log.metadata))

    def test_leggi_piano_con_termini(self):
        p = leggi_piano('{"query": ["a"], "termini": ["Marco Ferrante", "x", 3, "Marco Ferrante", "b", "c"]}')
        self.assertEqual(p.query, ["a"])
        self.assertEqual(p.termini[0], "Marco Ferrante")
        self.assertLessEqual(len(p.termini), 3)
