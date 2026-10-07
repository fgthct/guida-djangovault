// JavaScript minimo dell'applicazione (nessun inline: la CSP lo vieta).
// Ordine dei livelli del progetto: Django template -> HTMX -> Alpine -> JS custom.

// Componenti Alpine (build CSP: niente espressioni complesse negli attributi HTML,
// la logica sta qui e nei template si usa solo x-data="nome").
document.addEventListener("alpine:init", () => {
  Alpine.data("menu", () => ({
    open: false,
    toggle() {
      this.open = !this.open;
    },
    close() {
      this.open = false;
    },
  }));

  // Anteprima di una pagina di documento: l'immagine è su sfondo bianco, quindi in tema
  // scuro la si inverte (classe definita in frontend/css/app.css). Con «Colori originali» no.
  Alpine.data("anteprima", () => ({
    originale: false,
    get classe() {
      return this.originale ? "" : "anteprima-scura";
    },
  }));
});

// Errori HTMX (403, 404, 500, rete): htmx non sostituisce il contenuto, quindi
// avvisiamo l'utente in modo esplicito. textContent evita qualunque iniezione HTML.
function mostraAvviso(testo) {
  const zona = document.getElementById("avvisi");
  if (!zona) return;
  const riquadro = document.createElement("div");
  riquadro.setAttribute("role", "alert");
  riquadro.className =
    "rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300";
  riquadro.textContent = testo;
  zona.replaceChildren(riquadro);
  setTimeout(() => riquadro.remove(), 8000);
}

// 422 (form non valido) e 409 (conflitto, es. categoria in uso) contengono HTML utile
// da mostrare: htmx li scarterebbe come errori, quindi chiediamo di sostituirli.
document.body.addEventListener("htmx:beforeSwap", (evento) => {
  const stato = evento.detail.xhr.status;
  if (stato === 422 || stato === 409) {
    evento.detail.shouldSwap = true;
    evento.detail.isError = false;
  }
});

document.body.addEventListener("htmx:responseError", (evento) => {
  const stato = evento.detail.xhr.status;
  const messaggi = {
    403: "Non hai i permessi per questa operazione.",
    404: "Risorsa non trovata.",
    409: "Conflitto: la risorsa è stata modificata da qualcun altro.",
  };
  mostraAvviso(messaggi[stato] || `Errore del server (${stato}). Riprova tra poco.`);
});

document.body.addEventListener("htmx:sendError", () => {
  mostraAvviso("Server non raggiungibile. Controlla la connessione.");
});
