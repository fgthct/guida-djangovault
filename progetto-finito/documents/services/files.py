import hashlib
import mimetypes


def sha256_file(uploaded) -> str:
    """SHA-256 del file caricato, letto a blocchi (nessun file intero in memoria)."""
    h = hashlib.sha256()
    uploaded.seek(0)
    for blocco in uploaded.chunks(1024 * 1024):
        h.update(blocco)
    uploaded.seek(0)
    return h.hexdigest()


def tipo_contenuto(uploaded) -> str:
    return (mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream")[:100]
