"""Finto «Claude» per i test: nessuna chiamata di rete, risposte deterministiche e ispezionabili."""

from assistant.llm import AssistenteNonDisponibile, Classificazione, Piano


class FakeLLM:
    """Registra ciò che riceve (in `FakeLLM.chiamate`) e risponde citando la prima fonte."""

    chiamate: list[dict] = []
    varianti_da_dare: list[str] = []
    termini_da_dare: list[str] = []
    errore: str | None = None
    risposta: str | None = None
    classificazione_da_dare: "Classificazione | None" = None
    uso = {"input": 1200, "output": 80, "stop": "end_turn"}
    stop = None

    @classmethod
    def reset(cls):
        cls.chiamate = []
        cls.varianti_da_dare = []
        cls.termini_da_dare = []
        cls.errore = None
        cls.risposta = None
        cls.stop = None
        cls.classificazione_da_dare = None

    def classifica(self, *, titolo, nome_file, estratto, categorie):
        FakeLLM.chiamate.append(
            {"tipo": "classifica", "titolo": titolo, "nome_file": nome_file, "estratto": estratto, "categorie": categorie}
        )
        return FakeLLM.classificazione_da_dare or Classificazione()

    def piano(self, domanda, storia):
        FakeLLM.chiamate.append({"tipo": "piano", "domanda": domanda, "storia": storia})
        return Piano(query=list(FakeLLM.varianti_da_dare), termini=list(FakeLLM.termini_da_dare))

    def stream(self, *, system, messages):
        FakeLLM.chiamate.append({"tipo": "risposta", "system": system, "messages": messages})
        if FakeLLM.errore:
            raise AssistenteNonDisponibile(FakeLLM.errore)
        testo = FakeLLM.risposta or "Secondo il documento la scadenza è il 30 giugno [1]. **Attenzione**: <b>x</b>"
        for i in range(0, len(testo), 12):
            yield ("testo", testo[i : i + 12])
        yield ("fine", {**FakeLLM.uso, "stop": FakeLLM.stop or FakeLLM.uso["stop"]})
