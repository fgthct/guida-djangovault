"""File di prova generati al volo (nessun binario nel repository)."""

import io
import zipfile

from PIL import Image, ImageDraw, ImageFont


def pdf_con_testo(*pagine: str) -> bytes:
    """PDF minimo valido: una pagina per ogni stringa, testo in Helvetica."""
    oggetti = ["<< /Type /Catalog /Pages 2 0 R >>"]
    ids_pagine = [4 + 2 * i for i in range(len(pagine))]
    oggetti.append(f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in ids_pagine)}] /Count {len(pagine)} >>")
    oggetti.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, testo in enumerate(pagine):
        flusso = f"BT /F1 18 Tf 72 720 Td ({testo}) Tj ET"
        oggetti.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        oggetti.append(f"<< /Length {len(flusso)} >>\nstream\n{flusso}\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offset = []
    for n, corpo in enumerate(oggetti, 1):
        offset.append(len(out))
        out += f"{n} 0 obj\n{corpo}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(oggetti) + 1}\n0000000000 65535 f \n".encode()
    for o in offset:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(oggetti) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def immagine_con_testo(testo: str = "FATTURA NUMERO 2026", formato: str = "PNG", dimensioni=(900, 200)) -> bytes:
    img = Image.new("RGB", dimensioni, "white")
    ImageDraw.Draw(img).text((20, 60), testo, fill="black", font=ImageFont.load_default(size=56))
    buf = io.BytesIO()
    img.save(buf, format=formato)
    return buf.getvalue()


def pdf_scansione(testo: str = "CONTRATTO DI LOCAZIONE") -> bytes:
    """PDF che contiene solo un'immagine (nessun testo): come una scansione."""
    img = Image.open(io.BytesIO(immagine_con_testo(testo)))
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def _zip(file: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, contenuto in file.items():
            z.writestr(nome, contenuto)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(*paragrafi: str) -> bytes:
    corpo = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragrafi)
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {W}><w:body>{corpo}</w:body></w:document>',
    })


def xlsx(*celle: str) -> bytes:
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    si = "".join(f"<si><t>{c}</t></si>" for c in celle)
    return _zip({
        "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst {S}>{si}</sst>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {S}><sheetData><row><c t="inlineStr"><is><t>testo in cella</t></is></c></row></sheetData></worksheet>',
    })


def pptx(*diapositive: str) -> bytes:
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    file = {
        f"ppt/slides/slide{i}.xml": f'<?xml version="1.0"?><p:sld {A} xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>'
        for i, t in enumerate(diapositive, 1)
    }
    return _zip(file)


def odt(*paragrafi: str) -> bytes:
    T = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    corpo = "".join(f"<text:p>{p}</text:p>" for p in paragrafi)
    return _zip({
        "content.xml": f'<?xml version="1.0"?><office:document-content {T}><office:body><office:text>{corpo}</office:text></office:body></office:document-content>',
    })


def bomba_xml() -> bytes:
    """docx con entità XML annidate («billion laughs»): defusedxml deve rifiutarlo."""
    dtd = '<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    return _zip({"word/document.xml": f'<?xml version="1.0"?>{dtd}<w:document {W}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>'})
