// Avvisi in tempo reale (Fase 9). Il server dice solo «qualcosa è cambiato»; la pagina chiede il frammento HTML
// alla view normale tramite htmx (gli elementi ascoltano gli eventi dv:notifiche e dv:versione su <body>).
// Se il WebSocket non c'è, il polling già presente nelle pagine continua a funzionare da solo.
(function () {
  "use strict";
  var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/";
  var socket = null;
  var attesa = 1000;

  function documentiSeguiti() {
    var ids = {};
    document.querySelectorAll("[data-segui-documento]").forEach(function (el) {
      var n = parseInt(el.getAttribute("data-segui-documento"), 10);
      if (n > 0) ids[n] = true;
    });
    return Object.keys(ids).map(Number);
  }

  function segui() {
    var ids = documentiSeguiti();
    if (socket && socket.readyState === 1 && ids.length) socket.send(JSON.stringify({ segui: ids }));
  }

  function apri() {
    try { socket = new WebSocket(url); } catch (e) { return; }
    socket.onopen = function () { attesa = 1000; segui(); };
    socket.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && (m.t === "notifiche" || m.t === "versione")) {
        document.body.dispatchEvent(new CustomEvent("dv:" + m.t, { detail: m }));
      }
    };
    socket.onclose = function (ev) {
      socket = null;
      if (ev.code === 4401 || ev.code === 1008 || ev.code === 403) return;  // non autenticato: inutile riprovare
      setTimeout(apri, attesa);
      attesa = Math.min(attesa * 2, 30000);
    };
  }

  // Dopo ogni sostituzione htmx possono comparire nuovi documenti da seguire.
  document.addEventListener("htmx:afterSwap", segui);
  // Tornando sulla scheda, aggiorna subito ciò che nel frattempo poteva essere cambiato.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      document.body.dispatchEvent(new CustomEvent("dv:notifiche"));
      document.body.dispatchEvent(new CustomEvent("dv:versione"));
    }
  });
  apri();
})();
