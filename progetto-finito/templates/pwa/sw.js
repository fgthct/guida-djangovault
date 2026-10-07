/* Service worker di DjangoVault. versione: {{ versione }}
 *
 * Fa tre cose, e solo queste:
 *  1. mette da parte CSS, script e icone (/static/) per aprire l'app più in fretta;
 *  2. se il server non risponde, mostra la pagina «sei offline» invece dell'errore del browser;
 *  3. riceve i file condivisi dal telefono («Condividi → DjangoVault») e li passa alla pagina di caricamento.
 *
 * NON mette mai da parte pagine, documenti, risposte dell'assistente o altro che venga dal server: sono dati riservati e
 * un telefono si perde. Cache e service worker non contengono nulla che serva a entrare.
 */
const VERSIONE = "{{ versione }}";
const CACHE_STATICI = "dv-statici-" + VERSIONE;
const CACHE_CONDIVISO = "dv-condiviso";
const PAGINA_OFFLINE = "{% url 'core:offline' %}";
const DA_SUBITO = [PAGINA_OFFLINE, "{% load assets %}{% asset 'css/app.css' %}"];
const PAGINA_CARICA = "{% url 'documents:upload' %}";
const PERCORSO_CONDIVISIONE = "{% url 'core:condividi' %}";

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    caches.open(CACHE_STATICI).then((c) => c.addAll(DA_SUBITO)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((nomi) => Promise.all(nomi.filter((n) => n.startsWith("dv-statici-") && n !== CACHE_STATICI).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

// --- Condivisione dal telefono -------------------------------------------------------------------------------
// Il sistema fa un POST multipart a /condividi/. Qui non c'è il token CSRF e non serve: i file non vanno al server in
// questo passaggio, si conservano nel browser e li carica la pagina «Carica più file» (con il token e la sessione).
async function riceviCondivisione(richiesta) {
  try {
    const dati = await richiesta.formData();
    const cache = await caches.open(CACHE_CONDIVISO);
    for (const chiave of await cache.keys()) await cache.delete(chiave); // una condivisione alla volta
    let n = 0;
    for (const f of dati.getAll("file")) {
      if (!(f instanceof File) || !f.size) continue;
      await cache.put("/__condiviso/" + n, new Response(f, {
        headers: { "Content-Type": f.type || "application/octet-stream", "X-Nome": encodeURIComponent(f.name || "file") },
      }));
      n += 1;
    }
    // Solo testo o indirizzo (per esempio «Condividi» da una pagina web): diventa una nota di testo.
    const testo = [dati.get("title"), dati.get("url"), dati.get("text")].filter((x) => x && String(x).trim()).join("\n\n");
    if (!n && testo) {
      const titolo = String(dati.get("title") || "Nota condivisa").replace(/[\\/:*?"<>|\n\r]+/g, " ").trim().slice(0, 80) || "Nota condivisa";
      await cache.put("/__condiviso/0", new Response(testo + "\n", {
        headers: { "Content-Type": "text/plain; charset=utf-8", "X-Nome": encodeURIComponent(titolo + ".txt") },
      }));
      n = 1;
    }
    return Response.redirect(PAGINA_CARICA + "?condiviso=" + n, 303);
  } catch (errore) {
    return Response.redirect(PAGINA_CARICA, 303);
  }
}

self.addEventListener("fetch", (evento) => {
  const r = evento.request;
  const url = new URL(r.url);
  if (url.origin !== self.location.origin) return;

  if (r.method === "POST" && url.pathname === PERCORSO_CONDIVISIONE) {
    evento.respondWith(riceviCondivisione(r));
    return;
  }
  if (r.method !== "GET") return;

  // File statici: dalla cache, aggiornati in sottofondo (stale-while-revalidate).
  if (url.pathname.startsWith("/static/")) {
    evento.respondWith(
      caches.open(CACHE_STATICI).then(async (cache) => {
        const trovato = await cache.match(r);
        const rete = fetch(r).then((risposta) => {
          if (risposta.ok) cache.put(r, risposta.clone());
          return risposta;
        }).catch(() => trovato);
        return trovato || rete;
      })
    );
    return;
  }

  // Pagine: sempre dal server. Se non risponde, la pagina «offline» (il resto non si conserva mai).
  if (r.mode === "navigate") {
    evento.respondWith(
      fetch(r).catch(() => caches.open(CACHE_STATICI).then((c) => c.match(PAGINA_OFFLINE)))
    );
  }
});
