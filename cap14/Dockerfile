FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# L'ambiente virtuale sta fuori da /app, così il bind mount del codice in sviluppo
# non lo nasconde.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Strumenti dell'elaborazione dei file (Fase 6): poppler (testo e anteprime dei PDF) e Tesseract (OCR).
# LibreOffice è facoltativo (~600 MB): dà le anteprime di Word/Excel/PowerPoint e la lettura dei vecchi
# .doc/.xls/.ppt/.rtf. Per includerlo: INSTALL_LIBREOFFICE=1 in .env, poi `docker compose build`.
ARG INSTALL_LIBREOFFICE=0
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-ita tesseract-ocr-eng \
    && if [ "$INSTALL_LIBREOFFICE" = "1" ]; then \
         apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu-core; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Utente senza privilegi: lo usa la produzione (compose.prod.yml, `user: 1000:1000`). In sviluppo il codice è un
# bind mount e il contenitore resta root, come prima. Le cartelle scrivibili nascono già sue, così i volumi
# Docker nuovi ne ereditano il proprietario.
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app/media /app/staticfiles /models \
    && chown -R app:app /app /models

WORKDIR /app

# Prima solo le dipendenze: il layer si ricostruisce solo se cambiano. La cache di uv resta tra una build e l'altra
# (BuildKit): se cambia una sola dipendenza si scarica solo quella, non anche torch da capo.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

COPY --chown=app:app . .

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
