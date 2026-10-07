"""Configurazione di DjangoVault.

Tutto ciò che cambia tra un ambiente e l'altro (segreti, database, host, porte)
arriva da variabili d'ambiente. In sviluppo si possono mettere in `.env`
(vedi `.env.example`); `.env` non va mai versionato.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Lettura dell'ambiente
# --------------------------------------------------------------------------

def _carica_dotenv(percorso: Path) -> None:
    """Carica KEY=valore da .env senza sovrascrivere ciò che è già nell'ambiente."""
    if not percorso.is_file():
        return
    for riga in percorso.read_text(encoding="utf-8").splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        chiave, _, valore = riga.partition("=")
        os.environ.setdefault(chiave.strip(), valore.strip().strip("'\""))


_carica_dotenv(BASE_DIR / ".env")


def env(nome: str, default: str | None = None) -> str | None:
    return os.environ.get(nome, default)


def env_bool(nome: str, default: bool = False) -> bool:
    valore = os.environ.get(nome)
    if valore is None:
        return default
    return valore.strip().lower() in {"1", "true", "yes", "on", "si", "sì"}


def env_int(nome: str, default: int = 0) -> int:
    valore = os.environ.get(nome)
    return int(valore) if valore not in (None, "") else default


def env_list(nome: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.environ.get(nome, default).split(",") if v.strip()]


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "solo-sviluppo-non-usare-in-produzione"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY è obbligatoria quando DJANGO_DEBUG è spento.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Percorso dell'admin configurabile (in produzione conviene un percorso non ovvio).
ADMIN_URL = env("DJANGO_ADMIN_PATH", "admin/")


# --------------------------------------------------------------------------
# Applicazioni
# --------------------------------------------------------------------------
# Regola del progetto: nuove app solo per responsabilità reali del dominio.

INSTALLED_APPS = [
    "daphne",  # prima di staticfiles: `runserver` diventa ASGI e serve anche i WebSocket
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "channels",
    "accounts",
    "core",
    "documents",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.RequestContextMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.notifiche",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database (PostgreSQL + pgvector) e cache (Redis)
# --------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "djangovault"),
        "USER": env("DB_USER", "djangovault"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "127.0.0.1"),
        # In Docker compose imposta DB_PORT=5432; fuori da Docker vale la porta pubblicata.
        "PORT": env("DB_PORT") or env("DV_DB_PORT", "5440"),
        # Una connessione per richiesta, sempre. Sotto ASGI (Daphne, anche `runserver` con Channels) ogni richiesta gira in un
        # thread proprio che poi muore: una connessione «persistente» resterebbe aperta senza che nessuno la chiuda e,
        # a poco a poco, il database esaurisce i posti («sorry, too many clients already»). Django stesso sconsiglia le
        # connessioni persistenti con ASGI. Chi ha un carico enorme usi un pooler (PgBouncer), non CONN_MAX_AGE.
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 0),
        "CONN_HEALTH_CHECKS": True,
    }
}

REDIS_URL = env("REDIS_URL") or (
    f"redis://127.0.0.1:{env('DV_REDIS_PORT')}/0" if env("DV_REDIS_PORT") else None
)
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "djangovault",
        }
    }
else:
    # Senza Redis (per esempio nei test) si usa la cache in memoria del processo.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


# --------------------------------------------------------------------------
# Autenticazione
# --------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"


# --------------------------------------------------------------------------
# Internazionalizzazione
# --------------------------------------------------------------------------

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# File statici e media
# --------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# I media NON vengono mai serviti pubblicamente: i documenti passeranno sempre da
# una view autenticata che controlla i permessi (nessuna rotta /media/ in urls.py
# e nessun `location /media/` in Nginx).
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Sicurezza
# --------------------------------------------------------------------------

# Content Security Policy: niente script né stili inline, tutto dallo stesso host.
# Per questo motivo il progetto usa la build CSP di Alpine.js e non contiene
# <script> o style="" inline nei template.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],  # blob: per le anteprime delle foto della scansione
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

if not DEBUG:
    # In produzione dietro Nginx con HTTPS.
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL", True)
    SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
    CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0)
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]  # il controllo di Docker arriva in HTTP, senza passare dal proxy
    if env_bool("DJANGO_BEHIND_PROXY", True):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# --------------------------------------------------------------------------
# Log
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"semplice": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "semplice"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}


# --------------------------------------------------------------------------
# Servizi esterni (usati nelle fasi successive)
# --------------------------------------------------------------------------

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")

# Assistente IA (Fase 8): risponde alle domande sui documenti citando i passaggi (RAG).
AI_MODEL = env("AI_MODEL", "claude-sonnet-4-6")  # risposta
AI_QUERY_MODEL = env("AI_QUERY_MODEL", "claude-haiku-4-5")  # riformulazione della domanda ("" = non riformulare)
AI_BACKEND = env("AI_BACKEND", "assistant.llm.AnthropicLLM")  # i test usano un finto
AI_MAX_PASSAGES = env_int("AI_MAX_PASSAGES", 12)  # passaggi inviati al modello per domanda
AI_MAX_PER_DOCUMENT = env_int("AI_MAX_PER_DOCUMENT", 3)
AI_MAX_OCCURRENCES = env_int("AI_MAX_OCCURRENCES", 15)  # documenti elencati per ogni nome/termine cercato alla lettera
AI_MIN_SCORE = float(env("AI_MIN_SCORE", "0.80"))  # similarità minima di un passaggio (solo ricerca su tutto l'archivio)
# Un passaggio deve stare entro questo margine dal migliore trovato: evita di portarsi dietro testi solo vagamente affini.
AI_SCORE_MARGIN = float(env("AI_SCORE_MARGIN", "0.06"))
AI_MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 1500)
AI_MAX_QUESTION_CHARS = env_int("AI_MAX_QUESTION_CHARS", 1000)
AI_MAX_QUESTIONS_PER_HOUR = env_int("AI_MAX_QUESTIONS_PER_HOUR", 30)  # per utente
AI_HISTORY_MESSAGES = env_int("AI_HISTORY_MESSAGES", 6)  # messaggi precedenti ripresi nelle domande successive
AI_TIMEOUT_SECONDS = env_int("AI_TIMEOUT_SECONDS", 60)


# --------------------------------------------------------------------------
# Documenti
# --------------------------------------------------------------------------

DOCUMENTS_MAX_UPLOAD_MB = env_int("DOCUMENTS_MAX_UPLOAD_MB", 50)
DOCUMENTS_ALLOWED_EXTENSIONS = [
    e.lower().lstrip(".")
    for e in env_list(
        "DOCUMENTS_ALLOWED_EXTENSIONS",
        "pdf,doc,docx,odt,rtf,txt,md,xls,xlsx,ods,csv,ppt,pptx,odp,png,jpg,jpeg,gif,webp",
    )
]


# --------------------------------------------------------------------------
# Registro delle operazioni
# --------------------------------------------------------------------------

# Attivare SOLO quando davanti all'app c'è un proxy fidato (Nginx, Fase 10) che imposta
# X-Forwarded-For: altrimenti l'IP nel registro sarebbe falsificabile.
AUDIT_TRUST_X_FORWARDED_FOR = env_bool("AUDIT_TRUST_X_FORWARDED_FOR", False)


# --------------------------------------------------------------------------
# Elaborazione asincrona (Fase 6): Celery + Redis
# --------------------------------------------------------------------------
# Il database è la fonte di verità: lo stato di ogni versione (in coda, in corso, completata,
# errore) sta in `DocumentVersion.processing_status`. Redis fa solo da campanello: se un
# messaggio si perde, `documents.riprendi_elaborazioni` (ogni 5 minuti) rimette in coda.

def _redis_con_db(url: str, db: int) -> str:
    """Stesso server Redis, database numerato diverso: la cache e la coda non si mescolano."""
    parti = urlsplit(url)
    return urlunsplit(parti._replace(path=f"/{db}"))


# Tempo reale (Fase 9): WebSocket con Channels. Il livello dei canali usa Redis (db 2); senza Redis, e nei test,
# resta in memoria del processo (basta per un solo processo, non per web + worker).
ASGI_APPLICATION = "config.asgi.application"
if REDIS_URL and not (len(sys.argv) > 1 and sys.argv[1] == "test"):
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [_redis_con_db(REDIS_URL, 2)]}}
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
# Conservazione (0 = per sempre). Il registro è la prova di chi ha fatto cosa: per default non si cancella.
AUDIT_RETENTION_DAYS = env_int("AUDIT_RETENTION_DAYS", 0)
NOTIFICATION_RETENTION_DAYS = env_int("NOTIFICATION_RETENTION_DAYS", 90)  # solo notifiche già lette
REALTIME_MAX_DOCUMENTS = env_int("REALTIME_MAX_DOCUMENTS", 30)  # documenti seguiti da una sola connessione

CELERY_BROKER_URL = env("CELERY_BROKER_URL") or (_redis_con_db(REDIS_URL, 1) if REDIS_URL else "memory://")
if len(sys.argv) > 1 and sys.argv[1] == "test":
    CELERY_BROKER_URL = "memory://"  # i test non devono mai scrivere nella coda Redis vera
CELERY_RESULT_BACKEND = None  # i risultati non servono: lo stato sta nel database
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_EAGER", False)  # True = esegue subito, senza worker
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # un file pesante non blocca i task in coda dietro di sé
CELERY_WORKER_MAX_TASKS_PER_CHILD = 50  # ricicla i processi: LibreOffice e OCR non perdono memoria a lungo
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Pubblicare un task non deve bloccare il caricamento di un file se Redis è fermo.
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_TASK_PUBLISH_RETRY_POLICY = {"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5}
CELERY_TASK_SOFT_TIME_LIMIT = env_int("PROCESSING_SOFT_TIME_LIMIT", 600)
CELERY_TASK_TIME_LIMIT = CELERY_TASK_SOFT_TIME_LIMIT + 60

PROCESSING_OCR_LANGS = env("PROCESSING_OCR_LANGS", "ita+eng")
PROCESSING_OCR_MAX_PAGES = env_int("PROCESSING_OCR_MAX_PAGES", 30)  # pagine massime sottoposte a OCR
PROCESSING_OCR_DPI = env_int("PROCESSING_OCR_DPI", 200)
PROCESSING_TEXT_MAX_CHARS = env_int("PROCESSING_TEXT_MAX_CHARS", 2_000_000)
PROCESSING_SUBPROCESS_TIMEOUT = env_int("PROCESSING_SUBPROCESS_TIMEOUT", 120)  # secondi per ogni programma esterno
PROCESSING_PREVIEW_WIDTH = env_int("PROCESSING_PREVIEW_WIDTH", 720)
PROCESSING_MAX_IMAGE_PIXELS = env_int("PROCESSING_MAX_IMAGE_PIXELS", 100_000_000)
PROCESSING_MAX_ATTEMPTS = env_int("PROCESSING_MAX_ATTEMPTS", 3)
PROCESSING_STALE_MINUTES = env_int("PROCESSING_STALE_MINUTES", 15)  # "in corso" da più di così = interrotta
# LibreOffice (facoltativo): anteprime di Word/Excel/PowerPoint e testo dei formati vecchi (doc, xls, ppt, rtf).
PROCESSING_SOFFICE = env("PROCESSING_SOFFICE", "soffice")


# --------------------------------------------------------------------------
# Ricerca (Fase 7)
# --------------------------------------------------------------------------
# Configurazione PostgreSQL per l'italiano senza accenti ("societa" trova "società"): la crea la migration 0008.
SEARCH_CONFIG = "italian_unaccent"

# Ricerca per significato: embeddings in locale (vedi documents/embeddings.py).
EMBEDDING_BACKEND = env("EMBEDDING_BACKEND", "documents.embeddings.SentenceTransformerEmbedder")
if len(sys.argv) > 1 and sys.argv[1] == "test":
    AI_BACKEND = "assistant.tests.fakes.FakeLLM"  # i test non chiamano mai l'API vera
    ANTHROPIC_API_KEY = "chiave-di-prova"
    AI_MIN_SCORE = 0.3
    AI_SCORE_MARGIN = 1.0  # i test con l'embedder finto hanno punteggi molto sparsi: il margine si prova a parte
    EMBEDDING_BACKEND = "documents.tests.fakes.FakeEmbedder"  # i test non scaricano né caricano il modello vero
EMBEDDING_MODEL = env("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
EMBEDDING_DIM = 384  # fissa: è la dimensione della colonna nel database
EMBEDDING_THREADS = env_int("EMBEDDING_THREADS", 2)  # thread CPU usati da PyTorch in ogni processo
# Ingestione automatica (documents/services/ingestione.py): cartella osservata e casella email. Entrambe usano lo
# stesso percorso del caricamento da browser (stessi controlli, stessi doppioni) e chiedono la classificazione automatica.
INGESTIONE_UTENTE = env("INGESTIONE_UTENTE", "")  # proprietario dei documenti; vuoto = il primo superutente
INGESTIONE_CONSENSO_IA = env_bool("INGESTIONE_CONSENSO_IA", False)  # i documenti in arrivo possono essere letti dall'IA?
INGESTIONE_IMMAGINI = env_bool("INGESTIONE_IMMAGINI", False)  # PNG/JPG/… senza testo: di norma si scartano
INGESTIONE_CARTELLA = env("INGESTIONE_CARTELLA", str(BASE_DIR / "inbox" / "arrivi"))
INGESTIONE_CARTELLA_OGNI = env_int("INGESTIONE_CARTELLA_OGNI", 120)  # secondi fra un controllo e l'altro; 0 = spenta
INGESTIONE_ETA_MINIMA = env_int("INGESTIONE_ETA_MINIMA", 60)  # un file si importa solo se fermo da almeno N secondi
INGESTIONE_IMAP_HOST = env("INGESTIONE_IMAP_HOST", "")  # vuoto = posta spenta
INGESTIONE_IMAP_PORTA = env_int("INGESTIONE_IMAP_PORTA", 993)
INGESTIONE_IMAP_UTENTE = env("INGESTIONE_IMAP_UTENTE", "")
INGESTIONE_IMAP_PASSWORD = env("INGESTIONE_IMAP_PASSWORD", "")  # per Gmail: «password per le app», non quella dell'account
INGESTIONE_IMAP_CARTELLA = env("INGESTIONE_IMAP_CARTELLA", "INBOX")
INGESTIONE_POSTA_OGNI = env_int("INGESTIONE_POSTA_OGNI", 300)  # secondi; 0 = spenta
# Chi può spedire documenti: indirizzi completi o interi domini («@studio.it»). Senza elenco la posta NON funziona.
INGESTIONE_MITTENTI = [m.lower() for m in env_list("INGESTIONE_MITTENTI")]
# Il mittente si legge dal messaggio e si può falsificare: si accetta solo se il tuo provider ha verificato SPF o DKIM.
INGESTIONE_RICHIEDI_AUTENTICAZIONE = env_bool("INGESTIONE_RICHIEDI_AUTENTICAZIONE", True)
INGESTIONE_POSTA_CORPO = env_bool("INGESTIONE_POSTA_CORPO", True)  # una email senza allegati diventa una nota di testo

# Classificazione automatica (documents/services/classificazione.py): proposta di categoria dai documenti simili
# (in locale) e, solo per i documenti con consenso IA, da Claude Haiku. L'applicazione automatica scatta solo sopra la
# soglia di fiducia e solo per documenti ancora senza categoria; si annulla dalla pagina «Da smistare».
# Le soglie vanno tarate sui propri dati con `manage.py classifica --valuta`.
CLASSIFICAZIONE_VICINI = env_int("CLASSIFICAZIONE_VICINI", 5)  # documenti simili che «votano»
CLASSIFICAZIONE_SIMILITUDINE_MIN = float(env("CLASSIFICAZIONE_SIMILITUDINE_MIN", "0.15"))  # sotto: nessun voto (vettori centrati)
CLASSIFICAZIONE_AUTO = env_bool("CLASSIFICAZIONE_AUTO", True)  # false = solo proposte, mai applicazioni automatiche
CLASSIFICAZIONE_SOGLIA_AUTO = float(env("CLASSIFICAZIONE_SOGLIA_AUTO", "0.85"))
CLASSIFICAZIONE_MODELLO = env("CLASSIFICAZIONE_MODELLO", "") or AI_QUERY_MODEL  # Haiku; vuoto = quello della ricerca
CLASSIFICAZIONE_CARATTERI = env_int("CLASSIFICAZIONE_CARATTERI", 3000)  # estratto inviato al modello (solo con consenso IA)
SEARCH_CHUNK_CHARS = env_int("SEARCH_CHUNK_CHARS", 1000)
SEARCH_CHUNK_OVERLAP = env_int("SEARCH_CHUNK_OVERLAP", 150)
SEARCH_MAX_CHUNKS = env_int("SEARCH_MAX_CHUNKS", 1000)  # per versione: oltre, il testo non viene indicizzato
# I punteggi E5 stanno in una fascia stretta (≈0,75 senza relazione, ≥0,82 con relazione): sotto la soglia un
# passaggio non è considerato pertinente. Alzarla = risultati meno numerosi e più mirati.
SEARCH_SEMANTIC_MIN_SCORE = float(env("SEARCH_SEMANTIC_MIN_SCORE", "0.80"))
