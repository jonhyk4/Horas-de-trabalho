"""Ponto de entrada usado pelo PyInstaller (evita importações relativas no script principal)."""
import sys

from ep_toolbox.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
