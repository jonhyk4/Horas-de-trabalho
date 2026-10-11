"""Análise de manuais e QRH em PDF: datas de revisão, secções e bloqueios antes de gerar."""
from __future__ import annotations

import re
import sys
from collections import Counter

from pypdf import PdfReader

from .common import DATE_RE, MANUALS_WITHOUT_ROA_DEFAULT, PrepError, english_date, parse_date, sha256
from .history import history_warnings, proposed_revision

CODE_RE = re.compile(r"\b([A-Z0-9][A-Z0-9_-]*)\s+P\s+(\d+)\s*/\s*(\d+)\b")
MANUAL_NAMES = ["FCTM", "FCOM", "CCAP", "FCAP", "AFM", "CDL", "MEL", "WBM", "QRH"]
FOOTER_HEIGHT = 60
HEADER_HEIGHT = 70


def _effective_y(cm, tm) -> float:
    return tm[4] * cm[1] + tm[5] * cm[3] + cm[5]


def detect_fleet(text: str) -> str | None:
    if re.search(r"\bA330\b", text):
        return "A330"
    if re.search(r"\bA3(?:18|19|20|21)\b", text):
        return "A320F"
    return None


def analyze_pdf(path, profile: dict, progress=lambda *_: None, qrh: bool = False,
                ignore_date_pages=()) -> dict:
    """Lê o PDF e devolve a análise completa. Não altera ficheiros."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 15000))
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise PrepError("O PDF está protegido. Fornece uma cópia autorizada sem proteção.")
    if len(reader.pages) < 3:
        raise PrepError("O PDF não contém páginas suficientes para ser um manual completo.")

    pages, warnings, blockers = [], [], []
    date_counts: Counter = Counter()
    first_text, previous_issue = "", None
    date_location = "cabeçalho" if qrh else "rodapé"

    for index, page in enumerate(reader.pages):
        if index % 10 == 0:
            progress(f"A analisar página {index + 1} de {len(reader.pages)}…",
                     int(5 + 65 * index / len(reader.pages)))
        bottom = float(page.cropbox.bottom)
        top = float(page.cropbox.top) - bottom
        footer, header = [], []

        def visitor(text, cm, tm, font, size, footer=footer, header=header, top=top, bottom=bottom):
            y = _effective_y(cm, tm) - bottom
            if 0 <= y <= FOOTER_HEIGHT and text.strip():
                footer.append(text)
            if top - HEADER_HEIGHT <= y <= top and text.strip():
                header.append(text)

        text = page.extract_text(visitor_text=visitor) or ""
        foot, head = "\n".join(footer), "\n".join(header)
        date_text = head if qrh else foot
        if index == 0:
            first_text = text
        previous = re.search(r"last issue dated\s+(" + DATE_RE.pattern + r")", text, re.I)
        if previous and previous_issue is None:
            previous_issue = parse_date(previous.group(1)).isoformat()

        codes = CODE_RE.findall(foot.upper())
        code, local, total = codes[0] if len(codes) == 1 else ("", "", "")
        unique_dates = sorted({parse_date(m.group(0)).isoformat() for m in DATE_RE.finditer(date_text)})
        date = unique_dates[0] if len(unique_dates) == 1 else None
        if (index >= 2 or qrh) and index + 1 not in ignore_date_pages:
            date_counts.update(unique_dates)
        if len(unique_dates) > 1:
            warnings.append(f"Página {index + 1}: foram encontradas várias datas no {date_location}. "
                            "É necessário verificar este modelo de manual.")
        blank = "intentionally left blank" in text.lower() and len(text.strip()) < 350
        if qrh:
            if code.endswith("-FI"):
                code = "FI"
            elif code.endswith("-TRL"):
                code = "TRL"
            if not code:
                heading = " ".join(head.upper().splitlines())
                if re.search(r"\bFI(?:LL|L)ING INSTRUCTIONS\b", heading):
                    code = "FI"
                elif "TRANSMITTAL LETTER" in heading:
                    code = "TRL"
                elif "SUMMARY OF HIGHLIGHTS" in heading:
                    code = "SOH"
        pages.append({
            "source_page": index + 1, "code": code,
            "local_page": int(local) if local else None,
            "section_pages": int(total) if total else None,
            "date": date, "ambiguous_date": len(unique_dates) > 1, "blank": blank,
            "text_length": len(text.strip()), "footer": foot[:600], "header": head[:600],
            "date_location": date_location,
            "width": float(page.mediabox.width), "height": float(page.mediabox.height),
        })

    # Um verso em branco pertence à secção anterior só se a sequência o provar.
    for index in range(1, len(pages)):
        current, previous_page = pages[index], pages[index - 1]
        if (not current["code"] and current["blank"] and previous_page["code"]
                and previous_page["local_page"] is not None and previous_page["section_pages"] is not None
                and previous_page["local_page"] < previous_page["section_pages"]):
            current.update(code=previous_page["code"], local_page=previous_page["local_page"] + 1,
                           section_pages=previous_page["section_pages"], inferred_blank=True)

    anchors = [p["source_page"] for p in pages if p["code"] == "PLP-LESS" and p["local_page"] == 1]
    roa_blockers = []
    if not qrh and len(anchors) != 1:
        roa_blockers.append(f"Foram encontrados {len(anchors)} inícios de PLP-LESS. "
                            "Continua sem ROA ou verifica o manual para o inserir.")
    soh = [p["source_page"] for p in pages if p["code"].endswith("-SOH") or p["code"] == "SOH"]
    trl = [p["source_page"] for p in pages if p["code"] == "TRL"]
    fi = [p["source_page"] for p in pages if p["code"] == "FI"]

    issue = max(date_counts) if date_counts else None
    if not issue:
        blockers.append(f"Não foi encontrada uma data no {date_location} do manual. "
                        "Não é possível calcular a revisão.")
    dated_pages = pages if qrh else pages[2:]
    if any(p["ambiguous_date"] for p in dated_pages if p["source_page"] not in ignore_date_pages):
        blockers.append(f"Existem datas ambíguas no {date_location}. Este PDF precisa de ser verificado antes da geração.")

    detected_fleet = detect_fleet(first_text)
    detected_manual = next((m for m in MANUAL_NAMES if re.search(r"\b" + m + r"\b", first_text)), None)
    if detected_fleet and detected_fleet != profile["fleet"]:
        raise PrepError("A frota identificada no PDF não coincide com o perfil selecionado.")
    if detected_manual and detected_manual != profile["manual"]:
        raise PrepError("O manual identificado no PDF não coincide com o perfil selecionado.")
    if not detected_fleet or not detected_manual:
        warnings.append("Identificação automática incompleta. Confirma a frota e o manual antes de gerar.")
    if not profile.get("validated_sample"):
        warnings.append("Este perfil ainda não foi validado com um exemplo desta combinação. "
                        "Confirma a análise e as páginas do primeiro resultado.")
    warnings.extend(history_warnings(profile.get("history", [])))

    try:
        revision, exists = proposed_revision(profile.get("history", []), issue) if issue else ("", False)
    except PrepError as error:
        revision, exists = "", False
        roa_blockers.append(str(error))
    if not profile.get("history") and profile["manual"] not in MANUALS_WITHOUT_ROA_DEFAULT:
        warnings.append("Não existe histórico ROA para este manual. Podes continuar sem ROA.")
    if exists:
        roa_blockers.append(f"A data {english_date(issue)} já existe no ROA. A última revisão + 1 seria {revision}. "
                            "Confirma se importaste o último ROA concluído, sem a nova revisão ainda em preparação.")
    dated = [parse_date(r["date"]) for r in profile.get("history", []) if r.get("date")]
    if issue and dated and parse_date(issue) < max(dated):
        roa_blockers.append("A data mais recente do PDF é anterior ao histórico ROA guardado.")
    if not trl:
        warnings.append("Não foi detetada uma secção TRL (Transmittal Letter).")
    if not fi:
        warnings.append("Não foi detetada uma secção FI (Filing Instructions).")

    anchor_page = 1 if qrh or len(anchors) != 1 else anchors[0]
    roa_available = bool(profile.get("history")) and (qrh or len(anchors) == 1)
    return {
        "kind": "qrh" if qrh else "manual",
        "total_pages": len(pages), "pages": pages,
        "issue_date": issue, "revision": revision, "revision_exists": exists,
        "previous_issue_date": previous_issue,
        "anchor_page": anchor_page, "roa_blockers": roa_blockers,
        "roa_available": roa_available,
        "include_roa_default": roa_available and profile["manual"] not in MANUALS_WITHOUT_ROA_DEFAULT,
        "soh_pages": soh, "trl_pages": trl, "fi_pages": fi,
        "original_cover_pages": [] if qrh else [1, 2],
        "date_location": date_location,
        "date_counts": dict(sorted(date_counts.items(), reverse=True)),
        "warnings": warnings, "blockers": blockers,
        "detected_fleet": detected_fleet, "detected_manual": detected_manual,
        "sha256": sha256(path),
    }
