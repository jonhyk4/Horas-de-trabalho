"""Ponto de entrada: python -m ep_toolbox [--no-browser] [--data PASTA]."""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .paths import data_dir
from .server import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="EP Toolbox")
    parser.add_argument("--no-browser", action="store_true", help="não abrir o navegador")
    parser.add_argument("--data", type=Path, default=None, help="pasta de dados (predefinida: pasta do utilizador)")
    args = parser.parse_args(argv)
    folder = args.data or data_dir()
    try:
        run(folder, open_browser=not args.no_browser)
    except Exception:
        # Sem consola (duplo clique no Windows/macOS), o erro fica num ficheiro na pasta de dados.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "erro_arranque.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(traceback.format_exc(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
