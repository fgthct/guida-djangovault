"""Classificazione automatica: propone la categoria (e un riassunto con parole chiave) di un documento.

Due fonti indipendenti:

1. **Documenti simili** (in locale, gratis, nessun dato esce dal server). Ogni versione ha un vettore (la media dei
   suoi passaggi, `DocumentVersion.doc_vector`). Si cercano i documenti già categorizzati più vicini e ciascuno «vota»
   la propria categoria con il peso della somiglianza. I vettori si *centrano* (si toglie la media dell'archivio),
   altrimenti tutti i documenti sembrerebbero simili fra loro. Impara le convenzioni di chi ha ordinato l'archivio.
2. **Claude Haiku**, solo per documenti con il consenso IA: legge l'inizio del testo, conosce le categorie esistenti
   (con qualche titolo di esempio) e propone categoria, riassunto e parole chiave.

La fiducia sale quando le due fonti concordano. Sopra la soglia (`CLASSIFICAZIONE_SOGLIA_AUTO`) e solo per documenti
ancora senza categoria, la proposta viene applicata da sola ma resta annullabile; sotto, finisce nella coda
«Da smistare». Una categoria *nuova* non viene mai creata senza che una persona accetti.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.services import audit

from ..models import Category, ClassificationSuggestion, Document, DocumentVersion

logger = logging.getLogger(__name__)
S = ClassificationSuggestion.Status
SIM_PIENA = 0.5  # somiglianza (centrata) oltre la quale la vicinanza conta per intero nella fiducia
MAX_CATEGORIE_AL_MODELLO = 60


# --------------------------------------------------------------------------
# Richiesta e coda
# --------------------------------------------------------------------------

def richiedi(document: Document) -> ClassificationSuggestion:
    """Segna il documento come «da classificare appena indicizzato» (idempotente: non tocca decisioni già prese)."""
    sug, creata = ClassificationSuggestion.objects.get_or_create(document=document)
    if not creata and sug.status in (S.NONE, S.UNDONE, S.REJECTED):
        sug.status = S.REQUESTED
        sug.save(update_fields=["status", "updated_at"])
    return sug


def accoda_se_richiesta(document_id: int) -> None:
    """Da chiamare quando una versione è stata indicizzata: se il documento aspettava la classificazione, parte."""
    if ClassificationSuggestion.objects.filter(document_id=document_id, status=S.REQUESTED).exists():
        transaction.on_commit(lambda: _invia(document_id))


def _invia(document_id: int) -> bool:
    from ..tasks import classifica_documento_task

    try:
        classifica_documento_task.delay(document_id)
        return True
    except Exception:
        logger.warning("Broker non raggiungibile: il documento %s sarà classificato dal controllo periodico.", document_id, exc_info=True)
        return False


def riprendi_classificazioni(*, massimo: int = 20) -> int:
    """Rete di sicurezza periodica: classifica i documenti rimasti «in attesa» e già pronti; chiude quelli senza testo."""
    fatti = 0
    P = DocumentVersion.Processing
    for sug in ClassificationSuggestion.objects.filter(status=S.REQUESTED).select_related("document__current_version")[:massimo]:
        v = sug.document.current_version
        if v is None:
            continue
        if v.processing_status in (P.SKIPPED, P.FAILED) and _e_un_immagine(v):
            classifica_documento(sug.document_id)
            fatti += 1
        elif v.processing_status in (P.SKIPPED, P.FAILED):
            _chiudi_senza_proposta(sug, "Nessun testo da cui classificare.")
            fatti += 1
        elif v.processing_status == P.DONE and v.indexed_model:
            classifica_documento(sug.document_id)
            fatti += 1
    return fatti


# --------------------------------------------------------------------------
# Documenti simili
# --------------------------------------------------------------------------

@dataclass
class Voto:
    categoria_id: int
    nome: str
    peso: float
    fiducia: float  # 0..1: quota dei voti × vicinanza del migliore
    vicini: list[dict] = field(default_factory=list)


class Archivio:
    """I vettori (centrati) dei documenti già categorizzati visibili a un utente: si carica una volta per lotto."""

    def __init__(self, owner):
        righe = list(
            Document.objects.visible_to(owner)
            .filter(category__isnull=False, current_version__doc_vector__isnull=False)
            .values_list("pk", "title", "category_id", "category__name", "current_version__doc_vector")
        )
        self.ids = [r[0] for r in righe]
        self.titoli = [r[1] for r in righe]
        self.cat_ids = [r[2] for r in righe]
        self.cat_nomi = [r[3] for r in righe]
        if righe:
            grezza = np.vstack([np.asarray(r[4], dtype=np.float32) for r in righe])
            self.media = grezza.mean(axis=0)
            self.matrice = self._normalizza(grezza - self.media)
        else:
            self.media = None
            self.matrice = np.zeros((0, settings.EMBEDDING_DIM), dtype=np.float32)

    @staticmethod
    def _normalizza(m: np.ndarray) -> np.ndarray:
        norme = np.linalg.norm(m, axis=-1, keepdims=True)
        norme[norme == 0] = 1.0
        return m / norme

    def __len__(self) -> int:
        return len(self.ids)

    def somiglianze(self, vettore) -> np.ndarray:
        """Coseno fra il vettore (centrato con la media dell'archivio) e ogni documento categorizzato."""
        if not len(self):
            return np.zeros(0, dtype=np.float32)
        q = self._normalizza(np.asarray(vettore, dtype=np.float32) - self.media)
        return self.matrice @ q

    def vota(self, sims: np.ndarray, *, escludi_pk: int | None = None, k: int | None = None) -> Voto | None:
        """Le k categorie più votate fra i documenti più simili; None se nessuno è abbastanza vicino."""
        k = k or settings.CLASSIFICAZIONE_VICINI
        sims = sims.copy()
        if escludi_pk is not None and escludi_pk in self.ids:
            sims[self.ids.index(escludi_pk)] = -1.0
        ordine = np.argsort(-sims)[:k]
        scelti = [i for i in ordine if sims[i] >= settings.CLASSIFICAZIONE_SIMILITUDINE_MIN]
        if not scelti:
            return None
        pesi: dict[int, float] = {}
        for i in scelti:
            pesi[self.cat_ids[i]] = pesi.get(self.cat_ids[i], 0.0) + float(sims[i])
        migliore = max(pesi, key=pesi.get)
        quota = pesi[migliore] / sum(pesi.values())
        vicinanza = min(1.0, float(sims[scelti[0]]) / SIM_PIENA)
        nome = next(self.cat_nomi[i] for i in scelti if self.cat_ids[i] == migliore)
        return Voto(
            categoria_id=migliore,
            nome=nome,
            peso=pesi[migliore],
            fiducia=round(quota * vicinanza, 4),
            vicini=[
                {"id": self.ids[i], "titolo": self.titoli[i][:80], "categoria": self.cat_nomi[i], "sim": round(float(sims[i]), 3)}
                for i in scelti
            ],
        )


def vettore_della_versione(v: DocumentVersion):
    """Il vettore del documento; se manca (versione indicizzata prima di questa funzione) lo calcola dai passaggi."""
    if v.doc_vector is not None:
        return np.asarray(v.doc_vector, dtype=np.float32)
    return salva_vettore(v, [c for c in v.chunks.values_list("embedding", flat=True)])


def salva_vettore(v: DocumentVersion, vettori) -> "np.ndarray | None":
    """Il vettore del documento è la media dei vettori dei suoi passaggi. Lo scrive senza toccare altro."""
    if not len(vettori):
        return None
    media = np.mean(np.vstack([np.asarray(x, dtype=np.float32) for x in vettori]), axis=0)
    DocumentVersion.objects.filter(pk=v.pk).update(doc_vector=media)
    return media


# --------------------------------------------------------------------------
# Classificazione di un documento
# --------------------------------------------------------------------------

def _categorie_per_il_modello() -> list[dict]:
    from django.db.models import Count

    cat = list(Category.objects.annotate(n=Count("documents")).filter(n__gt=0).order_by("-n")[:MAX_CATEGORIE_AL_MODELLO])
    risultato = []
    for c in cat:
        esempi = list(c.documents.order_by("-updated_at").values_list("title", flat=True)[:4])
        risultato.append({"nome": c.name, "n": c.n, "descrizione": c.description, "esempi": [e[:60] for e in esempi]})
    return risultato


def _chiedi_al_modello(documento: Document, v: DocumentVersion):
    """Claude Haiku, solo con il consenso IA e con una chiave configurata. None se non applicabile."""
    if not documento.ai_allowed or not settings.ANTHROPIC_API_KEY:
        return None
    estratto = (v.extracted_text or "").strip()[: settings.CLASSIFICAZIONE_CARATTERI]
    if len(estratto) < 80:
        return None
    from assistant.llm import get_llm

    try:
        return get_llm().classifica(
            titolo=documento.title, nome_file=v.original_name, estratto=estratto, categorie=_categorie_per_il_modello()
        )
    except Exception:
        logger.warning("Classificazione con il modello non riuscita per il documento %s", documento.pk, exc_info=True)
        return None


@dataclass
class Decisione:
    categoria: Category | None = None
    nuova: str = ""
    fiducia: float = 0.0
    fonte: str = ""
    motivo: str = ""


def combina(voto: Voto | None, ia, categorie_per_nome: dict[str, Category]) -> Decisione:
    """Mette insieme le due fonti. Regole (la fiducia è sempre 0..1):

    - d'accordo → 0,5 + metà della fiducia dei vicini (le due fonti sono indipendenti: si rafforzano);
    - solo i vicini → la loro fiducia; solo l'IA con categoria esistente → 0,5;
    - in disaccordo → vince chi ha un'idea forte (vicini ≥ 0,5: conosce le convenzioni di chi ha ordinato l'archivio),
      con fiducia ridotta; altrimenti l'IA, con fiducia bassa;
    - l'IA propone una categoria nuova → solo se i vicini non hanno un'idea forte; fiducia 0,4, mai applicata da sola.
    """
    cat_ia = categorie_per_nome.get((ia.categoria or "").lower()) if ia else None
    nuova_ia = (ia.nuova_categoria or "").strip() if ia else ""
    cat_vic = Category.objects.filter(pk=voto.categoria_id).first() if voto else None
    forte_vic = voto is not None and cat_vic is not None and voto.fiducia >= 0.5

    if cat_vic and cat_ia and cat_vic.pk == cat_ia.pk:
        return Decisione(cat_vic, fiducia=round(0.5 + 0.5 * voto.fiducia, 3), fonte="vicini+ia", motivo="I documenti simili e l'IA indicano la stessa categoria.")
    if cat_vic and cat_ia:
        if forte_vic:
            return Decisione(cat_vic, fiducia=round(voto.fiducia * 0.6, 3), fonte="vicini",
                             motivo=f"I documenti simili stanno in «{cat_vic.name}»; l'IA proponeva «{cat_ia.name}».")
        return Decisione(cat_ia, fiducia=0.35, fonte="ia",
                         motivo=f"L'IA propone «{cat_ia.name}»; i documenti simili (poco vicini) stanno in «{cat_vic.name}».")
    if cat_ia and not cat_vic:
        return Decisione(cat_ia, fiducia=0.5, fonte="ia", motivo="Proposta dall'IA: nessun documento già ordinato è abbastanza simile.")
    if nuova_ia and not forte_vic:
        return Decisione(None, nuova=nuova_ia, fiducia=0.4, fonte="ia", motivo=f"Nessuna categoria esistente va bene: l'IA propone di crearne una, «{nuova_ia}».")
    if cat_vic:
        return Decisione(cat_vic, fiducia=voto.fiducia, fonte="vicini", motivo="Proposta dai documenti simili già ordinati.")
    return Decisione()


CATEGORIA_IMMAGINI = "Immagini e foto"


def _e_un_immagine(v: DocumentVersion) -> bool:
    nome = (v.original_name or "").lower()
    return (v.content_type or "").startswith("image/") or nome.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp", ".tif", ".tiff"))


def _proposta_immagine(per_nome: dict[str, Category]) -> Decisione:
    """Un'immagine senza testo non ha nulla da confrontare: si propone la categoria delle immagini (sempre da confermare)."""
    esistente = per_nome.get(CATEGORIA_IMMAGINI.lower())
    motivo = "È un'immagine senza testo: la categoria va decisa da te, ti propongo quella delle immagini."
    if esistente:
        return Decisione(esistente, fiducia=0.5, fonte="tipo di file", motivo=motivo)
    return Decisione(None, nuova=CATEGORIA_IMMAGINI, fiducia=0.4, fonte="tipo di file", motivo=motivo)


def classifica_documento(document_id: int, *, usa_ia: bool = True, applica: bool | None = None, archivio: Archivio | None = None, salva: bool = True):
    """Classifica un documento senza categoria. Ritorna la proposta salvata (o, con `salva=False`, la `Decisione`).

    `applica`: None = come da impostazioni (auto sopra soglia), False = mai, True = sopra soglia anche se in
    impostazioni è spento (usato dal comando a mano).
    """
    documento = Document.objects.select_related("current_version", "owner").filter(pk=document_id).first()
    if documento is None or documento.current_version is None:
        return None
    sug = ClassificationSuggestion.objects.filter(document=documento).first() if salva else None
    if documento.category_id is not None:
        if sug is not None and sug.status == S.REQUESTED:
            _chiudi_senza_proposta(sug, "Il documento ha già una categoria.")
        return None
    v = documento.current_version
    vettore = vettore_della_versione(v)

    voto = None
    if vettore is not None:
        archivio = archivio or Archivio(documento.owner)
        voto = archivio.vota(archivio.somiglianze(vettore), escludi_pk=documento.pk)
    ia = _chiedi_al_modello(documento, v) if usa_ia else None
    per_nome = {c.name.lower(): c for c in Category.objects.all()}
    decisione = combina(voto, ia, per_nome)
    if vettore is None and not (decisione.categoria or decisione.nuova) and _e_un_immagine(v):
        decisione = _proposta_immagine(per_nome)

    if not salva:
        return decisione, voto, ia

    riassunto = ia.riassunto if ia else ""
    parole = ia.parole_chiave if ia else []
    stato_finale = S.PENDING if (decisione.categoria or decisione.nuova) else S.NONE
    sug = sug or ClassificationSuggestion(document=documento)
    sug.status = stato_finale
    sug.category = decisione.categoria
    sug.proposed_category = decisione.categoria
    sug.new_category_name = decisione.nuova
    sug.confidence = decisione.fiducia
    sug.source = decisione.fonte
    sug.reason = decisione.motivo or ("Nessun testo da cui classificare." if vettore is None else "Nessuna proposta abbastanza sicura.")
    sug.summary = riassunto
    sug.keywords = parole
    sug.neighbors = voto.vicini if voto else []
    sug.decided_at = None
    sug.decided_by = None
    sug.save()

    automatica = settings.CLASSIFICAZIONE_AUTO if applica is None else applica
    if automatica and decisione.categoria and decisione.fiducia >= settings.CLASSIFICAZIONE_SOGLIA_AUTO:
        applica_proposta(sug, user=None, automatica=True)
        sug.refresh_from_db()
    return sug


def _chiudi_senza_proposta(sug: ClassificationSuggestion, motivo: str) -> None:
    sug.status = S.NONE
    sug.reason = motivo
    sug.save(update_fields=["status", "reason", "updated_at"])


# --------------------------------------------------------------------------
# Applicare, rifiutare, annullare
# --------------------------------------------------------------------------

def testo_descrizione(sug: ClassificationSuggestion) -> str:
    parti = [sug.summary.strip()] if sug.summary.strip() else []
    if sug.keywords:
        parti.append("Parole chiave: " + ", ".join(sug.keywords))
    return "\n\n".join(parti)


def applica_proposta(sug: ClassificationSuggestion, *, user, automatica: bool = False, categoria: Category | None = None) -> Document:
    """Assegna la categoria (quella proposta, o `categoria` se la persona ne ha scelta un'altra) e, se la descrizione è
    vuota, scrive riassunto e parole chiave. Non cambia `updated_at` (non è una modifica del contenuto)."""
    with transaction.atomic():
        sug = ClassificationSuggestion.objects.select_for_update(of=("self",)).select_related("category").get(pk=sug.pk)
        documento = Document.objects.select_for_update(of=("self",)).get(pk=sug.document_id)
        scelta = categoria or sug.category
        if scelta is None and sug.new_category_name and not automatica:
            nome = " ".join(sug.new_category_name.split())[:80]
            scelta = Category.objects.filter(name__iexact=nome).first() or Category.objects.create(name=nome)
        if scelta is None:
            raise ValueError("Nessuna categoria da assegnare.")
        modifiche = {"category": scelta}
        scritta = ""
        if not documento.description.strip() and (scritta := testo_descrizione(sug)):
            modifiche["description"] = scritta
        Document.objects.filter(pk=documento.pk).update(**modifiche)
        sug.status = S.AUTO if automatica else S.ACCEPTED
        sug.category = scelta
        sug.new_category_name = ""
        sug.decided_at = timezone.now()
        sug.decided_by = user
        sug.save()
        proposta = sug.proposed_category.name if sug.proposed_category_id and sug.proposed_category_id != scelta.pk else ""
        audit.record(
            "document.classified", user=user, obj=documento, categoria=scelta.name, fonte=sug.source,
            fiducia=sug.fiducia_percento, automatica=automatica, riassunto=bool(scritta), **({"proposta": proposta} if proposta else {}),
        )
    return documento


def rifiuta_proposta(sug: ClassificationSuggestion, *, user) -> None:
    sug.status = S.REJECTED
    sug.decided_at = timezone.now()
    sug.decided_by = user
    sug.save(update_fields=["status", "decided_at", "decided_by", "updated_at"])
    audit.record("document.classification_rejected", user=user, obj=sug.document)


def annulla_applicazione(sug: ClassificationSuggestion, *, user) -> bool:
    """Toglie la categoria assegnata (e la descrizione scritta da noi), ma solo se nel frattempo nessuno l'ha cambiata."""
    with transaction.atomic():
        documento = Document.objects.select_for_update(of=("self",)).get(pk=sug.document_id)
        if documento.category_id != sug.category_id:
            return False  # qualcuno l'ha già cambiata a mano: non si tocca
        modifiche = {"category": None}
        if documento.description.strip() and documento.description.strip() == testo_descrizione(sug).strip():
            modifiche["description"] = ""
        Document.objects.filter(pk=documento.pk).update(**modifiche)
        sug.status = S.UNDONE
        sug.decided_at = timezone.now()
        sug.decided_by = user
        sug.save(update_fields=["status", "decided_at", "decided_by", "updated_at"])
        audit.record("document.classification_undone", user=user, obj=documento)
    return True


def esito_delle_decisioni(owner) -> dict:
    """Come sono andate le proposte che una persona ha esaminato: accettate com'erano o corrette?

    Dice la verità sull'uso reale (a differenza di `valuta`, che prova su documenti già ordinati). Considera le proposte
    con una categoria proposta e una decisione presa da una persona (le applicazioni automatiche non contano:
    nessuno le ha esaminate, a meno che le abbia annullate)."""
    righe = ClassificationSuggestion.objects.filter(
        document__owner=owner, proposed_category__isnull=False, status__in=[S.ACCEPTED, S.REJECTED]
    ).values_list("status", "source", "confidence", "category_id", "proposed_category_id")
    voci = []
    for stato, fonte, fiducia, scelta, proposta in righe:
        voci.append({"fonte": fonte or "—", "fiducia": fiducia, "giusta": stato == S.ACCEPTED and scelta == proposta})
    per_fonte: dict[str, list[bool]] = {}
    for v in voci:
        per_fonte.setdefault(v["fonte"], []).append(v["giusta"])
    fasce = [(0.6, 1.01), (0.4, 0.6), (0.0, 0.4)]
    per_fascia = []
    for a, b in fasce:
        ok = [v["giusta"] for v in voci if a <= v["fiducia"] < b]
        per_fascia.append({"da": a, "a": min(b, 1.0), "n": len(ok), "giuste": sum(ok)})
    return {
        "n": len(voci),
        "giuste": sum(v["giusta"] for v in voci),
        "per_fonte": {k: (sum(x), len(x)) for k, x in per_fonte.items()},
        "per_fascia": per_fascia,
    }


def da_smistare(user):
    """Proposte da rivedere per i documenti dell'utente (la coda «Da smistare»)."""
    return ClassificationSuggestion.objects.filter(status=S.PENDING, document__owner=user).select_related("document", "category")


# --------------------------------------------------------------------------
# Valutazione sui propri dati
# --------------------------------------------------------------------------

def valuta(owner, *, quasi_uguali: float = 0.95) -> dict:
    """Quanto è affidabile la classificazione sui TUOI documenti? Ripropone la categoria di ogni documento già
    categorizzato usando solo gli altri («leave-one-out») e confronta con quella vera.

    Le copie quasi identiche (ODT e PDF dello stesso testo, similarità ≥ `quasi_uguali`) sono escluse dai vicini:
    senza questo la prova sarebbe troppo facile, perché ogni documento troverebbe il proprio gemello.
    """
    a = Archivio(owner)
    n = len(a)
    if n < 2:
        return {"n": n, "bins": [], "precisione": None, "copertura": None}
    sim = a.matrice @ a.matrice.T
    esiti = []
    for i in range(n):
        s = sim[i].copy()
        s[i] = -1.0
        s[s >= quasi_uguali] = -1.0
        voto = a.vota(s)
        if voto is None:
            esiti.append((None, False))
            continue
        esiti.append((voto.fiducia, voto.categoria_id == a.cat_ids[i]))
    risposti = [(f, ok) for f, ok in esiti if f is not None]
    soglie = [0.9, 0.8, 0.7, 0.6, 0.5, 0.0]
    bins = []
    for s in soglie:
        scelti = [ok for f, ok in risposti if f >= s]
        bins.append({
            "soglia": s, "n": len(scelti),
            "precisione": (sum(scelti) / len(scelti)) if scelti else None,
            "copertura": len(scelti) / n,
        })
    return {
        "n": n,
        "risposti": len(risposti),
        "precisione": sum(ok for _, ok in risposti) / len(risposti) if risposti else None,
        "copertura": len(risposti) / n,
        "bins": bins,
    }
