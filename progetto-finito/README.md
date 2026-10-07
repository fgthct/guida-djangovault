# DjangoVault

Document Management System professionale: documenti, versioni, workflow, permessi e ricerca semantica.
Applicazione Django **server-rendered**: nessuna SPA, il browser usa HTML, HTMX e (solo dove serve) Alpine.js.

## Stack

| Livello | Tecnologia |
|---|---|
| Linguaggio | Python 3.14, gestito con **uv** |
| Framework | Django 6.1 |
| Database | PostgreSQL 17 + **pgvector** |
| Cache / coda | Redis (cache sul db 0, coda dei task Celery sul db 1) |
| Elaborazione file | Celery (worker + pianificatore), poppler (PDF), Tesseract (OCR), LibreOffice facoltativo |
| Frontend | Django Templates + HTMX + Alpine.js (build CSP) + Tailwind CSS (build reale, niente CDN) |
| Contenitori | Docker Compose |

## Avvio rapido

**[SUL LAPTOP]**

```bash
uv python install 3.14
./scripts/up.sh
docker compose exec django python manage.py createsuperuser
```

`up.sh` crea `.env` con segreti casuali se manca, **controlla che le porte siano libere**, poi costruisce e avvia tutto.
L'app è su http://localhost:8100/ .

Comandi utili:

```bash
docker compose exec django python manage.py test      # suite di test
docker compose logs -f django                          # log dell'app
docker compose logs -f worker                          # log dell'elaborazione dei file
docker compose down                                    # ferma tutto (i dati restano nei volumi)
```

Servizi avviati: `postgres`, `redis`, `django` (il sito) e `worker` (elabora i file in background).

## Porte

Pubblicate solo su `127.0.0.1` (l'app web può essere aperta alla rete con `DV_BIND=0.0.0.0` in `.env`).
Le porte di default sono scelte per non collidere con altri servizi della macchina di sviluppo:

| Servizio | Porta host | Variabile |
|---|---|---|
| App web | 8100 | `DV_WEB_PORT` |
| PostgreSQL | 5440 | `DV_DB_PORT` |
| Redis | 6390 | `DV_REDIS_PORT` |

`./scripts/check_ports.sh` verifica che siano libere e, se una è occupata, dice da chi e propone la prima porta libera.

## Sviluppo di Django fuori da Docker

Database e Redis in Docker, Django in locale con uv:

```bash
cp -n .env.example .env
docker compose up -d postgres redis
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver 8100
```

In un secondo terminale, il worker (senza worker i file restano «in coda»). Sul computer servono poppler e Tesseract:

```bash
sudo apt install -y poppler-utils tesseract-ocr tesseract-ocr-ita tesseract-ocr-eng
uv run celery -A config worker -B -l info
```

## CSS (Tailwind)

Il CSS finale è `static/css/app.css`, generato da `frontend/css/app.css` con la CLI standalone di Tailwind (nessun Node richiesto; il binario si scarica da solo in `bin/`).
Il file generato è versionato, quindi per **avviare** il progetto non serve alcuna build. Serve solo quando si cambiano le classi nei template:

```bash
./scripts/tailwind.sh watch    # sviluppo: rigenera a ogni modifica
./scripts/tailwind.sh build    # build minificata prima del commit
```

`htmx` e Alpine.js sono in `static/vendor/` (niente CDN). Per aggiornarli: `./scripts/vendor_update.sh`.

## Regole di progetto

1. **HTML prima di JavaScript**: Django Template → HTMX → Alpine.js → JS custom.
2. **Sicurezza sempre server-side**: nascondere un pulsante è solo UX.
3. **Content Security Policy rigida** (`default-src 'self'`): niente `<script>` né `style=` inline. Per questo si usa la build CSP di Alpine.js, dove la logica sta in `static/js/app.js` (`Alpine.data(...)`) e nei template resta solo `x-data="nome"`.
4. **Nessuna rotta `/media/`**: i documenti passeranno sempre da una view che controlla i permessi.
5. **HTMX = frammenti**: la view restituisce un partial. Django 6 ha i partial nativi (`{% partialdef %}`, `render(request, "pagina.html#nome")`): vedi `core/dashboard.html#servizi`.
6. **Nuove app solo per responsabilità reali** del dominio. Oggi: `accounts` (utenti e accesso), `core` (dashboard, controlli di stato) e `documents` (documenti, versioni, categorie).
7. **Tema scuro, mai sfondi bianchi** (scelta di progetto: affaticano la vista). I colori sono token in `frontend/css/app.css` (`bg-page`, `bg-rail`, `bg-surface`, `bg-surface-2`, `border-line`, `text-fg`, `text-muted`): nei template si usano solo questi, mai `bg-white` o tinte chiare di Tailwind. Vale anche per l'admin di Django (`static/css/admin_dark.css`) e per le pagine di errore (`templates/errore.html`). `core/tests/test_tema.py` fa fallire la suite se compare una classe con sfondo chiaro. Unica eccezione: le pagine tecniche di errore che Django mostra solo con `DJANGO_DEBUG=1`.
8. **Ogni nuova view richiede l'autenticazione**: `core/tests/test_security.py` scandisce tutte le rotte da anonimo e fallisce se una è pubblica senza essere nell'elenco delle eccezioni.

## Documenti (Fase 2)

- **Service layer**: la logica sta in `documents/services/` (`create_document`, `update_document`, `delete_document`), le view solo validano e rispondono. Creazione atomica: se qualcosa fallisce il file appena scritto viene rimosso; alla cancellazione i file si tolgono dopo il commit.
- **Permessi in un solo punto**: `documents/permissions.py` e `Document.objects.visible_to(user)`. Le view non usano mai `objects.all()`. Un documento altrui risponde **404**, come se non esistesse. La Fase 4 sostituirà solo questi due punti.
- **File**: salvati con nome casuale (`documenti/xx/<uuid>.ext`), nome originale e SHA-256 nel database, scaricabili solo dalla view `scarica/` (autenticata). Un file identico già presente viene segnalato, non bloccato.
- **Limiti** (variabili d'ambiente): `DOCUMENTS_MAX_UPLOAD_MB` (50) e `DOCUMENTS_ALLOWED_EXTENSIONS`. Con Nginx (Fase 10) servirà anche `client_max_body_size` coerente.
- **Ricerca**: vedi «Ricerca» (Fase 7).
- **Categorie**: gestite solo dallo staff; una categoria in uso non si elimina (409).
- **Cache dei file statici**: `{% asset 'css/app.css' %}` aggiunge `?v=<data modifica>`, quindi niente più CSS vecchio nel browser.

### Versioni (Fase 3)

- Ogni caricamento crea una **nuova** `DocumentVersion` (numero progressivo) e la rende corrente: nessun file viene mai sovrascritto o cancellato. Il numero si calcola con il documento bloccato (`select_for_update`), quindi due caricamenti simultanei non ottengono lo stesso numero.
- Un file **identico alla versione corrente** (stesso SHA-256) è rifiutato; identico a una versione più vecchia è ammesso.
- **Ripristino** = nuova versione con il contenuto della vecchia (il file viene copiato, `restored_from` ricorda l'origine). La cronologia non si riscrive.
- Servizi in `documents/services/versions.py`: `add_version`, `restore_version`. Permessi: `can_add_version`, `can_restore_version` (oggi = `can_edit`).

### Permessi (Fase 4)

| Livello | Può |
|---|---|
| Lettura | vedere il documento e la cronologia, scaricare ogni versione |
| Modifica | + modificare i metadati, caricare nuove versioni, ripristinare |
| Amministrazione | + eliminare il documento e gestire la condivisione |

- Il **proprietario** e il **superutente** sono sempre Amministrazione. Un documento si condivide con un **utente** o con un **gruppo** (i gruppi Django si creano in `/admin/` → Gruppi e si assegnano agli utenti lì). Se un utente ha più accessi (diretto e da gruppi) vale il più alto.
- **404 se non si vede, 403 se si vede ma non si può fare l'azione.** Un documento non condiviso risponde 404, come se non esistesse.
- Tutto passa da `documents/permissions.py` (`livello`, `can_*`) e da `Document.objects.visible_to(user)`; i servizi `share_document` / `revoke_share` sono in `documents/services/sharing.py`. Nell'elenco `annota_livelli` calcola i livelli con una sola query.
- Non c'è un modello `Role` separato: i ruoli di sistema sono i gruppi Django, i ruoli sul documento sono i tre livelli. Se servirà separare DELETE e SHARE da ADMIN, si aggiunge un livello senza toccare view e servizi.
- **Revisione e approvazione** non sono un permesso del documento: le decide il proprietario per ogni invio (vedi Workflow). Un revisore senza accesso riceve automaticamente la Lettura.

### Workflow (Fase 5, rivisto: revisione a più persone)

```
Bozza ──il proprietario invia──▶ In revisione ──tutti approvano──▶ Approvato ──archivia──▶ Archiviato
  ▲                                   │                                │                       │
  └── uno rimanda / il proprietario ritira ┘◀──────────riapri──────────┘◀──────ripristina──────┘
```

- **Chi partecipa lo sceglie solo il proprietario**, da un elenco a discesa degli utenti attivi (con un ruolo facoltativo: «Responsabile acquisti», «CTO»…). Nessun altro può aggiungere o togliere persone; il proprietario non può essere revisore di sé stesso.
- **Messaggio ai revisori** (facoltativo, fino a 1000 caratteri): il proprietario scrive cosa controllare o cosa gli serve («Controlla il capitolo 3…»). Compare nella notifica (accorciato), nella pagina del documento, nella pagina di decisione e nel registro.
- **Modalità** (casella «Approvazione in sequenza» prima di confermare): *in sequenza* = rispondono uno alla volta nell'ordine delle righe, e solo chi è di turno riceve la campanella; *libera* = tutti sono avvisati subito e risponde chi vuole.
- **Esito**: il documento è approvato quando **tutti** approvano. Se **uno solo rimanda** (nota obbligatoria) il giro si chiude, le approvazioni decadono, il documento torna in **Bozza al proprietario** e tutti i partecipanti vengono avvisati. Il proprietario può anche **ritirare** il giro (nota obbligatoria).
- Ogni invio è un **giro** numerato (`ReviewRound`, con i `ReviewStep` dei revisori): lo storico resta consultabile nel dettaglio del documento e nel registro. Un nuovo invio parte con i revisori e la modalità dell'ultimo giro già precompilati, e le approvazioni sono legate alla versione inviata.
- Transizioni manuali rimaste (solo Amministrazione): Approvato → Archiviato, Approvato → Bozza (motivo obbligatorio), Archiviato → Bozza, Bozza → Archiviato. Tutto il resto passa da `documents/services/review.py` (`start_review`, `decide_review`, `withdraw_review`).
- Controllo nei servizi, con `select_for_update` e rivalidazione: se due persone agiscono insieme, il secondo riceve un avviso invece di sovrascrivere. Un vincolo di database ammette un solo giro in corso per documento.
- **Fuori dalla Bozza il documento è bloccato**: niente modifica dei metadati, nuove versioni o ripristini finché non torna in Bozza (`assicura_modificabile`). Condivisione, download ed eliminazione restano possibili secondo i permessi.
- Non implementate (da decidere se servono): fasi miste («insieme al precedente») e più proprietari.
- La migrazione `0005` rimuove `can_approve` e riporta in Bozza i documenti che erano «In revisione» con il vecchio sistema.

### Registro delle operazioni e notifiche (Fase 5)

- `core.AuditLog` registra accessi (riusciti, falliti, uscite), creazione/modifica/eliminazione documenti, download, versioni, condivisioni e cambi di stato, con utente, IP e dettagli. `core/services/audit.py: record()` si chiama dai servizi **dentro la stessa transazione**: se l'operazione viene annullata, sparisce anche la riga. Il registro è in sola lettura, anche nell'admin.
- L'IP arriva dal middleware `RequestContextMiddleware`. `X-Forwarded-For` si legge solo con `AUDIT_TRUST_X_FORWARDED_FOR=1` (da attivare con Nginx, Fase 10): altrimenti sarebbe falsificabile. Le password non vengono mai registrate.
- Chi vede cosa: **Attività** di un documento (chi lo amministra), **Registro** completo (solo superutenti).
- `core.Notification`: persistenti, con campanella nell'intestazione (HTMX aggiorna il contatore ogni 30 s e subito quando il WebSocket avvisa, Fase 9). Si notificano: richiesta di revisione (ai revisori di turno), avanzamento e chiusura del giro (a proprietario e partecipanti, con la nota), nuova condivisione (al destinatario o ai membri del gruppo). Chi compie l'azione non viene notificato.
- Conservazione del registro: per default **per sempre** (`AUDIT_RETENTION_DAYS=0`); si può impostare un periodo in giorni (Fase 10: pulizia giornaliera, vedi «Produzione»).

### Elaborazione asincrona (Fase 6)

Ogni versione caricata (nuova, ripristinata, prima del documento) viene elaborata **in background**: testo estratto, OCR se serve, anteprima della prima pagina. Il caricamento non aspetta mai l'elaborazione.

```
carica file ──▶ versione «In coda» ──▶ worker: «In corso» ──▶ «Completata» | «Non applicabile» | «Errore»
```

- **Il database è la fonte di verità, Redis è solo il campanello.** Lo stato sta in `DocumentVersion.processing_status`. Il task parte dopo il commit (`transaction.on_commit`): il worker non vede mai una riga non ancora salvata. Se Redis è fermo il caricamento riesce comunque e la versione resta «in coda».
- **Idempotente e senza doppioni**: la versione si «prenota» con `select_for_update(skip_locked=True)`, quindi due worker (o un messaggio duplicato) non lavorano mai lo stesso file; il lavoro pesante avviene fuori da ogni transazione. Un risultato che arriva dopo che la versione è stata eliminata viene scartato senza lasciare file.
- **Rete di sicurezza** (`documents.riprendi_elaborazioni`, ogni 5 minuti dal pianificatore del worker): rimette in coda ciò che è «in coda» da più di 2 minuti senza essere stato inviato e ciò che è «in corso» da più di 15 (`PROCESSING_STALE_MINUTES`) perché il worker è morto. Dopo `PROCESSING_MAX_ATTEMPTS` (3) tentativi passa a «Errore». Una versione in errore non viene ritentata da sola: si usa **Rielabora**.
- **Stesso contenuto = stesso risultato**: se esiste già una versione identica (stesso SHA-256) completata, testo e anteprima vengono copiati invece di rifare l'OCR (tipico del ripristino).
- **Formati**:

| Formato | Testo | Anteprima |
|---|---|---|
| PDF con testo | `pdftotext` | prima pagina (`pdftoppm`) |
| PDF scansionato (poco testo per pagina) | OCR Tesseract sulle prime 30 pagine (`PROCESSING_OCR_MAX_PAGES`) | prima pagina |
| png, jpg, gif, webp | OCR | miniatura |
| docx, xlsx, pptx, odt, ods, odp | lettura diretta dell'XML (`defusedxml`) | con LibreOffice, se installato |
| doc, xls, ppt, rtf | solo con LibreOffice (converte in PDF) | con LibreOffice |
| txt, md, csv | lettura diretta (UTF-8, UTF-16 o Windows-1252) | — |
| altro | «Nessun testo da estrarre» | — |

- **Sicurezza**: i programmi esterni girano senza shell, con timeout (`PROCESSING_SUBPROCESS_TIMEOUT`, 120 s) e in cartelle temporanee; gli archivi Office/ODF hanno un limite alla dimensione decompressa e un parser XML che rifiuta le entità ricorsive; le immagini enormi (`PROCESSING_MAX_IMAGE_PIXELS`) vengono rifiutate; il testo è limitato a `PROCESSING_TEXT_MAX_CHARS` e ripulito dai caratteri NUL che PostgreSQL non accetta. I dettagli di un errore imprevisto restano nei log, all'utente va un messaggio generico.
- **Anteprime**: PNG con nome casuale in `anteprime/`, servite solo da `…/versioni/N/anteprima/` con gli stessi permessi del download (404 se non vedi il documento). Sono pagine bianche: in tema scuro si mostrano invertite (casella «Colori originali» per vederle com'erano).
- **Interfaccia**: ogni versione mostra il suo stato; finché è in lavorazione il blocco si aggiorna da solo (HTMX, ogni 3 s) e a fine lavoro la pagina si ricarica una volta per mostrare anteprima e testo. **Rielabora** (livello Modifica, anche a documento non in bozza: sono dati derivati) rimette in coda; in coda/in corso non si può.
- **Dashboard**: «Coda dei task» (Redis raggiungibile, task in attesa) ed «Elaborazione file» (worker attivi, versioni in coda/in corso/in errore). Senza worker è un **Problema** ben visibile.
- **Registro**: `version.processing_failed` (utente «sistema») e `version.reprocess_requested`.
- **Comandi**:

```bash
docker compose exec django python manage.py elabora_versioni                # come il pianificatore: rimette in coda ciò che è fermo
docker compose exec django python manage.py elabora_versioni --falliti      # anche quelle in errore
docker compose exec django python manage.py elabora_versioni --tutte        # rielabora tutto (dopo un aggiornamento dell'OCR)
docker compose exec django python manage.py elabora_versioni --sincrono     # elabora subito in quel processo, senza worker
```

- **Dopo l'aggiornamento dalla v11**: le versioni già caricate risultano «in coda» e il pianificatore le elabora a gruppi di 200 ogni 5 minuti; per farlo subito, `elabora_versioni`.
- **LibreOffice** (facoltativo, ~600 MB): `INSTALL_LIBREOFFICE=1` in `.env` e `docker compose build` danno le anteprime di Word/Excel/PowerPoint e la lettura dei vecchi `.doc`/`.xls`/`.ppt`/`.rtf`. Senza, i formati moderni hanno testo ma non anteprima e i vecchi risultano «Nessun testo da estrarre».
- **Concorrenza**: `DV_WORKER_CONCURRENCY` (2). Ogni OCR usa una CPU; il worker ricicla i processi ogni 50 task.
- **Test**: la suite non scrive mai nella coda Redis vera (`manage.py test` usa un broker in memoria). I test di estrazione usano file veri generati al volo e si saltano se poppler/Tesseract mancano.
- L'avviso in tempo reale a fine elaborazione c'è dalla Fase 9 (vedi «Tempo reale»); il polling resta come rete di sicurezza.

### Ricerca (Fase 7)

Tre modi, scelti dal selettore accanto alla casella (predefinito: **Parole e significato**):

| Modo | Come funziona | Trova |
|---|---|---|
| Solo parole | full-text di PostgreSQL | «locazione» → locazione, locazioni; anche «societa» per «società» |
| Solo significato | vettori (pgvector) | «affitto» → un contratto che dice «locazione» |
| Parole e significato | i due elenchi fusi con Reciprocal Rank Fusion | chi è in alto in entrambi sale |

- **Parole**: configurazione `italian_unaccent` (accenti ignorati + radici italiane), colonne `tsvector` generate da PostgreSQL (`GeneratedField`) con indice GIN: titolo (peso A), descrizione (B), testo estratto della versione **corrente** (C). Sintassi dell'utente: «frase esatta», `-esclusa`, `a OR b`; non può mai dare errore di sintassi. Si trova anche per frammento di titolo/nome del file (`contr` → «Contratto»). Gli estratti con le parole evidenziate (`<mark>`) sono costruiti con `ts_headline` ed **escapati prima** dell'evidenziazione: il contenuto di un documento non può iniettare HTML.
- **Significato**: il testo estratto è spezzato in passaggi (~1000 caratteri, con sovrapposizione, prima per paragrafi, poi per frasi), ciascuno trasformato in un vettore da `intfloat/multilingual-e5-small` (384 dimensioni, locale, CPU, nessun dato esce dal server). Indice HNSW (coseno). Per ogni documento conta il passaggio migliore, che diventa l'estratto. Sotto `SEARCH_SEMANTIC_MIN_SCORE` (0.80, scala molto stretta con questo modello) un passaggio non conta: meglio nessun risultato che uno a caso.
- **Permessi**: la ricerca parte sempre da `Document.objects.visible_to(user)` già filtrato (categoria, stato, «miei»…); i passaggi di documenti non visibili non vengono nemmeno esaminati. Con pgvector ≥ 0.8 si usa `hnsw.iterative_scan` per non perdere risultati quando il filtro è stretto.
- **Indicizzazione**: a fine elaborazione il worker accoda `indicizza_versione`; stesso schema della Fase 6 (database come verità, Redis come campanello, rete di sicurezza ogni 10 minuti con pausa di 30 fra tentativi). Se cambi `EMBEDDING_MODEL` tutto risulta «da indicizzare» e viene rifatto. Dove vederlo: nella cronologia di ogni versione (pagina del documento) la riga «Ricerca per significato: indicizzato · N passaggi / in attesa / in corso / non riuscita», con **Riprova** (livello Modifica) se non è riuscita; in dashboard la voce «Indice per significato» (versioni indicizzate, passaggi, modello; **Problema** se qualcuna è rimasta ferma da oltre 10 minuti); nell'elenco, durante una ricerca per significato, «N non ancora ricercabili».
- **Se il modello non è disponibile** (primo avvio senza Internet, ecc.) l'elenco ricade sulla ricerca per parole con un avviso, senza errori.
- **Docker**: il modello (~470 MB) si scarica una volta nel volume `models_cache` (`HF_HOME=/models`). Sia il sito che il worker ne caricano una copia in memoria (~500 MB ciascuno, al primo uso). L'immagine cresce di ~1,2 GB per PyTorch (versione CPU).
- **Comandi**:

```bash
docker compose exec worker python manage.py scarica_modello                  # scarica e prova il modello (una volta)
docker compose exec django python manage.py indicizza_versioni               # mette in coda le versioni non indicizzate
docker compose exec django python manage.py indicizza_versioni --sincrono    # le indicizza subito, senza worker
docker compose exec django python manage.py indicizza_versioni --tutte       # rifà tutto (dopo aver cambiato modello o spezzatura)
```

- **Test**: usano un embedder finto deterministico (`documents/tests/fakes.py`), quindi non scaricano nulla. Una prova col modello vero: `DV_TEST_MODELLO_VERO=1 python manage.py test documents.tests.test_semantic.ModelloVeroTests`.
- Risposte in linguaggio naturale: vedi «Assistente IA» (Fase 8).

### Assistente IA — Chiedi (Fase 8)

Domande in italiano sui documenti, con risposta in streaming e **citazioni cliccabili** [1], [2]… che rimandano al documento e al passaggio. Sotto la risposta, «Fonti citate» elenca solo ciò che la risposta cita davvero; gli altri passaggi inviati al modello stanno in «Altri passaggi consultati, non citati». È un RAG: si recuperano i passaggi pertinenti (Fase 7) e li si dà a Claude, che deve rispondere solo con quelli.

```
domanda ─▶ riformulazione (Haiku: fino a 3 query autonome) ─▶ ricerca dei passaggi (permessi + consenso)
        ─▶ Sonnet risponde citando [n] ─▶ risposta salvata con le fonti ─▶ riga nel registro
```

- **Dove**: voce **Chiedi** nel menu (tutto ciò che puoi leggere) e pulsante **Chiedi** nella pagina di un documento (solo quel documento). Le conversazioni restano salvate e sono **private**: nemmeno il superutente le vede (404).
- **Privacy: consenso per documento.** I passaggi pertinenti vengono inviati all'API di Anthropic. Solo i documenti con `ai_allowed` possono essere letti dall'assistente; si decide dalla pagina del documento ("Assistente IA", livello Amministrazione, in qualunque stato) o con la casella al caricamento (precompilata). I documenti già presenti partono **senza** consenso: `ai_consenti --tutti` oppure `--categoria "Nome"`. Ogni cambio di consenso e ogni risposta (`ai.question`: documenti usati, modello, token; **non** il testo della domanda) finiscono nel registro.
- **Permessi**: la ricerca parte da `visible_to(user)` + `ai_allowed`; i passaggi degli altri documenti non vengono nemmeno esaminati, quindi non possono essere inviati. Se perdi l'accesso a un documento, le fonti già salvate nelle tue conversazioni mostrano «Documento non più accessibile» (il testo della risposta, già scritto, resta).
- **Recupero**: ogni query (la domanda + le riformulazioni) cerca i 30 passaggi più vicini; le liste si fondono con RRF, più la corsia delle parole (se la domanda nomina qualcosa che è nel testo). Massimo `AI_MAX_PASSAGES` (12) passaggi, 3 per documento. Sotto la soglia `AI_MIN_SCORE` (0.80), o troppo lontano dal passaggio migliore (`AI_SCORE_MARGIN`, 0.06: in un archivio con molti testi sullo stesso tema non ci si porta dietro quelli solo vagamente affini), non si risponde con un passaggio a caso: **se non c'è nulla di pertinente il modello non viene nemmeno chiamato** («Non ho trovato…»). Su un singolo documento la soglia non c'è.
- **Filtro per categoria**: nella pagina «Nuova domanda» un menu limita le domande ai documenti di una categoria (elenco, ricerca per termini e passaggi inclusi). La scelta resta legata alla conversazione.
- **Domande su nomi/termini («in quali libri appare Marco Ferrante?»)**: Haiku estrae anche i `termini` (in più c'è un'euristica sui nomi propri con la maiuscola). Per ogni termine la ricerca testuale trova fino a `AI_MAX_OCCURRENCES` (15) documenti che lo contengono e se ne prende il passaggio migliore per ciascuno, senza soglia né margine. Al modello arriva anche un blocco `<indice>` con i titoli dei documenti, e sotto la risposta compare un elenco deterministico ««X» compare in N documenti» (solo documenti ancora visibili all'utente).
- **Contro le istruzioni nascoste nei documenti** (prompt injection): gli estratti stanno in un blocco `<fonti>` e il loro testo è neutralizzato (`<`, `>`, `&`), quindi non può chiuderlo né aprirne uno falso; il prompt di sistema li dichiara dati e non istruzioni. La risposta è poi mostrata con escape completo: il modello non può produrre HTML.
- **Streaming**: SSE (`/chiedi/messaggi/N/flusso/`). La generazione parte quando il browser apre il flusso e il messaggio viene «prenotato» in modo atomico: una seconda scheda o un ricaricamento non generano (né pagano) due volte; si limitano a mostrare lo stato. Risposte rimaste a metà per più di 3 minuti diventano errore. Con Nginx (Fase 10) serve `proxy_buffering off` per quella rotta (l'header `X-Accel-Buffering: no` c'è già).
- **Limiti**: 1000 caratteri per domanda, 30 domande all'ora per utente, una domanda alla volta per conversazione, 1500 token di risposta, timeout 60 s e 2 tentativi sull'API.
- **Modelli** (`.env`): `AI_MODEL` (risposta, `claude-sonnet-4-6`) e `AI_QUERY_MODEL` (riformulazione, `claude-haiku-4-5`; vuoto = nessuna riformulazione). Per cambiare modello basta il nome. Senza `ANTHROPIC_API_KEY` la pagina lo dice e non parte nulla.
- **Errori dell'API** (chiave non valida, limite di richieste, servizio sovraccarico o irraggiungibile, credito esaurito…) diventano messaggi chiari per l'utente; i dettagli tecnici restano nei log.
- **Test**: usano un finto Claude (`assistant/tests/fakes.py`) e l'embedder finto: nessun test chiama l'API vera. Provata a mano solo la parte che non dipende dall'API (vedi sotto).
- **Non ancora provata con la chiave vera**: la chiamata reale ad Anthropic. Va fatto un controllo a mano dopo aver messo `ANTHROPIC_API_KEY` nel `.env`.
- Non incluso: risposte che leggono l'intero documento (solo passaggi), filtri per categoria nella chat, aggiornamento in tempo reale fra più schede (Fase 9).

### Definition of Done

Model corretto · Migration · Validazione · Business logic · Permessi · View · Template · HTMX se serve · Gestione errori · Audit se serve · Test · Logging se serve · Documentazione.

## Muoversi tra i documenti (Fase 5)

- **Ricerca rapida**: `Ctrl+K` (`Cmd+K` su Mac) o il pulsante «Cerca» in alto, da qualunque pagina. Scrivi, scegli con le frecce, `Invio` apre il documento. Con il campo vuoto mostra gli ultimi toccati. Cerca per parole e per significato insieme (come la modalità «entrambi» dell'elenco), al massimo 8 risultati, solo fra i documenti che puoi vedere. Con meno di 3 caratteri, o se il modello di ricerca per significato non è disponibile, ripiega in silenzio sulle sole parole.
- **Documenti simili**: nella pagina di un documento, fino a 5 documenti dello stesso argomento (stessa idea di «abbastanza vicino» del classificatore). Compaiono solo se il documento ha testo indicizzato.
- **Selezione multipla**: nell'elenco, le caselle a sinistra. Con almeno un documento scelto compare la barra: *Sposta* in una categoria (solo documenti in bozza e con permesso di modifica; gli altri sono saltati e contati) e *Scarica ZIP* (fino a 100 documenti). Non esiste l'eliminazione di massa, di proposito.
- **Ricerche salvate**: sopra la ricerca, «Salva questa ricerca come…» ricorda testo e filtri attuali (non la pagina). Sono personali, al massimo 20; con lo stesso nome si aggiornano.
- **Immagini senza testo**: propongono «Immagini e foto» in «Da smistare», sempre da confermare.

## Struttura

```
config/          settings (da variabili d'ambiente), urls, asgi/wsgi
accounts/        utente personalizzato, accesso/uscita
config/celery.py app Celery e pianificazione periodica
core/            dashboard, controlli di stato, audit (services/audit.py), notifiche, form/tag condivisi, migration pgvector
documents/       modelli, permessi (permissions.py), services/, extraction.py (testo e anteprime), tasks.py (Celery), form, view, test
templates/       base.html, app.html, errore.html (+ 400/403/404/500), components/, admin/, accounts/, core/
static/          css/ (app.css generato, admin_dark.css), js/, vendor/ (htmx, Alpine), img/
frontend/css/    sorgente Tailwind
scripts/         up.sh, check_ports.sh, tailwind.sh, vendor_update.sh
```

## Roadmap

- [x] **Fase 1 — Foundation**: repository, Docker, Django 6.1, PostgreSQL + pgvector, configurazione, base template, Tailwind, HTMX, autenticazione
- [x] **Fase 2 — Documents**: modello, categorie, upload, elenco con ricerca dal vivo, dettaglio, modifica, eliminazione, download protetto
- [x] **Fase 3 — Versioning**: nuove versioni, cronologia, download di una versione, ripristino
- [x] **Fase 4 — Permissions**: condivisione con utenti e gruppi, livelli Lettura/Modifica/Amministrazione
- [x] **Fase 5 — Workflow**: stati e transizioni con permessi, registro delle operazioni (audit), notifiche
- [x] **Fase 6 — Async processing**: Celery + Redis, estrazione del testo (PDF, OCR, Office, OpenDocument), anteprime, riprese automatiche
- [x] **Fase 7 — Search**: full-text italiano senza accenti, estratti evidenziati, ricerca per significato (pgvector, e5-small locale), ricerca combinata
- [x] **Fase 8 — RAG**: assistente «Chiedi» con Claude (Sonnet + Haiku), citazioni, streaming, consenso IA per documento, registro
- [x] **Fase 9 — Real-time**: WebSocket per notifiche e stato dell'elaborazione
- [x] **Fase 10 — Production**: Nginx + Daphne, HTTPS, copie di sicurezza, manutenzione periodica

### Prospettiva: REST e PWA

Dopo le fasi principali: API REST (DRF) per client esterni e **PWA installabile** su dispositivi della rete.
Nota tecnica: service worker e installazione richiedono un contesto sicuro, cioè **HTTPS** (fa eccezione solo `localhost`).
Per provarla da altri dispositivi in LAN servirà quindi un certificato locale (per esempio con `mkcert`) davanti all'app.


### Tempo reale (Fase 9)

- **WebSocket `/ws/`** con Django Channels + Daphne (`daphne` è la prima app di `INSTALLED_APPS`, quindi `runserver` è ASGI e in Docker non cambia nulla). Il livello dei canali usa Redis (db 2); senza Redis o nei test è in memoria.
- **Gli avvisi non portano dati**: dicono solo «qualcosa è cambiato». Il browser chiede poi il frammento HTML alla view normale, che controlla i permessi come sempre. Il WebSocket non può quindi far trapelare contenuti.
- **Che cosa si aggiorna subito**: il contatore della campanella (quando nasce una notifica, `core/services/notifications.py`) e il blocco «elaborazione/indicizzazione» di una versione (segnale `post_save` su `DocumentVersion`, anche quando a salvare è il worker Celery: passa da Redis). Gli avvisi partono solo dopo il commit e un errore di Redis non fa mai fallire l'operazione.
- **Sicurezza**: connessione solo con sessione valida (altrimenti chiusa con codice 4401), `Origin` controllato contro `ALLOWED_HOSTS`, messaggi in ingresso piccoli e validati, un utente può «seguire» solo documenti che vede (`visible_to`) e al massimo `REALTIME_MAX_DOCUMENTS` (30).
- **Se il WebSocket non c'è** (rete aziendale, proxy) tutto funziona come prima grazie al polling (30 s per le notifiche, 3-4 s per l'elaborazione). Il client (`static/js/realtime.js`) si riconnette da solo con attesa crescente.
- **Fase 10 (Nginx)**: per `/ws/` servono `proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";` e un `proxy_read_timeout` lungo; in produzione l'app gira con `daphne -b 0.0.0.0 -p 8000 config.asgi:application`.


### Importare cartelle di file (libri, guide…)

Dal **laptop**, un comando manda al server i file nuovi di una cartella e li fa importare (stesso percorso del caricamento da browser: testo, OCR e indice partono da soli in background; per molti libri servono minuti od ore sulla CPU del server):

```bash
./scripts/invia_cartella.py ~/Documenti/libri --categoria "Libri informatica"
./scripts/invia_cartella.py ~/Documenti --per-sottocartelle    # categoria = nome della prima sottocartella
./scripts/invia_cartella.py ~/Documenti/guide --prova          # elenca soltanto, non invia
```

Le immagini (PNG, JPG, GIF, WebP) di norma **non** si inviano (non hanno testo da cercare): `--con-immagini` le include. Usa solo `rsync` e `ssh` (alias `scrivania`; `--host`, `--progetto` per cambiarli). I file sul laptop non vengono toccati. Un registro in `~/.cache/djangovault/inviati.json` ricorda ciò che è già dentro (per contenuto, non per nome): rilanciarlo manda solo il nuovo. Il server riconosce comunque i doppioni dal contenuto (SHA-256). Opzioni: `--con-consenso-ia` (di norma i documenti arrivano **senza** il consenso IA e non saranno letti da «Chiedi» finché non lo si dà; con questa opzione partono già consentiti), `--tutto` (ignora il registro). Si inviano solo le estensioni ammesse (`DOCUMENTS_ALLOWED_EXTENSIONS`) fino a `DOCUMENTS_MAX_UPLOAD_MB` (50): gli altri file vengono elencati come «salto».

Sul server i file passano da `inbox/` (montata nel contenitore) e `./scripts/prod.sh importa [--categoria X] [--per-sottocartelle] [--prova]` li importa e li toglie da lì. Proprietario: il primo superutente (`manage.py importa_cartella --utente nome` per un altro).

**Dopo l'importazione.** `./scripts/prod.sh categorizza Romanzi --da "Libri informatica" --contiene "titolo"` sposta documenti già importati in una categoria (`--prova` per vedere prima; non cancella nulla). Nell'elenco documenti le categorie sono schede con il numero di documenti, il filtro «Tipo» separa le immagini dal resto e il badge «Bozza» non compare (gli altri stati sì).

### Caricare dal browser (più file e cartelle)

**Documenti → Carica più file** (`/documenti/carica/`): trascini file o intere cartelle (oppure «Scegli file» / «Scegli una cartella»), scegli la categoria e il consenso IA e premi *Carica*. I file vanno al server **uno alla volta**: si vede l'avanzamento per file, un file rotto non ferma gli altri e un file identico a uno già presente (SHA-256, o a un altro dello stesso lotto) risulta «Già presente» senza creare un doppione. Si saltano i file nascosti, i temporanei di Office, i tipi non ammessi, i file vuoti o oltre `DOCUMENTS_MAX_UPLOAD_MB`. Categoria: *Automatica* (la propone il sistema, vedi sotto), *Nessuna*, *Dalla sottocartella* (trascinando `miei_libri`, il file `miei_libri/Romanzi/x.pdf` va in «Romanzi») oppure una categoria esistente. Nginx deve ammettere almeno `DV_NGINX_MAX_BODY` per file. Il caricamento singolo con titolo e descrizione resta in *Carica un documento*.

### Classificazione automatica e «Da smistare»

Per i documenti **senza categoria** il sistema propone categoria, riassunto e parole chiave, con due fonti indipendenti:

1. **Documenti simili**, in locale e gratis: ogni versione ha un vettore (media dei suoi passaggi); i 5 documenti già categorizzati più vicini «votano» la loro categoria. Impara le convenzioni con cui hai ordinato l'archivio. Nessun dato esce dal server.
2. **Claude Haiku**, solo per i documenti con il **consenso IA** (e con la chiave configurata): riceve titolo, nome del file, i primi `CLASSIFICAZIONE_CARATTERI` (3000) caratteri e l'elenco delle categorie con qualche titolo di esempio. Senza consenso nessun testo parte.

Se le due fonti concordano la fiducia sale. Sopra `CLASSIFICAZIONE_SOGLIA_AUTO` (0,85) la categoria viene assegnata da sola; altrimenti la proposta finisce in **Documenti → Da smistare**, dove si accetta (anche scegliendo un'altra categoria), si ignora o si accettano in blocco quelle sopra una fiducia scelta. Le assegnazioni automatiche restano elencate per 30 giorni e si **annullano** con un clic (solo se nel frattempo nessuno ha cambiato la categoria). Una categoria *nuova* proposta dall'IA non viene mai creata senza che tu accetti. Ogni applicazione, rifiuto e annullamento è nel registro attività. Se la descrizione è vuota ci scrive riassunto e parole chiave (non sovrascrive mai una descrizione tua) e non cambia la data di aggiornamento.

Quando parte: caricando con categoria *Automatica*, con `importa_cartella --classifica` / `invia_cartella.py --classifica`, dal pulsante **Suggerisci categoria** nella scheda, o per i documenti già presenti dal comando:

```bash
./scripts/prod.sh classifica --valuta     # quanto indovina sui TUOI documenti già categorizzati (non scrive nulla)
./scripts/prod.sh classifica --prova      # mostra le proposte senza salvarle
./scripts/prod.sh classifica              # salva le proposte in «Da smistare»
./scripts/prod.sh classifica --applica    # in più applica quelle sopra soglia (annullabili)
./scripts/prod.sh classifica --senza-ia   # solo documenti simili
```

**Descrizione delle categorie.** In **Categorie** (staff) ogni categoria ha una riga «Cosa ci va dentro?»: Haiku la legge insieme al nome e a 4 titoli di esempio, con la precedenza sui titoli. Serve soprattutto per categorie dal nome vago o che si somigliano («Guide server e deploy» contro «Prodotti (Morpheo, Ristoflow, Antiqua)»). Con poche righe ben scritte gli errori dell'IA calano molto.

**Misurare gli errori veri.** La categoria proposta si conserva (`proposed_category`) anche quando in «Da smistare» ne scegli un'altra; `classifica --valuta` stampa in cima «Le tue decisioni»: quante proposte hai accettato com'erano, per origine (`vicini`, `ia`, `vicini+ia`) e per fascia di fiducia. È il dato più onesto per scegliere la soglia automatica.

**Prima di fidarsi dell'automatico**, `--valuta` ripropone la categoria di ogni documento già ordinato usando solo gli altri (escludendo le copie quasi identiche, altrimenti ogni documento ritroverebbe il proprio gemello) e mostra, per soglia di fiducia, quanti ne coprirebbe e quanti ne indovinerebbe. Si sceglie la soglia dove «giuste» è quasi 100%. Impostazioni: `CLASSIFICAZIONE_AUTO=0` per avere solo proposte e mai applicazioni automatiche, `CLASSIFICAZIONE_SOGLIA_AUTO`, `CLASSIFICAZIONE_VICINI`, `CLASSIFICAZIONE_SIMILITUDINE_MIN`, `CLASSIFICAZIONE_MODELLO`. Costo indicativo di Haiku: meno di un centesimo per documento. I documenti già indicizzati hanno il vettore calcolato dalla migrazione `0011`. Le immagini senza testo non hanno proposta («Nessuna proposta»).

### Ingestione automatica (cartella, posta, laptop)

Tre canali portano i file nello stesso percorso del caricamento da browser (stessi controlli, stessi doppioni) e chiedono la **classificazione automatica**: la categoria la propone il sistema e ciò che è incerto finisce in «Da smistare». I documenti in arrivo hanno il consenso IA **spento** (`INGESTIONE_CONSENSO_IA=0`) e appartengono a `INGESTIONE_UTENTE` (vuoto = primo superutente).

- **Cartella osservata sul server** (`inbox/arrivi/`, ogni `INGESTIONE_CARTELLA_OGNI` = 120 s, eseguita dal worker). Un file si prende solo se è fermo da `INGESTIONE_ETA_MINIMA` secondi (non si legge un file ancora in copia); una sottocartella dà il nome alla categoria; ciò che non si può importare va in `inbox/arrivi/_scartati/`. Dopo l'importazione il file viene tolto dalla cartella.
- **Casella email dedicata** (IMAP, ogni `INGESTIONE_POSTA_OGNI` = 300 s). Configurazione in `.env.prod`: `INGESTIONE_IMAP_HOST/UTENTE/PASSWORD` (Gmail: password per le app) e **`INGESTIONE_MITTENTI`, obbligatorio**. Sicurezza: si accetta solo un mittente nell'elenco *e* solo se l'intestazione `Authentication-Results` più in alto dice `dkim=pass` o `spf=pass` (altrimenti il mittente si falsifica in un attimo). Ogni allegato ammesso diventa un documento; un'email senza allegati diventa una nota di testo. Nulla viene cancellato dalla casella: i messaggi esaminati sono solo segnati come letti.
- **Laptop → server**: `scripts/installa_invio.sh CARTELLA [--ogni 30] [--togli]` mette in cron `scripts/invio_automatico.sh`, che spedisce la cartella con `invia_cartella.py --classifica`. Esce in silenzio se la cartella o il server non ci sono o se un altro invio è in corso; scrive in `~/.cache/djangovault/invio.log` solo quando ha inviato qualcosa o c'è un errore.

Verifica sul server: `./scripts/prod.sh ingestione stato` (non mostra la password), `ingestione cartella --prova`, `ingestione posta --prova` (legge senza importare né segnare come letto). Dopo aver cambiato `.env.prod`: `./scripts/prod.sh up`.

### HTTPS privato e app sul telefono (PWA)

**Perché serve HTTPS.** Un browser installa un sito come app, registra il service worker e dà accesso alla fotocamera solo su una connessione sicura. Con un certificato vero il server **resta privato** (rete di casa e WireGuard): niente porte aperte su Internet.

**Certificato con la sfida DNS** (`scripts/certificato.sh`, Let's Encrypt via acme.sh in Docker + acme-dns). Il nome (per esempio `dv.morpheoproject.com`) è un record **A** sul DNS del dominio che punta all'indirizzo *privato* del server (quello di casa, o quello di WireGuard); il router non deve inoltrare nessuna porta. Un CNAME una tantum delega la sfida ad acme-dns, così il rinnovo non ha più bisogno del DNS del dominio (va bene per qualsiasi provider, anche Aruba):

```bash
./scripts/certificato.sh emetti dv.morpheoproject.com   # 1ª volta: scrive il CNAME da aggiungere sul DNS (_acme-challenge.dv → <id>.auth.acme-dns.io)
# aggiungi quel CNAME, aspetta qualche minuto, poi rilancia lo stesso comando: ottiene il certificato in deploy/certs/
./scripts/certificato.sh attiva dv.morpheoproject.com   # modo https in .env.prod (copia di prima: .env.prod.http)
./scripts/prod.sh up
./scripts/certificato.sh cron                           # rinnovo automatico ogni notte
./scripts/certificato.sh stato                          # nome e scadenza; disattiva: torna al .env.prod di prima
```

Se il router non risolve il nome (alcuni bloccano i nomi pubblici che puntano a indirizzi privati: «protezione DNS rebinding») aggiungi un'eccezione per quel nome. Fuori casa il telefono deve avere WireGuard acceso e l'indirizzo del server tra gli `AllowedIPs`. `deploy/acme/` contiene le credenziali di acme-dns (non è nelle copie né nel pacchetto): se lo perdi, si rifà `emetti`.

**Installare l'app.** Apri `https://dv.morpheoproject.com/` dal telefono: Android/Chrome → menu ⋮ → *Installa app*; iPhone/Safari → Condividi → *Aggiungi alla schermata Home*. Dal tasto lungo sull'icona (Android) si arriva a «Scansiona», «Carica file» e «Da smistare».

**Condividere verso DV.** Android: dal menu *Condividi* di qualsiasi app (PDF, foto, un testo o un indirizzo web) scegli DjangoVault: i file si aprono nella pagina di caricamento e si confermano con *Carica* (un testo o un indirizzo diventa una nota). **iPhone non permette alle app web di comparire nel menu Condividi**: da Safari/File/Foto usa invece *Condividi → Mail* verso la casella di DV (vedi «Ingestione automatica»), oppure apri DV e scegli i file con «Carica più file».

**Scansionare con la fotocamera** (`/documenti/scansiona/`). Una foto per pagina (o dalla galleria), riordino e rotazione, filtro *Documento* (sfondo bianco, testo nitido) / *Grigi* / *Colori*; le pagine diventano un solo PDF, costruito nel browser, e passano dal caricamento normale (OCR sul server, classificazione automatica). Limiti: il filtro è semplice, non corregge la prospettiva né le ombre forti; consenso IA spento di default, perché le scansioni sono spesso personali.

**Riservatezza del service worker.** Mette da parte solo CSS, script e icone più la pagina «non raggiungibile». Pagine, documenti e risposte dell'assistente **non** si conservano mai sul telefono: offline l'app non mostra documenti.

### «Apri al passaggio» (fonti di Chiedi)

Ogni fonte di una risposta ha il link «Apri al passaggio»: per i PDF apre il file nel browser alla **pagina** in cui si trova il passaggio (`#page=N`); per gli altri tipi apre la scheda del documento. La pagina si ricava al momento: `pdftotext` dà il testo pagina per pagina e si cerca l'inizio del passaggio ignorando punteggiatura e a capo. Nelle scansioni (PDF senza testo vero, letto con l'OCR) la pagina non si trova e il PDF si apre alla prima. L'apertura passa dal controllo dei permessi e finisce nel registro («Apertura»); la risposta ha una CSP propria, con `object-src 'self'`, perché il lettore PDF del browser la richiede.

### Produzione (Fase 10)

**Che cosa gira** (`compose.prod.yml`, progetto `djangovault-prod`, volumi separati da quelli di sviluppo): Nginx (unico servizio esposto) → Daphne (HTTP e WebSocket) → Postgres + Redis (nessuna porta pubblicata) + il worker Celery. Il codice sta nell'immagine (nessun bind mount) e l'app gira come utente senza privilegi (uid 1000).

**Prima volta**

```bash
./scripts/tailwind.sh build                              # il CSS deve esserci nell'immagine
./scripts/prod.sh init dms.example.org                   # crea .env.prod con segreti casuali (o: init 192.168.1.9 --http)
nano .env.prod                                           # controlla i valori, metti ANTHROPIC_API_KEY
./scripts/prod.sh up                                     # controlla .env.prod, costruisce e avvia
./scripts/prod.sh superuser                              # primo amministratore
```

`./scripts/prod.sh check` verifica da solo i casi tipici di errore (segreti rimasti «cambiami», host d'esempio, certificati mancanti, `DEBUG` acceso, modo http senza `DJANGO_SECURE_SSL=0`). Altri comandi: `status`, `logs [servizio]`, `down`.

**HTTP o HTTPS?** `DV_NGINX_MODE`:
- `https`: Nginx termina il TLS (TLS 1.2/1.3), porta 80 → 301 verso 443. Servono `fullchain.pem` e `privkey.pem` in `DV_CERTS_DIR` (di default `deploy/certs/`). Con Let's Encrypt, prima del primo avvio (la porta 80 deve essere libera):
  ```bash
  sudo apt install -y certbot
  sudo certbot certonly --standalone -d dms.example.org
  sudo cp /etc/letsencrypt/live/dms.example.org/{fullchain,privkey}.pem deploy/certs/ && sudo chown $USER deploy/certs/*.pem
  ```
  Per il rinnovo (certbot lo programma da solo) aggiungi un «deploy hook» che ricopia i file e ricarica Nginx: `docker compose -f compose.prod.yml exec nginx nginx -s reload`. Quando il HTTPS funziona da qualche giorno si può attivare `DJANGO_HSTS_SECONDS=31536000`.
- `http` (con `DJANGO_SECURE_SSL=0`): per una rete interna fidata o con un altro proxy/tunnel davanti che porta già il HTTPS. Senza HTTPS le password viaggiano in chiaro.

**Cosa fa Nginx** (`deploy/nginx/`): file statici dal volume raccolto da `collectstatic`; nessuna rotta `/media/` (i documenti passano sempre da una view che controlla i permessi); `/ws/` con l'upgrade a WebSocket; lo streaming di «Chiedi» senza buffer; al massimo 30 richieste/minuto per indirizzo sull'accesso (`limit_req`); dimensione massima della richiesta `DV_NGINX_MAX_BODY` (di poco sopra `DOCUMENTS_MAX_UPLOAD_MB`); `X-Forwarded-For` e `Host` fissati da Nginx (per questo `AUDIT_TRUST_X_FORWARDED_FOR=1` è sicuro qui; se davanti a Nginx c'è un altro proxy, l'IP registrato sarà il suo). Le intestazioni di sicurezza (CSP, X-Frame-Options…) le mette Django.

**Copie di sicurezza**

```bash
./scripts/prod.sh backup                       # backups/<data>/{db.dump, media.tar.gz, SHA256SUMS}; si tengono le ultime 14 (DV_BACKUP_KEEP)
./scripts/prod.sh restore backups/<data> --yes # SOVRASCRIVE database e file attuali (controlla prima l'integrità)
```

**Copia notturna automatica e copia sul laptop** (script con cron, rilanciabili senza creare doppioni):

```bash
# sul SERVER, dal progetto: ogni notte alle 03:30 fa `prod.sh backup` (log in backups/backup.log)
./scripts/installa_backup.sh server
# sul LAPTOP: servono solo due file (installa_backup.sh e scarica_backup.sh), non tutto il progetto
mkdir -p ~/bin/djangovault && scp scrivania:progetti/djangovault/scripts/{installa_backup.sh,scarica_backup.sh} ~/bin/djangovault/
chmod +x ~/bin/djangovault/*.sh && ~/bin/djangovault/installa_backup.sh laptop   # ogni ora scarica le copie nuove in ~/Backup/djangovault
```

`scarica_backup.sh` scarica solo le copie nuove, ne controlla l'SHA-256 (una copia incompleta o danneggiata viene scartata e riscaricata), non cancella mai nulla per ordine del server (tiene le ultime 30, `DV_LOCALE_KEEP`) e, se il server è spento, non fa nulla e riprova all'ora dopo. Variabili: `DV_SERVER`, `DV_REMOTO`, `DV_DEST`. Per toglierle: `installa_backup.sh server|laptop --togli`. **`.env.prod` non è nelle copie** (contiene i segreti): conservane una copia a parte. Se il server è spento alle 03:30 la copia di quella notte salta (cron non recupera).

La copia sta sulla stessa macchina: va portata anche fuori (altro disco, altro computer, `rsync`/`scp` programmato con cron). Per portare in produzione i dati dello sviluppo: `DV_STACK=dev ./scripts/prod.sh backup`, poi `./scripts/prod.sh up` una volta (crea volumi e tabelle) e `./scripts/prod.sh restore backups/<data> --yes`. Dopo un ripristino su un modello di ricerca diverso: `indicizza_versioni`.

**Manutenzione periodica** (Celery beat, ogni 24 ore, anche a mano con `python manage.py manutenzione`): sessioni scadute; notifiche **già lette** più vecchie di `NOTIFICATION_RETENTION_DAYS` (90); voci del registro più vecchie di `AUDIT_RETENTION_DAYS` (0 = non si cancella mai).

**Aggiornare**: `tar xzf …`, poi `./scripts/prod.sh up` (le migrazioni partono da sole all'avvio). Fai prima un `backup`.

**Limiti noti**: un solo server (niente alta disponibilità); l'anteprima e il download dei file passano da Daphne (con ASGI Django legge il file in memoria prima di inviarlo: va bene per `DOCUMENTS_MAX_UPLOAD_MB` = 50, non per file molto più grandi).
