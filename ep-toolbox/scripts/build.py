"""Gera a aplicação empacotada para o sistema onde é executado (Windows ou macOS).

Uso (na raiz do projeto):
    python -m pip install --user -r requirements.txt -r requirements-build.txt
    python scripts/build.py

Resultado: dist/EP Toolbox/ (pasta autónoma). Não é necessário instalar Python
nem ter privilégios de administrador para a executar. Copia a pasta para onde
quiseres, por exemplo para o ambiente de trabalho ou para a pasta do utilizador.

Os modelos da empresa (models/*.docx, capas PDF) não são incluídos por defeito;
se existirem na pasta models/ do projeto, são copiados para o pacote.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "EP Toolbox"


def main() -> int:
    if sys.platform not in ("win32", "darwin"):
        print("Aviso: a build oficial é para Windows ou macOS. Esta máquina é " + sys.platform + ".")
    # Ícone e assinatura de código ficam fora deste script: dependem do certificado da empresa.
    separator = os.pathsep
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'ep_toolbox' / 'ui'}{separator}ep_toolbox/ui",
        "--collect-submodules", "pypdf",
        "--hidden-import", "lxml._elementpath",
        str(ROOT / "scripts" / "launcher.py"),
    ]
    models = ROOT / "models"
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    if models.is_dir():
        target = ROOT / "dist" / NAME / "models"
        shutil.copytree(models, target, dirs_exist_ok=True)
        print("Modelos copiados para", target)
    print("Pronto:", ROOT / "dist" / NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
