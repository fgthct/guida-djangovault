// Costruisce un PDF da una serie di immagini JPEG (una pagina per immagine), senza librerie esterne.
// Il JPEG si incorpora così com'è (filtro DCTDecode): niente ricompressione, il PDF è piccolo e veloce da creare.
//
//   DVPdf.daJpeg([{ dati: Uint8Array, larghezza: 1600, altezza: 2100 }, …], { titolo: "Scansione" }) -> Uint8Array
//
// La pagina ha larghezza A4 (595 pt) e l'altezza segue la proporzione della foto: la foto riempie la pagina.
(function (radice) {
  "use strict";
  const codifica = new TextEncoder();

  function daJpeg(pagine, opzioni) {
    if (!pagine || !pagine.length) throw new Error("Nessuna pagina");
    const titolo = ((opzioni && opzioni.titolo) || "").replace(/[^\x20-\x7e]/g, "?").replace(/([()\\])/g, "\\$1").slice(0, 120);
    const pezzi = [];
    const offset = []; // posizione di ogni oggetto (numerati da 1)
    let lunghezza = 0;
    const scrivi = (x) => {
      const b = typeof x === "string" ? codifica.encode(x) : x;
      pezzi.push(b);
      lunghezza += b.length;
    };
    const oggetto = (n, corpo) => { offset[n] = lunghezza; scrivi(n + " 0 obj\n" + corpo + "\nendobj\n"); };

    // Oggetti: 1 catalogo · 2 elenco pagine · 3 info · poi per ogni pagina: pagina, contenuto, immagine.
    const n = pagine.length;
    const idPagina = (i) => 4 + i * 3;
    scrivi("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n");
    oggetto(1, "<< /Type /Catalog /Pages 2 0 R >>");
    oggetto(2, "<< /Type /Pages /Count " + n + " /Kids [" + pagine.map((_, i) => idPagina(i) + " 0 R").join(" ") + "] >>");
    oggetto(3, "<< /Producer (DjangoVault)" + (titolo ? " /Title (" + titolo + ")" : "") + " >>");
    pagine.forEach((p, i) => {
      const W = 595;
      const H = Math.max(1, Math.round((W * p.altezza) / p.larghezza));
      const id = idPagina(i);
      oggetto(id, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + W + " " + H + "] /Resources << /XObject << /Im0 " + (id + 2) + " 0 R >> >> /Contents " + (id + 1) + " 0 R >>");
      const contenuto = "q " + W + " 0 0 " + H + " 0 0 cm /Im0 Do Q";
      oggetto(id + 1, "<< /Length " + contenuto.length + " >>\nstream\n" + contenuto + "\nendstream");
      offset[id + 2] = lunghezza;
      scrivi((id + 2) + " 0 obj\n<< /Type /XObject /Subtype /Image /Width " + p.larghezza + " /Height " + p.altezza +
        " /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length " + p.dati.length + " >>\nstream\n");
      scrivi(p.dati);
      scrivi("\nendstream\nendobj\n");
    });

    const totale = 4 + n * 3; // numero di oggetti + 1 (il numero 0 è riservato)
    const inizioXref = lunghezza;
    let xref = "xref\n0 " + totale + "\n0000000000 65535 f \n";
    for (let k = 1; k < totale; k++) xref += String(offset[k]).padStart(10, "0") + " 00000 n \n";
    scrivi(xref + "trailer\n<< /Size " + totale + " /Root 1 0 R /Info 3 0 R >>\nstartxref\n" + inizioXref + "\n%%EOF\n");

    const fuori = new Uint8Array(lunghezza);
    let pos = 0;
    for (const b of pezzi) { fuori.set(b, pos); pos += b.length; }
    return fuori;
  }

  radice.DVPdf = { daJpeg };
  if (typeof module !== "undefined" && module.exports) module.exports = radice.DVPdf;
})(typeof self !== "undefined" ? self : globalThis);
