from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest

from .servidor_proc import Servidor


@pytest.fixture(scope="session")
def segredo() -> str:
    """Segredo NOVO em memoria por execucao; nunca impresso nem gravado."""
    return secrets.token_hex(32)


@pytest.fixture(scope="module")
def servidor(segredo: str) -> Iterator[Servidor]:
    """Servidor real recem-iniciado, um por modulo de teste (estado das reservas e em memoria)."""
    srv = Servidor(segredo=segredo)
    try:
        srv.esperar_pronto()
        yield srv
    finally:
        srv.parar()


@pytest.fixture
def fresco(segredo: str) -> Iterator[Servidor]:
    """Processo novo por teste (o estado das reservas e em memoria)."""
    srv = Servidor(segredo=segredo)
    try:
        srv.esperar_pronto()
        yield srv
    finally:
        srv.parar()
