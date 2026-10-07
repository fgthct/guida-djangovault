// «Salva questa ricerca»: la ricerca dal vivo cambia l'URL senza ricaricare la pagina, quindi i filtri
// attuali si leggono dall'indirizzo nel momento dell'invio, non quando la pagina è stata generata.
document.addEventListener("submit", (e) => {
  if (!e.target.matches("#salva-ricerca")) return;
  e.target.querySelector('input[name="filtri"]').value = window.location.search;
});
