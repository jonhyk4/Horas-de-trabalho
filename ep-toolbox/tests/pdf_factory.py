"""Cria PDF de teste com texto posicionado, sem depender de ficheiros da empresa."""
from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path, pages: list[list[tuple[float, float, str]]], width=612, height=792) -> None:
    """pages: lista de páginas; cada página é uma lista de (x, y, texto)."""
    objects: list[bytes] = []
    page_ids = []
    # Objetos fixos: 1 catálogo, 2 árvore de páginas, 3 tipo de letra.
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # preenchido depois com a lista de páginas
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    next_id = 4
    for lines in pages:
        content = "BT /F1 10 Tf " + " ".join(
            f"1 0 0 1 {x} {y} Tm ({_escape(text)}) Tj" for x, y, text in lines) + " ET"
        data = content.encode("latin-1")
        content_id = next_id
        page_id = next_id + 1
        next_id += 2
        objects.append(f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>".encode())
        page_ids.append(page_id)
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as stream:
        stream.write(bytes(out))
