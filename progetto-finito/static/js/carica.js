// Caricamento di più file/cartelle: i file vanno al server UNO alla volta (avanzamento chiaro, un file rotto non
// ferma gli altri, e due file identici nello stesso lotto vengono riconosciuti come doppioni).
// Nessun codice inline (CSP): tutto qui, con textContent al posto di innerHTML.
(function () {
  const radice = document.getElementById("carica");
  if (!radice) return;

  const url = radice.dataset.url;
  const ammesse = new Set(radice.dataset.estensioni.split(",").filter(Boolean));
  const massimo = Number(radice.dataset.maxMb) * 1024 * 1024;
  const csrf = radice.querySelector("input[name=csrfmiddlewaretoken]").value;

  const $ = (id) => document.getElementById(id);
  const zona = $("zona"), inputFile = $("input-file"), inputCartella = $("input-cartella");
  const pannello = $("pannello"), elenco = $("elenco"), conteggio = $("conteggio");
  const barra = $("barra"), fine = $("fine"), avvia = $("avvia"), svuota = $("svuota");

  let coda = []; // { file, percorso, riga, stato }
  let inCorso = false;

  const ETICHETTE = {
    attesa: ["In coda", "text-muted"],
    invio: ["Invio…", "text-indigo-300"],
    importato: ["Caricato", "text-emerald-400"],
    presente: ["Già presente", "text-amber-300"],
    saltato: ["Saltato", "text-amber-300"],
    errore: ["Errore", "text-red-400"],
  };

  function dimensione(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function motivoScarto(file, percorso) {
    const parti = percorso.split("/");
    if (parti.some((p) => p.startsWith(".") || p.startsWith("~$")) || /^(thumbs\.db|desktop\.ini)$/i.test(file.name)) return "nascosto";
    const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
    if (!ammesse.has(ext)) return "tipo non ammesso";
    if (file.size === 0) return "file vuoto";
    if (file.size > massimo) return "oltre il limite di " + radice.dataset.maxMb + " MB";
    return null;
  }

  function creaRiga(percorso, file) {
    const li = document.createElement("li");
    li.className = "flex items-center justify-between gap-3 px-4 py-2.5";
    const nome = document.createElement("span");
    nome.className = "min-w-0 flex-1 truncate";
    nome.textContent = percorso;
    nome.title = percorso;
    const peso = document.createElement("span");
    peso.className = "shrink-0 text-xs text-muted";
    peso.textContent = dimensione(file.size);
    const stato = document.createElement("span");
    stato.className = "shrink-0 text-right text-xs";
    li.append(nome, peso, stato);
    elenco.appendChild(li);
    return { li, nome, stato };
  }

  function imposta(voce, chiave, dettaglio, link) {
    const [testo, colore] = ETICHETTE[chiave];
    voce.stato = chiave;
    const s = voce.riga.stato;
    s.className = "shrink-0 text-right text-xs " + colore;
    s.textContent = testo + (dettaglio ? " · " + dettaglio : "");
    if (link) {
      const a = document.createElement("a");
      a.href = link;
      a.className = "ml-2 underline hover:text-fg";
      a.textContent = "apri";
      s.appendChild(a);
    }
  }

  function aggiungi(file, percorso) {
    percorso = percorso || file.webkitRelativePath || file.name;
    if (coda.some((v) => v.percorso === percorso && v.file.size === file.size)) return; // già in elenco
    const voce = { file, percorso, riga: creaRiga(percorso, file), stato: "attesa" };
    const motivo = motivoScarto(file, percorso);
    if (motivo) {
      imposta(voce, "saltato", motivo);
    } else {
      imposta(voce, "attesa");
    }
    coda.push(voce);
  }

  function aggiorna() {
    pannello.classList.toggle("hidden", coda.length === 0);
    const per = (s) => coda.filter((v) => v.stato === s).length;
    const attesa = per("attesa");
    conteggio.textContent =
      coda.length + " file · " + attesa + " da caricare · " + per("importato") + " caricati · " +
      per("presente") + " già presenti · " + per("saltato") + " saltati · " + per("errore") + " errori";
    const daFare = attesa + per("invio") + per("importato") + per("presente") + per("errore");
    barra.style.width = daFare ? Math.round(((daFare - attesa - per("invio")) / daFare) * 100) + "%" : "0%";
    avvia.disabled = inCorso || attesa === 0;
    avvia.textContent = inCorso ? "Caricamento…" : attesa ? "Carica " + attesa + " file" : "Carica";
    svuota.disabled = inCorso;
  }

  // --- Raccolta file da trascinamento (anche cartelle) -------------------------------------------------------
  function leggiVoce(entry, prefisso) {
    return new Promise((risolvi) => {
      if (entry.isFile) {
        entry.file((f) => { aggiungi(f, prefisso + f.name); risolvi(); }, () => risolvi());
      } else if (entry.isDirectory) {
        const lettore = entry.createReader();
        const tutte = [];
        const passo = () => lettore.readEntries(async (voci) => {
          if (!voci.length) {
            for (const v of tutte) await leggiVoce(v, prefisso + entry.name + "/");
            risolvi();
          } else { tutte.push(...voci); passo(); } // readEntries restituisce a blocchi: si ripete fino a vuoto
        }, () => risolvi());
        passo();
      } else risolvi();
    });
  }

  async function raccogli(evento) {
    const voci = evento.dataTransfer && evento.dataTransfer.items;
    if (voci && voci.length && voci[0].webkitGetAsEntry) {
      const entry = [...voci].map((v) => v.webkitGetAsEntry()).filter(Boolean); // da leggere subito: dopo l'await sparisce
      for (const e of entry) await leggiVoce(e, "");
    } else if (evento.dataTransfer) {
      [...evento.dataTransfer.files].forEach((f) => aggiungi(f));
    }
    fine.classList.add("hidden");
    aggiorna();
  }

  // --- Invio ------------------------------------------------------------------------------------------------
  function invia(voce) {
    return new Promise((risolvi) => {
      const dati = new FormData();
      dati.append("file", voce.file, voce.file.name);
      dati.append("percorso", voce.percorso);
      dati.append("categoria", $("categoria").value);
      dati.append("ai_allowed", $("ai").checked ? "1" : "0");
      const x = new XMLHttpRequest();
      x.open("POST", url);
      x.setRequestHeader("X-CSRFToken", csrf);
      x.upload.onprogress = (e) => {
        if (e.lengthComputable) voce.riga.stato.textContent = "Invio… " + Math.round((e.loaded / e.total) * 100) + "%";
      };
      x.onload = () => {
        let r = null;
        try { r = JSON.parse(x.responseText); } catch (_) { /* risposta non JSON */ }
        if (r && ETICHETTE[r.stato]) imposta(voce, r.stato, r.dettaglio, r.url);
        else imposta(voce, "errore", x.status === 403 ? "sessione scaduta: ricarica la pagina" : x.status === 413 ? "file troppo grande per il server" : "HTTP " + x.status);
        risolvi();
      };
      x.onerror = () => { imposta(voce, "errore", "server non raggiungibile"); risolvi(); };
      x.send(dati);
    });
  }

  async function avviaCoda() {
    if (inCorso) return;
    inCorso = true;
    fine.classList.add("hidden");
    aggiorna();
    for (const voce of coda.filter((v) => v.stato === "attesa")) {
      imposta(voce, "invio");
      voce.riga.li.scrollIntoView({ block: "nearest" });
      aggiorna();
      await invia(voce);
      aggiorna();
    }
    inCorso = false;
    aggiorna();
    const nuovi = coda.filter((v) => v.stato === "importato").length;
    fine.classList.remove("hidden");
    fine.textContent = "Finito: " + nuovi + " nuovi documenti. L'elaborazione (testo, OCR, indicizzazione) prosegue in background. ";
    const a = document.createElement("a");
    a.href = url.replace(/carica\/file\/$/, "");
    a.className = "text-indigo-300 hover:text-indigo-200";
    a.textContent = "Vai ai documenti →";
    fine.appendChild(a);
  }

  // --- Eventi -----------------------------------------------------------------------------------------------
  $("scegli-file").addEventListener("click", () => inputFile.click());
  $("scegli-cartella").addEventListener("click", () => inputCartella.click());
  zona.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inputFile.click(); } });
  for (const input of [inputFile, inputCartella]) {
    input.addEventListener("change", () => {
      [...input.files].forEach((f) => aggiungi(f));
      input.value = "";
      fine.classList.add("hidden");
      aggiorna();
    });
  }
  ["dragenter", "dragover"].forEach((n) => zona.addEventListener(n, (e) => {
    e.preventDefault();
    zona.classList.add("border-indigo-400", "bg-surface-2");
  }));
  ["dragleave", "drop"].forEach((n) => zona.addEventListener(n, () => zona.classList.remove("border-indigo-400", "bg-surface-2")));
  zona.addEventListener("drop", (e) => { e.preventDefault(); raccogli(e); });
  // Un file lasciato fuori dalla zona non deve far aprire il file nel browser, perdendo l'elenco.
  ["dragover", "drop"].forEach((n) => window.addEventListener(n, (e) => { if (!zona.contains(e.target)) e.preventDefault(); }));

  avvia.addEventListener("click", avviaCoda);
  svuota.addEventListener("click", () => { coda = []; elenco.replaceChildren(); fine.classList.add("hidden"); aggiorna(); });
  window.addEventListener("beforeunload", (e) => { if (inCorso) { e.preventDefault(); e.returnValue = ""; } });

  // --- File condivisi dal telefono («Condividi → DjangoVault»): li ha messi da parte il service worker ---------
  async function riprendiCondivisi() {
    if (!("caches" in window) || !/[?&]condiviso=/.test(location.search)) return;
    try {
      const cache = await caches.open("dv-condiviso");
      const chiavi = await cache.keys();
      for (const chiave of chiavi) {
        const risposta = await cache.match(chiave);
        if (!risposta) continue;
        const nome = decodeURIComponent(risposta.headers.get("X-Nome") || "file");
        aggiungi(new File([await risposta.blob()], nome, { type: risposta.headers.get("Content-Type") || "" }), nome);
        await cache.delete(chiave);
      }
      history.replaceState(null, "", location.pathname); // un aggiornamento della pagina non li ripropone
      if (chiavi.length) {
        fine.classList.remove("hidden");
        fine.textContent = "Hai condiviso " + chiavi.length + " file" + ": controlla la categoria e premi «Carica».";
      }
    } catch (_) { /* senza cache non c'è nulla da riprendere */ }
    aggiorna();
  }

  aggiorna();
  riprendiCondivisi();
})();
