"""Localização de pastas em Windows e macOS sem privilégios de administrador."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .common import APP_NAME


def app_root() -> Path:
    """Pasta com os recursos da aplicação (código ou pasta do executável empacotado)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    """Modelos Word/PDF da empresa. Podem ser indicados com EP_TOOLBOX_MODELS.

    Na aplicação empacotada fica uma pasta 'models' ao lado do executável, para
    que possa ser substituída sem reconstruir a aplicação.
    """
    override = os.environ.get("EP_TOOLBOX_MODELS")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    return app_root() / "models"


def data_dir() -> Path:
    """Pasta de dados do utilizador: nunca requer administrador."""
    override = os.environ.get("EP_TOOLBOX_DATA")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME
