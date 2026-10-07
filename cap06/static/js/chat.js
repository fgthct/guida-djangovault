// Chat con l'assistente: risposta in streaming (SSE). Nessun inline: la CSP lo vieta.
// Il testo in arrivo si inserisce con textContent (mai come HTML); a fine flusso il server rimanda la risposta già
// formattata e con le fonti, che sostituisce la bolla provvisoria.
(function () {
  function inFondo() {
    window.scrollTo({ top: document.body.scrollHeight });
  }

  function avvia(el) {
    if (el.dataset.avviato) return;
    el.dataset.avviato = "1";
    const testo = el.querySelector("[data-testo]");
    const stato = el.querySelector("[data-stato]");
    const sorgente = new EventSource(el.dataset.stream);
    let finito = false;

    function concludi() {
      if (finito) return;
      finito = true;
      sorgente.close();
      // Stato definitivo dal database (se la generazione è ancora in corso altrove, il frammento fa polling).
      htmx.ajax("GET", el.dataset.finale, { target: el, swap: "outerHTML" });
    }

    sorgente.addEventListener("stato", (e) => {
      stato.textContent = JSON.parse(e.data);
    });
    sorgente.addEventListener("testo", (e) => {
      stato.textContent = "";
      testo.textContent += JSON.parse(e.data);
      inFondo();
    });
    ["fine", "errore", "altrove"].forEach((nome) => sorgente.addEventListener(nome, concludi));
    // Connessione caduta: EventSource riproverebbe da solo (e rilancerebbe la domanda), noi no.
    sorgente.onerror = concludi;
  }

  function cerca(radice) {
    radice.querySelectorAll("[data-stream]").forEach(avvia);
    if (radice.matches && radice.matches("[data-stream]")) avvia(radice);
  }

  cerca(document);
  htmx.onLoad(cerca);

  const form = document.getElementById("form-domanda");
  if (form) {
    // Dopo l'invio la casella si svuota; con errore (422) resta il testo scritto.
    form.addEventListener("htmx:afterRequest", (e) => {
      if (e.detail.successful) {
        form.reset();
        document.getElementById("errore-domanda").replaceChildren();
      }
      inFondo();
    });
  }

  // Invio manda la domanda, Maiusc+Invio va a capo.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.target.matches("textarea[data-invio]")) {
      e.preventDefault();
      e.target.form.requestSubmit();
    }
  });
})();
