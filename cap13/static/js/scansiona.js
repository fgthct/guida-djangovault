// Scansione con la fotocamera: foto delle pagine → un PDF → caricamento normale (OCR e classificazione sul server).
// Tutto avviene nel browser; nessun codice inline (CSP), nessun innerHTML.
(function () {
  const radice = document.getElementById("scansiona");
  if (!radice || !window.DVPdf) return;

  const url = radice.dataset.url;
  const massimo = Number(radice.dataset.maxMb) * 1024 * 1024;
  const csrf = radice.querySelector("input[name=csrfmiddlewaretoken]").value;
  const $ = (id) => document.getElementById(id);
  const LATO_PDF = 2000;   // lato lungo delle pagine nel PDF (leggibile per l'OCR, file di dimensione ragionevole)
  const LATO_ANTEPRIMA = 360;
  const QUALITA = 0.8;

  let pagine = []; // { file, rotazione, li, img, url }
  let occupato = false;

  // --- Elaborazione di una foto ---------------------------------------------------------------------------------
  async function apri(file) {
    if (window.createImageBitmap) {
      try { return await createImageBitmap(file, { imageOrientation: "from-image" }); } catch (_) { /* si prova all'antica */ }
    }
    return await new Promise((ok, no) => {
      const img = new Image();
      const u = URL.createObjectURL(file);
      img.onload = () => { URL.revokeObjectURL(u); ok(img); };
      img.onerror = () => { URL.revokeObjectURL(u); no(new Error("immagine non leggibile")); };
      img.src = u;
    });
  }

  // Sfondo bianco e testo nitido: si stira il contrasto in modo che la carta (la parte più chiara e più estesa)
  // diventi bianca e l'inchiostro resti scuro. È un filtro semplice: non corregge ombre forti né la prospettiva.
  function filtroDocumento(dati) {
    const px = dati.data;
    const istogramma = new Uint32Array(256);
    for (let i = 0; i < px.length; i += 4) {
      const l = (px[i] * 77 + px[i + 1] * 151 + px[i + 2] * 28) >> 8;
      px[i] = px[i + 1] = px[i + 2] = l;
      istogramma[l] += 1;
    }
    const totale = px.length / 4;
    const percentile = (p) => { let somma = 0; for (let v = 0; v < 256; v++) { somma += istogramma[v]; if (somma >= totale * p) return v; } return 255; };
    const basso = percentile(0.01);
    const alto = Math.max(basso + 40, percentile(0.55));
    const scala = 255 / (alto - basso);
    for (let i = 0; i < px.length; i += 4) {
      const v = Math.max(0, Math.min(255, (px[i] - basso) * scala));
      px[i] = px[i + 1] = px[i + 2] = v;
    }
  }

  function filtroGrigi(dati) {
    const px = dati.data;
    for (let i = 0; i < px.length; i += 4) {
      px[i] = px[i + 1] = px[i + 2] = (px[i] * 77 + px[i + 1] * 151 + px[i + 2] * 28) >> 8;
    }
  }

  async function disegna(pagina, filtro, latoMax) {
    const sorgente = await apri(pagina.file);
    const w0 = sorgente.width, h0 = sorgente.height;
    const scala = Math.min(1, latoMax / Math.max(w0, h0));
    const w = Math.max(1, Math.round(w0 * scala)), h = Math.max(1, Math.round(h0 * scala));
    const ruotata = pagina.rotazione % 180 !== 0;
    const tela = document.createElement("canvas");
    tela.width = ruotata ? h : w;
    tela.height = ruotata ? w : h;
    const c = tela.getContext("2d", { willReadFrequently: true });
    c.translate(tela.width / 2, tela.height / 2);
    c.rotate((pagina.rotazione * Math.PI) / 180);
    c.drawImage(sorgente, -w / 2, -h / 2, w, h);
    if (sorgente.close) sorgente.close();
    if (filtro !== "colori") {
      const dati = c.getImageData(0, 0, tela.width, tela.height);
      (filtro === "documento" ? filtroDocumento : filtroGrigi)(dati);
      c.putImageData(dati, 0, 0);
    }
    return tela;
  }

  const inJpeg = (tela, qualita) => new Promise((ok, no) => tela.toBlob((b) => (b ? ok(b) : no(new Error("JPEG non creato"))), "image/jpeg", qualita));

  // --- Anteprime ------------------------------------------------------------------------------------------------
  async function anteprima(pagina) {
    try {
      const tela = await disegna(pagina, $("filtro").value, LATO_ANTEPRIMA);
      const blob = await inJpeg(tela, 0.7);
      if (pagina.url) URL.revokeObjectURL(pagina.url);
      pagina.url = URL.createObjectURL(blob);
      pagina.img.src = pagina.url;
      pagina.img.alt = "Pagina " + (pagine.indexOf(pagina) + 1);
    } catch (_) {
      pagina.img.alt = "Anteprima non disponibile";
    }
  }

  function bottone(testo, etichetta, azione) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = testo;
    b.setAttribute("aria-label", etichetta);
    b.title = etichetta;
    b.className = "rounded-md bg-black/60 px-2 py-1 text-sm text-fg hover:bg-black/80";
    b.addEventListener("click", azione);
    return b;
  }

  function creaScheda(pagina) {
    const li = document.createElement("li");
    li.className = "relative overflow-hidden rounded-xl border border-line bg-surface";
    const img = document.createElement("img");
    img.className = "aspect-[3/4] w-full object-contain bg-black/30";
    img.alt = "Anteprima";
    const numero = document.createElement("span");
    numero.className = "absolute left-2 top-2 rounded-md bg-black/60 px-2 py-0.5 text-xs";
    const barra = document.createElement("div");
    barra.className = "flex items-center justify-between gap-1 p-2";
    barra.append(
      bottone("◀", "Sposta prima", () => sposta(pagina, -1)),
      bottone("↻", "Ruota di 90 gradi", () => ruota(pagina)),
      bottone("▶", "Sposta dopo", () => sposta(pagina, 1)),
      bottone("✕", "Togli questa pagina", () => togli(pagina)),
    );
    li.append(img, numero, barra);
    pagina.li = li; pagina.img = img; pagina.numero = numero;
    return li;
  }

  function riordina() {
    $("pagine").replaceChildren(...pagine.map((p) => p.li));
    pagine.forEach((p, i) => { p.numero.textContent = String(i + 1); });
    aggiorna();
  }

  function sposta(pagina, passo) {
    const i = pagine.indexOf(pagina), j = i + passo;
    if (j < 0 || j >= pagine.length) return;
    pagine.splice(i, 1); pagine.splice(j, 0, pagina);
    riordina();
  }
  function ruota(pagina) { pagina.rotazione = (pagina.rotazione + 90) % 360; anteprima(pagina); }
  function togli(pagina) {
    if (pagina.url) URL.revokeObjectURL(pagina.url);
    pagine = pagine.filter((p) => p !== pagina);
    riordina();
  }

  function aggiorna() {
    $("pagine-pannello").classList.toggle("hidden", pagine.length === 0);
    $("pagine-conteggio").textContent = pagine.length + (pagine.length === 1 ? " pagina" : " pagine") + " pronte.";
    $("foto").textContent = pagine.length ? "Fotografa un'altra pagina" : "Fotografa una pagina";
    $("crea").disabled = occupato || pagine.length === 0;
  }

  function aggiungi(files) {
    for (const file of files) {
      if (!/^image\//.test(file.type) && !/\.(jpe?g|png|webp|heic|heif)$/i.test(file.name)) continue;
      const pagina = { file, rotazione: 0, url: null };
      pagine.push(pagina);
      $("pagine").appendChild(creaScheda(pagina));
      anteprima(pagina);
    }
    if (!$("titolo").value) $("titolo").value = "Scansione " + nomeData();
    $("esito").textContent = "";
    riordina();
  }

  function nomeData() {
    const d = new Date(), z = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + z(d.getMonth() + 1) + "-" + z(d.getDate()) + " " + z(d.getHours()) + "." + z(d.getMinutes());
  }

  // --- Creazione del PDF e invio --------------------------------------------------------------------------------
  function messaggio(testo, errore, link) {
    const e = $("esito");
    e.className = "text-sm " + (errore ? "text-red-400" : "text-emerald-400");
    e.textContent = testo;
    if (link) {
      const a = document.createElement("a");
      a.href = link; a.textContent = " Apri →"; a.className = "underline hover:text-fg";
      e.appendChild(a);
    }
  }

  async function creaEInvia() {
    if (occupato || !pagine.length) return;
    occupato = true; aggiorna();
    try {
      const filtro = $("filtro").value;
      const jpeg = [];
      for (let i = 0; i < pagine.length; i++) {
        messaggio("Preparo la pagina " + (i + 1) + " di " + pagine.length + "…", false);
        const tela = await disegna(pagine[i], filtro, LATO_PDF);
        const blob = await inJpeg(tela, QUALITA);
        jpeg.push({ dati: new Uint8Array(await blob.arrayBuffer()), larghezza: tela.width, altezza: tela.height });
      }
      const titolo = ($("titolo").value || "Scansione " + nomeData()).trim();
      const pdf = DVPdf.daJpeg(jpeg, { titolo });
      if (pdf.length > massimo) throw new Error("Il PDF pesa " + (pdf.length / 1048576).toFixed(1) + " MB, oltre il limite di " + radice.dataset.maxMb + " MB: togli qualche pagina.");
      const nomeFile = titolo.replace(/[\\/:*?"<>|\x00-\x1f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 100) + ".pdf";

      const dati = new FormData();
      dati.append("file", new Blob([pdf], { type: "application/pdf" }), nomeFile);
      dati.append("percorso", "");
      dati.append("categoria", $("categoria").value);
      dati.append("ai_allowed", $("ai").checked ? "1" : "0");
      messaggio("Invio al server…", false);
      const risposta = await fetch(url, { method: "POST", body: dati, headers: { "X-CSRFToken": csrf }, credentials: "same-origin" });
      let r = null;
      try { r = await risposta.json(); } catch (_) { /* non JSON */ }
      if (!r) throw new Error(risposta.status === 403 ? "Sessione scaduta: ricarica la pagina." : risposta.status === 413 ? "File troppo grande per il server." : "Errore del server (" + risposta.status + ").");
      if (r.stato === "importato") {
        messaggio("Caricato. Il testo viene letto in background; la categoria compare tra poco.", false, r.url);
        azzera(false);
      } else if (r.stato === "presente") {
        messaggio("Questo documento c'è già: " + (r.dettaglio || ""), false, r.url);
      } else {
        throw new Error(r.dettaglio || "Il server non ha accettato il file.");
      }
    } catch (e) {
      messaggio(e.message || "Qualcosa è andato storto.", true);
    } finally {
      occupato = false; aggiorna();
    }
  }

  function azzera(cancellaEsito) {
    pagine.forEach((p) => p.url && URL.revokeObjectURL(p.url));
    pagine = [];
    $("pagine").replaceChildren();
    $("titolo").value = "";
    if (cancellaEsito) $("esito").textContent = "";
    aggiorna();
  }

  // --- Eventi ---------------------------------------------------------------------------------------------------
  $("foto").addEventListener("click", () => $("input-foto").click());
  $("galleria").addEventListener("click", () => $("input-galleria").click());
  for (const input of [$("input-foto"), $("input-galleria")]) {
    input.addEventListener("change", () => { aggiungi([...input.files]); input.value = ""; });
  }
  $("filtro").addEventListener("change", () => pagine.forEach(anteprima));
  $("crea").addEventListener("click", creaEInvia);
  $("azzera").addEventListener("click", () => { if (!pagine.length || confirm("Scartare tutte le pagine?")) azzera(true); });
  window.addEventListener("beforeunload", (e) => { if (pagine.length && !occupato) { e.preventDefault(); e.returnValue = ""; } });

  aggiorna();
})();
