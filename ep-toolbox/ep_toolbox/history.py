"""Histórico ROA (Record of Amendments): validação, cálculo da revisão e atualização."""
from __future__ import annotations

import copy
import re

from .common import PrepError, english_date, parse_date

REVISION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,19}")
HISTORY_FIELDS = ("revision", "date", "approval_date", "approved")


def clean_history(rows) -> list[dict]:
    """Valida linhas vindas da UI ou de um ROA importado e devolve linhas normalizadas."""
    if not isinstance(rows, list) or len(rows) > 5000:
        raise PrepError("Histórico inválido.")
    clean = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise PrepError("Histórico inválido.")
        row = {field: str(raw.get(field, "")).strip() for field in HISTORY_FIELDS}
        if any(len(value) > 250 for value in row.values()):
            raise PrepError("Há um campo demasiado longo no histórico.")
        if not row["revision"] or not parse_date(row["date"]):
            raise PrepError("Cada linha deve ter número de revisão e data válidos.")
        if row["approval_date"] not in ("", "--"):
            parse_date(row["approval_date"])
        clean.append(row)
    return clean


def history_warnings(rows) -> list[str]:
    seen, repeated, numbers = set(), set(), []
    for row in rows:
        revision = str(row["revision"]).strip()
        if revision in seen:
            repeated.add(revision)
        seen.add(revision)
        if revision.isdigit():
            numbers.append(int(revision))
    warnings = []
    if repeated:
        warnings.append(
            "Revisões repetidas no histórico importado: " + ", ".join(sorted(repeated))
            + ". Foram preservadas; podes revê-las no separador Histórico."
        )
    if numbers:
        missing = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
        if missing:
            warnings.append(
                "Números ausentes no histórico: " + ", ".join(f"{n:02d}" for n in missing)
                + ". Não foram inventadas revisões."
            )
    return warnings


def proposed_revision(history, issue_date) -> tuple[str, bool]:
    """Devolve (revisão proposta, se a data já existe no histórico)."""
    if not history:
        return "", False
    issue = parse_date(issue_date)
    exists = any(row.get("date") and parse_date(row["date"]) == issue for row in history)
    last = str(history[-1]["revision"]).strip()
    if not last.isdigit():
        raise PrepError("A última linha do ROA deve ter uma revisão numérica para calcular a seguinte.")
    return f"{int(last) + 1:02d}", exists


def update_history(history, revision, issue_date, start_empty=False) -> tuple[list[dict], bool]:
    """Acrescenta uma revisão sem alterar o original. Devolve (novo histórico, se foi acrescentada)."""
    rows = copy.deepcopy(history)
    revision = str(revision).strip()
    if not REVISION_RE.fullmatch(revision):
        raise PrepError("Indica um número de revisão válido (até 20 caracteres).")
    issue = parse_date(issue_date)
    if not issue:
        raise PrepError("Falta a data da edição.")
    if not rows and not start_empty:
        raise PrepError("Importa o histórico ROA ou confirma que pretendes iniciar um histórico novo.")
    for row in rows:
        row_date = parse_date(row["date"])
        if str(row["revision"]) == revision:
            if row_date != issue:
                raise PrepError(f"A revisão {revision} já existe com outra data. Corrige o histórico ou o número.")
            return rows, False
        if row_date == issue:
            raise PrepError(f"Esta edição já está registada como revisão {row['revision']}.")
    dated = [parse_date(row["date"]) for row in rows if row.get("date")]
    if dated and issue <= max(dated):
        raise PrepError("A data é anterior à última revisão guardada. O histórico não foi alterado.")
    rows.append({"revision": revision, "date": english_date(issue.isoformat()), "approval_date": "", "approved": ""})
    return rows, True
