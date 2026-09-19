from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest

from .procs import ServidorMcpReal, exigir_servidor_real


@pytest.fixture(scope="session")
def segredo() -> str:
    """Segredo NOVO em memoria por execucao; nunca impresso nem gravado."""
    return secrets.token_hex(32)


@pytest.fixture
def servidor_mcp(segredo: str) -> Iterator[ServidorMcpReal]:
    """Servidor MCP REAL recem-iniciado (venv do servidor), um por teste (estado das reservas e em memoria)."""
    exigir_servidor_real()
    srv = ServidorMcpReal(segredo=segredo)
    try:
        srv.esperar_porta(srv.porta)
        yield srv
    finally:
        srv.parar()
