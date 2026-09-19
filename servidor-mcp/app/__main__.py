"""Ponto de entrada: `python -m app` (a partir de servidor-mcp/, com o venv do servidor)."""

from __future__ import annotations

import sys

from .config import ConfigError, carregar_config
from .data import DadosError
from .log import emitir
from .server import PortaIndisponivel, servir


def main() -> int:
    try:
        config = carregar_config()
        servir(config)
    except ConfigError as exc:
        emitir("erro_boot", motivo=str(exc))
        return 2
    except DadosError as exc:
        emitir("erro_boot", motivo=str(exc))
        return 2
    except PortaIndisponivel as exc:  # so o bind; um OSError de runtime nao e "porta"
        emitir("erro_boot", motivo=f"nao foi possivel abrir a porta: {exc}")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
