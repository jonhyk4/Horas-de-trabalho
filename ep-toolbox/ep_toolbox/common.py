"""Constantes, datas e utilitários de ficheiros partilhados pela aplicação."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

VERSION = "3.0.0"
APP_NAME = "EP Toolbox"

FLEETS = ("A320F", "A330")
MANUAL_TITLES = {
    "AFM": "AIRPLANE FLIGHT MANUAL",
    "CCAP": "CABIN CREW AIRLINE POLICY",
    "CDL": "CONFIGURATION DEVIATION LIST",
    "FCAP": "FLIGHT CREW AIRLINE POLICY",
    "FCOM": "FLIGHT CREW OPERATION MANUAL",
    "FCTM": "FLIGHT CREW TECHNIQUES MANUAL",
    "MEL": "MINIMUM EQUIPMENT LIST",
    "WBM": "WEIGHT AND BALANCE MANUAL",
    "QRH": "QUICK REFERENCE HANDBOOK",
}
# Manuais que não levam ROA por defeito (mantém o comportamento da versão anterior).
MANUALS_WITHOUT_ROA_DEFAULT = ("AFM", "CDL", "WBM")

MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(JAN|FEB|MAR|APR|ABR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*(\d{2}|\d{4})\b",
    re.I,
)


class PrepError(Exception):
    """Erro esperado, com uma mensagem legível para o utilizador."""

    def __init__(self, message: str, recovery: dict | None = None):
        super().__init__(message)
        self.recovery = recovery


def fleet_label(fleet: str) -> str:
    return "A319/A320/A321" if fleet == "A320F" else fleet


def profile_key(fleet: str, manual: str) -> str:
    if fleet not in FLEETS or manual not in MANUAL_TITLES:
        raise PrepError("Frota ou manual inválido.")
    return f"{fleet}__{manual}"


def parse_date(value) -> date | None:
    """Aceita AAAA-MM-DD ou DD MMM AA (inglês ou ABR). Devolve None se vazio."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    match = DATE_RE.fullmatch(value)
    if not match:
        raise PrepError(f"Data inválida: {value}. Usa AAAA-MM-DD ou DD MMM AA.")
    day, month, year = match.groups()
    year = int(year)
    if year < 100:
        year += 2000
    try:
        return date(year, MONTHS.index(month.upper().replace("ABR", "APR")) + 1, int(day))
    except ValueError as error:
        raise PrepError(f"Data inválida: {value}.") from error


def english_date(value) -> str:
    parsed = parse_date(value)
    return f"{parsed.day:02d} {MONTHS[parsed.month - 1]} {parsed.year % 100:02d}" if parsed else ""


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value) -> None:
    """Escreve JSON de forma atómica: primeiro um ficheiro temporário, depois substitui."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")
