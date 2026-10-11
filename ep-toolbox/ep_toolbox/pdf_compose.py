"""Composição dos PDF finais: manual completo, ficheiro TAP, PRINT e Filing Instructions."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import Fit

from .common import PrepError, VERSION, parse_date


def page_plan(analysis: dict, issue_date: str, incremental: bool, print_version: bool = False) -> dict:
    """Decide que páginas de origem entram em cada ficheiro."""
    issue = parse_date(issue_date)
    if not issue:
        raise PrepError("Não foi possível determinar a data da edição nas páginas.")
    iso = issue.isoformat()
    covers = set(analysis.get("cover_pages", []))
    checked = analysis["pages"] if analysis.get("kind") == "qrh" else analysis["pages"][2:]
    if any(p["ambiguous_date"] for p in checked if p["source_page"] not in covers):
        raise PrepError("Há páginas com várias datas na zona de revisão. É necessário verificar o PDF antes de gerar.")

    exclude = set(analysis.get("original_cover_pages", [1, 2]) + analysis["soh_pages"])
    full = [p["source_page"] for p in analysis["pages"]
            if p["source_page"] not in exclude and p["code"] not in ("TRL", "FI")]
    changed = []
    if incremental or print_version:
        changed = [p["source_page"] for p in analysis["pages"]
                   if p["source_page"] in full and p["date"] == iso and p["source_page"] not in covers]
        if not changed:
            raise PrepError("Não há páginas de conteúdo com a data indicada na zona de revisão. "
                            "O incremental não foi gerado.")
    fi = [p["source_page"] for p in analysis["pages"] if p["source_page"] not in exclude and p["code"] == "FI"]
    if print_version and not fi:
        raise PrepError("Não foram encontradas Filing Instructions para compor o __PRINT.")
    return {
        "full": full,
        "incremental": changed if incremental else [],
        "print": fi + changed if print_version else [],
    }


def _outline_tree(outline) -> list[dict]:
    result = []
    for entry in outline:
        if isinstance(entry, list):
            if result:
                result[-1]["children"] = _outline_tree(entry)
        else:
            result.append({"dest": entry, "children": []})
    return result


def compose_pdf(source_path, output, pages, anchor, cover_path=None, roa_path=None,
                transmittal_path=None, preserve_original_covers=False) -> dict:
    """Monta um PDF a partir de páginas da origem, com capas, Transmittal Letter e ROA opcionais."""
    reader = PdfReader(str(source_path))
    cover = PdfReader(str(cover_path)) if cover_path else None
    roa = PdfReader(str(roa_path)) if roa_path else None
    transmittal = PdfReader(str(transmittal_path)) if transmittal_path else None
    if cover and len(cover.pages) != 4:
        raise PrepError("As capas TAP devem conter exatamente quatro páginas.")
    cover_count = len(cover.pages) if cover else 0
    transmittal_count = len(transmittal.pages) if transmittal else 0
    roa_count = len(roa.pages) if roa else 0
    prefix = cover_count + transmittal_count

    writer = PdfWriter()
    # Uma única importação mantém objetos partilhados e destinos nomeados.
    writer.append(reader, pages=[p - 1 for p in pages], import_outline=False)
    if cover:
        writer.merge(0, cover, import_outline=False)
    if transmittal:
        writer.merge(cover_count, transmittal, import_outline=False)
    roa_position = prefix + sum(p < anchor for p in pages) if roa else None
    if roa:
        writer.merge(roa_position, roa, import_outline=False)
    page_map = {p - 1: prefix + i + (roa_count if roa and p >= anchor else 0) for i, p in enumerate(pages)}

    def filter_tree(nodes):
        kept = []
        for node in nodes:
            dest = node["dest"]
            title = str(dest.get("/Title", ""))
            if "summary of highlights" in title.lower():
                continue
            children = filter_tree(node["children"])
            try:
                old = reader.get_destination_page_number(dest)
            except Exception:
                old = None
            target = page_map.get(old)
            if old in (0, 1) and cover and old not in page_map and not preserve_original_covers:
                target = 0
            if target is None and children:
                target = children[0]["target"]
            if target is None:
                continue
            kept.append({"title": "Capas TAP" if title.lower() == "front page" and old not in page_map else title,
                         "target": target, "old": old, "children": children, "dest": dest})
        return kept

    tree = filter_tree(_outline_tree(reader.outline))
    count = 0
    inserted_roa = False
    if cover and not any(node["title"] == "Capas TAP" and node["target"] == 0 for node in tree):
        writer.add_outline_item("Capas TAP", 0)
        count += 1
    if transmittal:
        writer.add_outline_item("Transmittal Letter — Highlights", cover_count)
        count += 1

    def write_tree(nodes, parent=None):
        nonlocal count, inserted_roa
        for node in nodes:
            if roa and node["old"] == anchor - 1 and not inserted_roa:
                writer.add_outline_item("Record of Amendments (ROA)", roa_position, parent=parent)
                count += 1
                inserted_roa = True
            dest = node["dest"]
            flags = int(dest.get("/F", 0))
            color = dest.get("/C")
            item = writer.add_outline_item(
                node["title"], node["target"], parent=parent,
                bold=bool(flags & 2), italic=bool(flags & 1),
                color=tuple(color) if color else None,
                is_open=int(dest.get("/Count", 0)) >= 0, fit=Fit.fit(),
            )
            count += 1
            write_tree(node["children"], item)

    write_tree(tree)
    if roa and not inserted_roa:
        writer.add_outline_item("Record of Amendments (ROA)", roa_position)
        count += 1
    writer.add_metadata({"/Title": Path(output).stem, "/Producer": f"EP Toolbox {VERSION}"})
    writer.page_mode = "/UseOutlines"
    with open(output, "wb") as stream:
        writer.write(stream)
    writer.close()

    check = PdfReader(str(output))
    expected = len(pages) + prefix + roa_count
    if len(check.pages) != expected:
        raise PrepError("O número de páginas produzido não coincide com o plano.")

    mapping = []
    for i in range(cover_count):
        mapping.append({"output_page": i + 1, "origin": "Capas TAP", "source_page": i + 1, "code": "CAPA"})
    for old, new in page_map.items():
        mapping.append({"output_page": new + 1, "origin": "PDF original", "source_page": old + 1, "code": ""})
    for i in range(transmittal_count):
        mapping.append({"output_page": cover_count + i + 1, "origin": "Transmittal Letter gerada",
                        "source_page": i + 1, "code": "TRL"})
    for i in range(roa_count):
        mapping.append({"output_page": roa_position + i + 1, "origin": "ROA gerado",
                        "source_page": i + 1, "code": "PLP-ROA"})
    return {
        "pages": expected, "bookmarks": count,
        "roa_start": roa_position + 1 if roa else None,
        "mapping": sorted(mapping, key=lambda row: row["output_page"]),
    }
