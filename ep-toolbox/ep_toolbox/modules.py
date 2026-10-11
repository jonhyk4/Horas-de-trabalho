"""Registo de módulos de trabalho.

Cada módulo sabe analisar a origem de um tipo de perfil. Para acrescentar uma
funcionalidade nova, cria uma classe Module e acrescenta-a a MODULES.
"""
from __future__ import annotations

from pathlib import Path

from .common import PrepError
from .pdf_analysis import analyze_pdf
from .qrh import compile_zip, qrh_fleets


class Module:
    id = ""
    title = ""
    source_label = ""
    source_suffix = ""

    def handles(self, profile: dict) -> bool:
        raise NotImplementedError

    def analyze(self, source_path, profile, work_dir, progress, resolve_profile=None):
        """Devolve (caminho do PDF a usar na geração, análise, perfil efetivo)."""
        raise NotImplementedError


class ManualModule(Module):
    id = "manuais"
    title = "Manuais"
    source_label = "PDF original do fabricante"
    source_suffix = ".pdf"

    def handles(self, profile):
        return profile["manual"] != "QRH"

    def analyze(self, source_path, profile, work_dir, progress, resolve_profile=None):
        analysis = analyze_pdf(source_path, profile, progress)
        analysis.update(profile_key=None, cover_pages=[])
        return Path(source_path), analysis, profile


class QrhModule(Module):
    id = "qrh"
    title = "Edição QRHs"
    source_label = "ZIP original de QRHs"
    source_suffix = ".zip"

    def handles(self, profile):
        return profile["manual"] == "QRH"

    def analyze(self, source_path, profile, work_dir, progress, resolve_profile=None):
        compiled, manifest = compile_zip(source_path, work_dir, progress)
        fleets = qrh_fleets(manifest)
        if len(fleets) > 1:
            details = "; ".join(f"{row['registration'] or row['file']}: {row['detected_fleet'] or 'não identificada'}"
                                for row in manifest["inventory"])
            raise PrepError("O ZIP contém QRHs de frotas diferentes. Separa-os por frota. " + details)
        effective = profile
        if len(fleets) == 1:
            detected = next(iter(fleets))
            if detected != profile["fleet"]:
                if resolve_profile is None:
                    raise PrepError(f"O ZIP contém QRHs {detected}, mas está selecionada a frota "
                                    f"{profile['fleet']}. Seleciona {detected} para usar as capas e o ROA corretos.")
                effective = resolve_profile(detected + "__QRH")
                if effective["fleet"] != detected or effective["manual"] != "QRH":
                    raise PrepError("Não foi possível selecionar o perfil QRH da frota identificada.")
        analysis = analyze_pdf(compiled, effective, lambda text, value: progress(text, 30 + int(value * .6)),
                               qrh=True, ignore_date_pages=manifest["cover_pages"])
        analysis.update(
            profile_key=effective["fleet"] + "__QRH", selected_fleet=profile["fleet"],
            fleet_changed=effective["fleet"] != profile["fleet"],
            qrh_inventory=manifest["inventory"], qrh_ignored=manifest["ignored"],
            zip_sha256=manifest["zip_sha256"], cover_pages=manifest["cover_pages"],
        )
        analysis["warnings"].append(
            "QRH: as capas Airbus são conservadas no completo. O ROA é colocado depois das capas TAP e antes "
            "dos QRHs compilados. Confirma os cabeçalhos, as secções FI/TRL e as páginas alteradas no relatório.")
        return compiled, analysis, effective


MODULES: list[Module] = [ManualModule(), QrhModule()]


def module_for(profile: dict) -> Module:
    for module in MODULES:
        if module.handles(profile):
            return module
    raise PrepError("Nenhum módulo aceita este perfil.")
