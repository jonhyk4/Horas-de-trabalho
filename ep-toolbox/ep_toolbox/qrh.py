"""Compilação de QRH a partir de ZIP com subpastas, preservando os bookmarks de cada PDF."""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

from pypdf import PdfReader, PdfWriter

from .common import PrepError, atomic_json, sha256
from .pdf_analysis import detect_fleet

MAX_TOTAL = 4 * 1024 ** 3
MAX_PDF = 512 * 1024 ** 2
MAX_MEMBERS = 10000
MAX_QRH = 500
IGNORED_SUFFIX = re.compile(r"(?:GSOH|LEDU)$", re.I)


def _natural_key(value: str):
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", value)]


def _registration(name: str, first_text: str) -> str | None:
    match = re.search(r"(?<![A-Z0-9])CS[-_ ]([A-Z0-9]{3,5})(?![A-Z0-9])",
                      name.upper() + " " + first_text.upper())
    return "CS-" + match.group(1) if match else None


def compile_zip(path, folder, progress=lambda *_: None) -> tuple[Path, dict]:
    """Extrai os PDFs completos do ZIP e compila-os num único PDF com bookmarks."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 15000))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "Fonte_QRH_compilada.pdf"
    inventory, ignored, readers, cover_pages = [], [], [], []
    total = 0
    writer = PdfWriter()
    try:
        with zipfile.ZipFile(path) as archive:
            members = sorted((i for i in archive.infolist() if not i.is_dir()),
                             key=lambda i: _natural_key(i.filename))
            if len(members) > MAX_MEMBERS:
                raise PrepError(f"O ZIP excede {MAX_MEMBERS} ficheiros.")
            for info in members:
                name = info.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise PrepError("O ZIP contém um caminho inválido: " + info.filename)
                if info.flag_bits & 1:
                    raise PrepError("O ZIP está protegido por palavra-passe.")
                basename = parts[-1]
                stem = Path(basename).stem
                if any(p.startswith(".") or p == "__MACOSX" for p in parts):
                    continue
                if IGNORED_SUFFIX.search(stem) or IGNORED_SUFFIX.search(basename):
                    ignored.append(name)
                    continue
                with archive.open(info) as stream:
                    signature = stream.read(5)
                    if signature != b"%PDF-":
                        if basename.lower().endswith(".pdf"):
                            raise PrepError("O ficheiro não é um PDF válido: " + name)
                        continue
                    total += info.file_size
                    if info.file_size > MAX_PDF or total > MAX_TOTAL:
                        raise PrepError("O ZIP excede os limites: 512 MB por PDF ou 4 GB de PDFs no total.")
                    if len(inventory) >= MAX_QRH:
                        raise PrepError(f"O ZIP excede {MAX_QRH} QRHs completos.")
                    local = folder / f"original_{len(inventory) + 1:04}.pdf"
                    size = len(signature)
                    with local.open("wb") as target:
                        target.write(signature)
                        while True:
                            chunk = stream.read(1024 ** 2)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > MAX_PDF:
                                raise PrepError("PDF descomprimido demasiado grande: " + name)
                            target.write(chunk)
                reader = PdfReader(str(local), strict=False)
                readers.append(reader)
                if reader.is_encrypted:
                    raise PrepError("QRH protegido: " + name)
                if not reader.pages:
                    raise PrepError("QRH sem páginas: " + name)
                offset = len(writer.pages)
                first_text = reader.pages[0].extract_text() or ""
                registration = _registration(name, first_text)
                label = (registration + " — " + stem) if registration and registration not in stem.upper() else stem
                progress(f"A compilar QRH {len(inventory) + 1}: {basename}", min(30, 5 + len(inventory)))
                writer.append(reader, outline_item=label, import_outline=True)
                inventory.append({
                    "file": name, "pages": len(reader.pages), "start_page": offset + 1,
                    "end_page": offset + len(reader.pages), "sha256": sha256(local),
                    "detected_fleet": detect_fleet(first_text), "registration": registration,
                })
                # Capas e páginas em branco identificáveis ficam fora da análise de datas.
                for index, page in enumerate(reader.pages[:2]):
                    text = (page.extract_text() or "").strip()
                    is_cover = (index == 0 and re.search(r"\bQRH\b|QUICK REFERENCE HANDBOOK", text, re.I)
                                and not re.search(r"\bP\s+\d+\s*/", text))
                    is_blank = (index == 1 and re.search(r"intentionally left blank", text, re.I) and len(text) < 350)
                    if is_cover or is_blank:
                        cover_pages.append(offset + index + 1)
        if not inventory:
            raise PrepError("Não foram encontrados PDFs completos de QRH. GSOH e LEDU são ignorados.")
        writer.page_mode = "/UseOutlines"
        with output.open("wb") as stream:
            writer.write(stream)
        check = PdfReader(str(output))
        if len(check.pages) != sum(i["pages"] for i in inventory):
            raise PrepError("A compilação QRH não passou a verificação de páginas.")
        manifest = {"inventory": inventory, "ignored": ignored, "cover_pages": cover_pages,
                    "zip_sha256": sha256(path)}
        atomic_json(folder / "Inventario_QRH.json", manifest)
        return output, manifest
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise PrepError("O ZIP não é válido ou está incompleto.") from error
    finally:
        writer.close()
        for reader in readers:
            reader.close()


def qrh_fleets(manifest: dict) -> set[str]:
    return {row["detected_fleet"] for row in manifest["inventory"] if row["detected_fleet"]}
