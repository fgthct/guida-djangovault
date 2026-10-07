"""Scarica e prova il modello di embeddings (una volta sola, serve Internet). Poi resta nella cartella HF_HOME."""

import time

from django.core.management.base import BaseCommand, CommandError

from documents.embeddings import ModelloNonDisponibile, get_embedder


class Command(BaseCommand):
    help = "Scarica il modello per la ricerca per significato e ne verifica il funzionamento."

    def handle(self, *args, **opzioni):
        inizio = time.time()
        try:
            vettore = get_embedder().domanda("prova di funzionamento")
        except ModelloNonDisponibile as errore:
            raise CommandError(str(errore)) from errore
        self.stdout.write(self.style.SUCCESS(f"Modello pronto: {len(vettore)} dimensioni, {time.time() - inizio:.1f} s."))
