"""Geração de uma edição: documentos Word, PDF finais, relatório e arquivo verificado.

Regra central: o histórico ROA só avança quando todos os ficheiros foram gerados,
convertidos, compostos e arquivados. Uma falha deixa os documentos recuperáveis
numa pasta __FALHA, sem acrescentar revisão.
"""
from __future__ import annotations

import csv
import html
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from .common import MANUAL_TITLES, PrepError, VERSION, atomic_json, now_tag, parse_date, sha256
from .converters import convert_to_pdf
from .history import proposed_revision, update_history
from .office import fill_letter, fill_roa, fill_transmittal, normalize_highlights, transmittal_ready
from .paths import models_dir
from .pdf_compose import compose_pdf, page_plan
from .store import selected_forms

DOCUMENT_SUFFIXES = (".pdf", ".docx", ".csv", ".json", ".html")


def _move_results(source: Path, destination: Path) -> None:
    """Move a pasta temporária para o destino; se o sistema bloquear, copia com verificação."""
    if destination.exists():
        raise PrepError("A pasta de destino já existe; os resultados existentes foram preservados.")
    for attempt in range(4):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt < 3:
                time.sleep(.2 * (2 ** attempt))
    # Antivírus ou indexação podem bloquear a pasta: copia byte a byte e verifica.
    destination.mkdir()
    marker = destination / ".em_curso"
    try:
        marker.write_text("a copiar", encoding="ascii")
        for original in sorted(p for p in source.rglob("*") if p.is_file()):
            target = destination / original.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if sha256(original) != sha256(target):
                raise PrepError("A cópia dos resultados falhou na verificação de integridade: " + original.name)
        marker.unlink()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    shutil.rmtree(source, ignore_errors=True)


def _archive(folder: Path, name: str) -> Path:
    members = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES)
    if not members:
        raise PrepError("Não existem documentos para arquivar.")
    archive = folder / f"{name}__Documentos.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in members:
            zipped.write(path, path.name)
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None or len(check.infolist()) != len(members):
            archive.unlink(missing_ok=True)
            raise PrepError("O arquivo ZIP não passou a verificação de integridade.")
    return archive


def _write_summary(folder: Path, report: dict) -> None:
    for kind, data in report["outputs"].items():
        with (folder / f"Mapa_paginas_{kind}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["output_page", "origin", "source_page", "code"], delimiter=";")
            writer.writeheader()
            writer.writerows(data["mapping"])
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(k)}</td><td>{v['pages']}</td><td>{v['bookmarks']}</td>"
                   f"<td>{v['roa_start'] or '—'}</td></tr>" for k, v in report["outputs"].items())
    warnings = "".join("<li>" + esc(w) + "</li>" for w in report["warnings"])
    forms = "; ".join(f["code"] + " — " + f["title"] for f in report["forms"]) or "Nenhum"
    content = (
        '<!doctype html><html lang="pt-PT"><meta charset="utf-8"><title>Resumo da edição</title>'
        '<style>body{font:16px system-ui;max-width:900px;margin:48px auto;padding:0 24px;color:#233238}'
        'table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}</style>'
        f'<h1>{esc(report["base_name"])} · Revisão {esc(str(report["revision"]))}</h1>'
        f'<p>Data da edição: {esc(str(report["issue_date"]))} · Exportação: {esc(report["converter"])}</p>'
        '<table><tr><th>Ficheiro</th><th>Páginas</th><th>Bookmarks</th><th>Início ROA</th></tr>' + rows + '</table>'
        f'<p>Formulários referidos na carta: {esc(forms)}.</p><ul>{warnings}</ul>'
        '<p>Os documentos devem ser revistos antes da utilização operacional. Nenhuma aprovação ou envio foi realizado.</p>'
        f'<p>SHA-256 do original:<br><code>{esc(report["source_sha256"])}</code></p></html>'
    )
    (folder / "Resumo.html").write_text(content, encoding="utf-8")


def generate_edition(store, key: str, source_path, analysis: dict, options: dict, progress=lambda *_: None) -> dict:
    profile = store.get(key)
    original_profile = dict(profile)
    if sha256(source_path) != analysis["sha256"]:
        raise PrepError("O PDF de origem mudou. Volta a analisá-lo.")
    cover = store.cover_path(profile)
    if analysis.get("blockers"):
        raise PrepError(" ".join(analysis["blockers"]))
    issue = parse_date(analysis["issue_date"]).isoformat()
    include_roa = bool(options.get("include_roa", analysis.get("include_roa_default", bool(profile.get("history")))))
    if include_roa and analysis.get("roa_blockers"):
        raise PrepError(" ".join(analysis["roa_blockers"]))
    if include_roa and not analysis.get("roa_available"):
        raise PrepError("Não há histórico ou posição de inserção válidos para incluir o ROA. Continua sem ROA.")

    try:
        revision, exists = proposed_revision(profile.get("history", []), issue)
    except PrepError:
        if include_roa:
            raise
        revision, exists = "", False
    if include_roa and exists:
        raise PrepError("A data já existe no ROA. Importa o último histórico concluído antes desta revisão "
                        "ou continua sem ROA.")
    if not include_roa and exists:
        revision = next(str(row["revision"]) for row in profile["history"]
                        if parse_date(row["date"]).isoformat() == issue)
    if include_roa and not revision:
        raise PrepError("Falta o histórico ROA para calcular a revisão seguinte.")

    print_requested = bool(options.get("print_version", False))
    printing = print_requested and bool(analysis["fi_pages"])
    forms = selected_forms(store.forms(), options.get("form_codes"))
    engine = options.get("converter", "auto")
    page_size = None
    if analysis.get("kind") == "qrh":
        sizes = Counter((round(p["width"], 2), round(p["height"], 2))
                        for p in analysis["pages"] if p["source_page"] not in analysis.get("cover_pages", []))
        page_size = sizes.most_common(1)[0][0]

    highlights = normalize_highlights(options.get("highlights", []), allow_empty=True)
    make_transmittal = bool(highlights)
    previous_issue = analysis.get("previous_issue_date") or ""
    prior = [parse_date(r["date"]) for r in profile.get("history", [])
             if r.get("date") and parse_date(r["date"]).isoformat() < issue]
    if not previous_issue and prior:
        previous_issue = max(prior).isoformat()
    if make_transmittal and not transmittal_ready():
        raise PrepError("O modelo da Transmittal Letter não foi encontrado na pasta models.")

    history, added = (update_history(profile.get("history", []), revision, issue)
                      if include_roa else (profile.get("history", []), False))
    plan = page_plan(analysis, issue, True, printing)
    name = f"{profile['fleet']}__{profile['manual']}__{parse_date(issue).strftime('%y%m%d')}"
    tap_name = "TAP__" + name + ".pdf"
    run = now_tag()
    folder = store.base / "saidas" / (name + "__" + run)
    temp = store.base / "temporarios" / ("processamento_" + run)
    temp.mkdir(parents=True)
    try:
        progress("A gerar ROA e carta Word…", 10)
        roa_docx = temp / (name + "__ROA.docx")
        letter_docx = temp / (name + "__Carta_ANAC.docx")
        if include_roa:
            fill_roa(profile, history, issue, roa_docx, blank_page=False)
        fill_letter(profile, revision, issue, True, letter_docx, forms=forms, attachment_name=tap_name)
        progress("A exportar os documentos para PDF…", 25)
        transmittal_docx = temp / (name + "__Transmittal_Letter.docx")
        docx_paths = ([roa_docx] if include_roa else []) + [letter_docx]
        if make_transmittal:
            fill_transmittal(profile, revision, issue, highlights, transmittal_docx,
                             page_size=page_size, previous_date=previous_issue)
            docx_paths.append(transmittal_docx)
        converter = convert_to_pdf(docx_paths, engine, lambda message: progress(message, 25))

        roa_pdf = roa_docx.with_suffix(".pdf") if include_roa else None
        roa_pages = len(PdfReader(str(roa_pdf)).pages) if include_roa else 0
        if include_roa and roa_pages % 2:
            # Número ímpar de páginas: acrescenta verso em branco e volta a converter.
            fill_roa(profile, history, issue, roa_docx, blank_page=True)
            convert_to_pdf([roa_docx], engine, lambda message: progress(message, 35))
            roa_pages = len(PdfReader(str(roa_pdf)).pages)
            if roa_pages % 2:
                raise PrepError("Não foi possível paginar o ROA com verso em branco. Verifica o documento Word.")
        transmittal_pages = 0
        if make_transmittal:
            transmittal_pdf = transmittal_docx.with_suffix(".pdf")
            transmittal_pages = len(PdfReader(str(transmittal_pdf)).pages)
            if transmittal_pages % 2:
                fill_transmittal(profile, revision, issue, highlights, transmittal_docx, blank_page=True,
                                 page_size=page_size, previous_date=previous_issue)
                convert_to_pdf([transmittal_docx], engine, lambda message: progress(message, 40))
                transmittal_pages = len(PdfReader(str(transmittal_pdf)).pages)
                if transmittal_pages % 2:
                    raise PrepError("Não foi possível paginar a Transmittal Letter com verso em branco.")

        preserve = analysis.get("kind") == "qrh"
        progress("A compor o manual completo e atualizar os bookmarks…", 48)
        outputs = {"completo": compose_pdf(source_path, temp / (name + ".pdf"), plan["full"],
                                           analysis["anchor_page"], cover_path=cover,
                                           roa_path=roa_pdf, preserve_original_covers=preserve)}
        if plan["incremental"]:
            progress("A compor o incremental…", 70)
            outputs["incremental"] = compose_pdf(
                source_path, temp / tap_name, plan["incremental"], analysis["anchor_page"],
                cover_path=cover, transmittal_path=transmittal_pdf if make_transmittal else None,
                preserve_original_covers=preserve)
        if printing:
            progress("A compor o ficheiro de impressão…", 82)
            outputs["print"] = compose_pdf(source_path, temp / (name + "__PRINT.pdf"), plan["print"],
                                           analysis["anchor_page"])
        if analysis["fi_pages"]:
            outputs["filing_instructions"] = compose_pdf(
                source_path, temp / (name + "__Filing_Instructions.pdf"), analysis["fi_pages"],
                analysis["anchor_page"])
        for data in outputs.values():
            for row in data["mapping"]:
                if row["origin"] == "PDF original":
                    row["code"] = analysis["pages"][row["source_page"] - 1]["code"]

        warnings = list(analysis["warnings"])
        if not include_roa:
            warnings.append("Edição gerada sem ROA; o histórico ROA não foi alterado.")
        if not make_transmittal:
            warnings.append("Transmittal Letter não gerada por ausência de highlights.")
        if print_requested and not printing:
            warnings.append("O __PRINT não foi gerado por não terem sido identificadas Filing Instructions.")
        undated = [p["source_page"] for p in analysis["pages"][2:] if not p["date"] and p["code"] not in ("TRL", "FI")]
        if undated:
            warnings.append("Páginas sem data de revisão omitidas do incremental: " + ", ".join(map(str, undated)) + ".")
        warnings.append("Referência e datas do ofício por preencher. O formulário ANAC é anexado fora desta ferramenta.")

        report = {
            "app_version": VERSION, "base_name": name, "revision": revision, "issue_date": issue,
            "created_at": run, "kind": analysis.get("kind", "manual"),
            "forms": forms, "highlights": highlights, "include_roa": include_roa,
            "history_added": added, "history": history,
            "profile": {k: v for k, v in profile.items() if k != "history"},
            "source_sha256": analysis["sha256"], "zip_sha256": analysis.get("zip_sha256"),
            "qrh_inventory": analysis.get("qrh_inventory", []),
            "cover_sha256": sha256(cover), "converter": converter,
            "previous_issue_date": previous_issue if make_transmittal else None,
            "transmittal_pages": transmittal_pages, "roa_pages": roa_pages,
            "source_pages": analysis["total_pages"], "soh_removed": analysis["soh_pages"],
            "full_front_matter_removed": analysis["trl_pages"] + analysis["fi_pages"],
            "outputs": outputs, "warnings": warnings,
            "templates_folder": str(models_dir()),
        }
        atomic_json(temp / "Edicao.json", report)
        _write_summary(temp, report)
        _archive(temp, name)
        progress("A confirmar o histórico e a guardar a edição…", 95)
        with store.lock:
            if store.get(key) != original_profile:
                raise PrepError("O histórico mudou durante o processamento. Repete a operação.")
            _move_results(temp, folder)
            if include_roa:
                updated = dict(original_profile, history=history)
                store.save_profile(updated)
        progress("Documentos preparados.", 100)
        return {"folder_name": folder.name, "base_name": name, "warnings": warnings,
                "outputs": {k: {f: v for f, v in d.items() if f != "mapping"} for k, d in outputs.items()},
                "files": sorted(p.name for p in folder.iterdir() if p.is_file())}
    except Exception as error:
        # Preserva os documentos editáveis e os highlights para recuperação.
        if temp.exists():
            recovery = folder.with_name(folder.name + "__FALHA")
            atomic_json(temp / "Falha.json", {"base_name": name, "revision": revision, "issue_date": issue,
                                              "created_at": run, "profile": {k: v for k, v in profile.items() if k != "history"},
                                              "highlights": highlights, "forms": forms, "error": str(error)})
            try:
                _move_results(temp, recovery)
            except Exception as recovery_error:
                raise PrepError(f"{error}\nOs documentos permanecem em: {temp}\n"
                                f"Não foi possível mover a pasta de recuperação: {recovery_error}") from error
            raise PrepError(f"{error}\nDocumentos e highlights recuperáveis em: {recovery}",
                            recovery={"folder_name": recovery.name,
                                      "files": sorted(p.name for p in recovery.iterdir() if p.is_file())}) from error
        raise
