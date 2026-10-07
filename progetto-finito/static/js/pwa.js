// Registra il service worker (solo in HTTPS o su localhost: altrove il browser non lo permette).
// Nessun codice inline (CSP).
(function () {
  if (!("serviceWorker" in navigator)) return;
  const sicuro = window.isSecureContext;
  if (!sicuro) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* senza service worker l'app funziona lo stesso */ });
  });
})();
