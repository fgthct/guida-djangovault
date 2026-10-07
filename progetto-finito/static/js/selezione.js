// Selezione multipla nell'elenco dei documenti. Delega gli eventi al documento: la tabella viene
// sostituita da HTMX a ogni ricerca, quindi i singoli elementi non si possono agganciare una volta sola.
(() => {
  const modulo = () => document.getElementById("selezione");
  const caselle = () => Array.from(modulo()?.querySelectorAll('input[name="scelti"]') ?? []);

  function aggiorna() {
    const m = modulo();
    if (!m) return;
    const scelte = caselle().filter((c) => c.checked).length;
    const barra = m.querySelector("[data-barra-selezione]");
    const tutte = m.querySelector("[data-seleziona-tutti]");
    barra.hidden = scelte === 0;
    barra.querySelector("[data-conteggio]").textContent = String(scelte);
    if (tutte) {
      tutte.checked = scelte > 0 && scelte === caselle().length;
      tutte.indeterminate = scelte > 0 && scelte < caselle().length;
    }
  }

  document.addEventListener("change", (e) => {
    if (!e.target.closest("#selezione")) return;
    if (e.target.matches("[data-seleziona-tutti]")) {
      caselle().forEach((c) => { c.checked = e.target.checked; });
    }
    if (e.target.matches('input[name="scelti"], [data-seleziona-tutti]')) aggiorna();
  });

  document.addEventListener("click", (e) => {
    if (e.target.matches("[data-deseleziona]")) {
      caselle().forEach((c) => { c.checked = false; });
      aggiorna();
    }
  });

  // Prima di spostare chiede la categoria: un «Sposta in…» vuoto non deve partire.
  document.addEventListener("submit", (e) => {
    if (!e.target.matches("#selezione")) return;
    const premuto = e.submitter?.value;
    if (premuto === "categoria" && !e.target.querySelector('select[name="categoria"]').value) {
      e.preventDefault();
      e.target.querySelector('select[name="categoria"]').focus();
    }
  });

  document.body.addEventListener("htmx:afterSwap", aggiorna);
  window.addEventListener("pageshow", aggiorna);
})();
