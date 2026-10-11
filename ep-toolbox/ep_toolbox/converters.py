"""Conversão DOCX → PDF. Usa Microsoft Word (Windows, se instalado) ou LibreOffice (Windows e macOS)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import PrepError

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
CONVERSION_TIMEOUT = 600


def find_libreoffice() -> str | None:
    candidates = [shutil.which("soffice"), shutil.which("libreoffice")]
    if sys.platform == "darwin":
        candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        candidates.append(str(Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice"))
    elif sys.platform == "win32":
        # Instalação por utilizador (não requer administrador) e instalação para todos.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(str(Path(local) / "Programs/LibreOffice/program/soffice.exe"))
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if os.environ.get(variable):
                candidates.append(str(Path(os.environ[variable]) / "LibreOffice/program/soffice.exe"))
    return next((p for p in candidates if p and Path(p).is_file()), None)


def word_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application\\CLSID"):
            return True
    except OSError:
        return False


def available_converters() -> list[dict]:
    result = [{"id": "auto", "label": "Automático"}]
    if word_available():
        result.append({"id": "word", "label": "Microsoft Word"})
    if find_libreoffice():
        result.append({"id": "libreoffice", "label": "LibreOffice"})
    return result


def _convert_libreoffice(paths, progress) -> str:
    soffice = find_libreoffice()
    if not soffice:
        raise PrepError("A exportação para PDF precisa do Microsoft Word (Windows) ou do LibreOffice. "
                        "Nenhum foi detetado. O LibreOffice pode ser instalado sem administrador.")
    with tempfile.TemporaryDirectory(prefix="ep_office_") as profile:
        for path in paths:
            progress("A exportar pelo LibreOffice: " + path.name)
            command = [soffice, "-env:UserInstallation=" + Path(profile).as_uri(), "--headless",
                       "--convert-to", "pdf", "--outdir", str(path.parent), str(path)]
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                                        timeout=CONVERSION_TIMEOUT, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired as error:
                raise PrepError("O LibreOffice excedeu o tempo de exportação. "
                                "Os documentos Word ficaram guardados para recuperação.") from error
            if result.returncode or not path.with_suffix(".pdf").is_file():
                raise PrepError("Falhou a exportação pelo LibreOffice: " + result.stdout + result.stderr)
    return "LibreOffice"


def _convert_word(paths, progress) -> str:
    """Exportação pelo Word (só Windows). Não testada neste ambiente; validar no computador de destino."""
    if not word_available():
        raise PrepError("O Microsoft Word não foi detetado neste computador.")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        word.AutomationSecurity = 3
        for path in paths:
            progress("A exportar pelo Word: " + path.name)
            document = None
            try:
                document = word.Documents.Open(str(path), ReadOnly=False, AddToRecentFiles=False)
                document.Fields.Update()
                document.Repaginate()
                document.Save()
                document.ExportAsFixedFormat(str(path.with_suffix(".pdf")), 17, False)
            finally:
                if document is not None:
                    document.Close(False)
    finally:
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()
    return "Microsoft Word"


def convert_to_pdf(paths, engine: str = "auto", progress=lambda *_: None) -> str:
    """Converte cada DOCX para PDF ao lado do ficheiro. Devolve o nome do conversor usado."""
    paths = [Path(p).resolve() for p in paths]
    if engine not in ("auto", "word", "libreoffice"):
        raise PrepError("Conversor de documentos inválido.")
    if engine == "word" or (engine == "auto" and word_available()):
        used = _convert_word(paths, progress)
    else:
        used = _convert_libreoffice(paths, progress)
    for path in paths:
        pdf = path.with_suffix(".pdf")
        if not pdf.is_file() or pdf.stat().st_size < 100:
            raise PrepError("A exportação não produziu um PDF válido: " + path.name)
    return used
