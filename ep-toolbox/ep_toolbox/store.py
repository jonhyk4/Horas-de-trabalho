"""Armazenamento local: perfis, capas, formulários, edições e cópias de segurança."""
from __future__ import annotations

import copy
import json
import re
import shutil
import threading
from pathlib import Path

from .common import FLEETS, MANUAL_TITLES, PrepError, atomic_json, now_tag, parse_date, profile_key
from .history import clean_history, history_warnings

DEFAULT_FORM = {"code": "9.2.6.4.53", "title": "Notificação de Alterações sem Necessidade de Aprovação Prévia"}
BACKUP_KEEP = 30
FOLDERS = ("capas", "saidas", "temporarios", "copias_seguranca")


def validate_forms(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 200:
        raise PrepError("O banco aceita até 200 formulários.")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise PrepError("Formulário inválido.")
        code = str(row.get("code", "")).strip()
        title = str(row.get("title", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,79}", code) or not title or len(title) > 500:
            raise PrepError("Cada formulário precisa de um código (até 80 caracteres) e uma designação (até 500).")
        if code.casefold() in seen:
            raise PrepError("Código de formulário repetido: " + code)
        seen.add(code.casefold())
        result.append({"code": code, "title": title})
    return result


def selected_forms(bank: list[dict], codes=None) -> list[dict]:
    if codes is None:
        codes = [DEFAULT_FORM["code"]] if any(r["code"] == DEFAULT_FORM["code"] for r in bank) else []
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise PrepError("Seleciona os formulários a referir na carta.")
    available = {r["code"]: r for r in bank}
    if any(code not in available for code in codes):
        raise PrepError("O banco de formulários mudou. Volta a selecionar os formulários da edição.")
    return [dict(available[code]) for code in dict.fromkeys(codes)]


def _blank_profile(fleet: str, manual: str) -> dict:
    return {"fleet": fleet, "manual": manual, "title": MANUAL_TITLES[manual], "part": "OM(B)",
            "anchor": "PLP-LESS", "footer_height": 60, "cover": "", "history": [],
            "validated_sample": False}


class Store:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.lock = threading.RLock()
        for folder in FOLDERS:
            (self.base / folder).mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.base / "perfis.json"
        self.forms_path = self.base / "formularios.json"
        if not self.profiles_path.exists():
            atomic_json(self.profiles_path,
                        {profile_key(f, m): _blank_profile(f, m) for f in FLEETS for m in MANUAL_TITLES})
        if not self.forms_path.exists():
            atomic_json(self.forms_path, [DEFAULT_FORM])

    # ----- cópias de segurança -----
    def _backup(self, path: Path) -> None:
        if path.exists():
            shutil.copy2(path, self.base / "copias_seguranca" / f"{path.stem}_{now_tag()}.json")
        backups = sorted((self.base / "copias_seguranca").glob(f"{path.stem}_*.json"))
        for old in backups[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)

    # ----- perfis -----
    def _read_profiles(self) -> dict:
        return json.loads(self.profiles_path.read_text(encoding="utf-8"))

    def profiles(self) -> dict:
        with self.lock:
            return copy.deepcopy(self._read_profiles())

    def get(self, key: str) -> dict:
        profiles = self.profiles()
        if key not in profiles:
            raise PrepError("Perfil desconhecido.")
        return profiles[key]

    def save_profile(self, profile: dict) -> None:
        key = profile_key(profile["fleet"], profile["manual"])
        with self.lock:
            profiles = self._read_profiles()
            self._backup(self.profiles_path)
            profiles[key] = copy.deepcopy(profile)
            atomic_json(self.profiles_path, profiles)

    def set_history(self, key: str, rows) -> dict:
        profile = self.get(key)
        profile["history"] = clean_history(rows)
        self.save_profile(profile)
        return profile

    def set_cover(self, key: str, source_path) -> dict:
        from pypdf import PdfReader
        profile = self.get(key)
        reader = PdfReader(str(source_path))
        if reader.is_encrypted or len(reader.pages) != 4:
            raise PrepError("Seleciona as quatro páginas TAP, num PDF sem proteção.")
        name = f"{key}__{now_tag()}.pdf"
        shutil.copy2(source_path, self.base / "capas" / name)
        previous = profile.get("cover")
        profile["cover"] = name
        profile["cover_note"] = ""
        if any("NOT APPROVED" in (page.extract_text() or "") for page in reader.pages):
            profile["cover_note"] = "A capa fornecida contém o carimbo NOT APPROVED, que foi preservado."
        self.save_profile(profile)
        if previous and previous != name:
            (self.base / "capas" / previous).unlink(missing_ok=True)
        return profile

    def cover_path(self, profile: dict) -> Path:
        name = profile.get("cover", "")
        if not name or Path(name).name != name:
            raise PrepError("Faltam as capas TAP deste perfil. Importa o PDF de quatro páginas em Perfis e capas.")
        path = self.base / "capas" / name
        if not path.is_file():
            raise PrepError("O ficheiro de capas deste perfil não foi encontrado.")
        return path

    def summary(self) -> list[dict]:
        result = []
        for key, profile in self.profiles().items():
            item = dict(profile, key=key)
            item["cover_ready"] = bool(profile.get("cover") and (self.base / "capas" / profile["cover"]).is_file())
            item["warnings"] = history_warnings(profile.get("history", []))
            result.append(item)
        return result

    # ----- formulários -----
    def forms(self) -> list[dict]:
        with self.lock:
            return validate_forms(json.loads(self.forms_path.read_text(encoding="utf-8")))

    def save_forms(self, rows) -> None:
        rows = validate_forms(rows)
        with self.lock:
            self._backup(self.forms_path)
            atomic_json(self.forms_path, rows)

    # ----- edições -----
    def editions(self) -> list[dict]:
        """Lista as edições concluídas ou falhadas a partir das pastas de saída."""
        result = []
        for folder in (self.base / "saidas").iterdir():
            if not folder.is_dir() or (folder / ".em_curso").exists():
                continue
            report_path = folder / "Edicao.json"
            failed = (folder / "Falha.json").is_file()
            if failed:
                report_path = folder / "Falha.json"
            if not report_path.is_file():
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            profile = report.get("profile", {})
            result.append({
                "folder_name": folder.name,
                "base_name": report.get("base_name", folder.name),
                "fleet": profile.get("fleet", ""), "manual": profile.get("manual", ""),
                "revision": report.get("revision", ""), "issue_date": report.get("issue_date", ""),
                "created_at": report.get("created_at", ""),
                "status": "failed" if failed else "done",
                "error": report.get("error", ""),
                "forms": report.get("forms", []),
                "highlights": report.get("highlights", []),
                "outputs": report.get("outputs", {}),
                "files": sorted(p.name for p in folder.iterdir() if p.is_file()),
            })
        return sorted(result, key=lambda row: row["created_at"], reverse=True)
