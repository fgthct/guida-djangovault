// Ricerca rapida (Ctrl+K / Cmd+K, oppure il pulsante in alto): niente HTML dal server, solo JSON
// costruito con textContent, quindi nessuna iniezione possibile.
(() => {
  const dialogo = document.getElementById("ricerca-rapida");
  if (!dialogo || typeof dialogo.showModal !== "function") return;
  const campo = document.getElementById("rr-campo");
  const lista = document.getElementById("rr-risultati");
  const tutti = document.getElementById("rr-tutti");
  const vuoto = document.getElementById("rr-vuoto");
  const indirizzo = dialogo.dataset.indirizzo;
  const elenco = dialogo.dataset.elenco;
  let scelto = -1;
  let timer = null;
  let richiesta = 0;

  const voci = () => Array.from(lista.querySelectorAll("a"));

  function evidenzia(i) {
    const v = voci();
    if (!v.length) { scelto = -1; return; }
    scelto = (i + v.length) % v.length;
    v.forEach((a, n) => {
      a.classList.toggle("bg-surface-2", n === scelto);
      a.setAttribute("aria-selected", n === scelto ? "true" : "false");
    });
    v[scelto].scrollIntoView({ block: "nearest" });
  }

  function mostra(dati) {
    lista.replaceChildren();
    for (const r of dati.risultati) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = r.url;
      a.setAttribute("role", "option");
      a.className = "flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm text-fg hover:bg-surface-2";
      const titolo = document.createElement("span");
      titolo.className = "min-w-0 truncate";
      titolo.textContent = r.titolo;
      const cat = document.createElement("span");
      cat.className = "shrink-0 text-xs " + (r.categoria ? "text-indigo-300" : "text-muted");
      cat.textContent = r.categoria || "Senza categoria";
      a.append(titolo, cat);
      li.append(a);
      lista.append(li);
    }
    vuoto.hidden = dati.risultati.length > 0;
    tutti.hidden = !dati.q;
    tutti.href = elenco + "?q=" + encodeURIComponent(dati.q);
    evidenzia(0);
  }

  async function cerca() {
    const mia = ++richiesta;
    try {
      const r = await fetch(indirizzo + "?q=" + encodeURIComponent(campo.value), { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      const dati = await r.json();
      if (mia === richiesta) mostra(dati);
    } catch (e) {
      if (mia === richiesta) {
        lista.replaceChildren();
        vuoto.hidden = false;
        vuoto.textContent = "Ricerca non disponibile ora.";
      }
    }
  }

  function apri() {
    if (!dialogo.open) dialogo.showModal();
    campo.select();
    vuoto.textContent = "Nessun documento trovato.";
    cerca();
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      apri();
    }
  });
  document.querySelectorAll("[data-apri-ricerca]").forEach((b) => b.addEventListener("click", apri));
  dialogo.addEventListener("click", (e) => { if (e.target === dialogo) dialogo.close(); });
  campo.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(cerca, 250); });
  campo.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); evidenzia(scelto + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); evidenzia(scelto - 1); }
    else if (e.key === "Enter") {
      const v = voci();
      e.preventDefault();
      if (v[scelto]) window.location.href = v[scelto].href;
      else if (campo.value.trim()) window.location.href = elenco + "?q=" + encodeURIComponent(campo.value.trim());
    }
  });
})();
