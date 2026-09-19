"""Configuracao do agente: tudo vem do ambiente, com falha rapida no boot.

O agente nao tem segredo: o `requestState` do MCP e opaco para ele (guardar e ecoar).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

PORTA_A2A_PADRAO = 7300
MCP_URL_PADRAO = "http://localhost:7301/mcp"
# Host que aparece no `url` do agent card (o wire de referencia usa 127.0.0.1).
HOST_CARD_PADRAO = "127.0.0.1"
TIMEOUT_MCP_PADRAO_S = 10.0
TIMEOUT_MCP_MAX_S = 120.0


class ConfigError(Exception):
    """Configuracao invalida."""


@dataclass(frozen=True)
class Config:
    a2a_port: int
    mcp_url: str
    mcp_timeout_s: float
    card_host: str

    @property
    def card_url(self) -> str:
        """URL do endpoint JSON-RPC anunciada no card."""
        host = f"[{self.card_host}]" if ":" in self.card_host else self.card_host
        return f"http://{host}:{self.a2a_port}/a2a"


def _porta(valor: str | None) -> int:
    if valor is None or not valor.strip():
        return PORTA_A2A_PADRAO
    try:
        porta = int(valor.strip())
    except ValueError:
        raise ConfigError("A2A_PORT invalida: use um inteiro entre 1 e 65535.") from None
    if not 1 <= porta <= 65535:
        raise ConfigError("A2A_PORT invalida: use um inteiro entre 1 e 65535.")
    return porta


def _mcp_url(valor: str | None) -> str:
    if valor is None or not valor.strip():
        return MCP_URL_PADRAO
    url = valor.strip()
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ConfigError(
            "MCP_URL invalida: use uma URL http(s) completa, ex.: http://host:7301/mcp"
        )
    return url


def _timeout(valor: str | None) -> float:
    if valor is None or not valor.strip():
        return TIMEOUT_MCP_PADRAO_S
    try:
        segundos = float(valor.strip())
    except ValueError:
        raise ConfigError("MCP_TIMEOUT_S invalido: use um numero de segundos.") from None
    if not 0.05 <= segundos <= TIMEOUT_MCP_MAX_S:  # tambem rejeita nan/inf
        raise ConfigError(
            f"MCP_TIMEOUT_S invalido: use entre 0.05 e {TIMEOUT_MCP_MAX_S:g} segundos."
        )
    return segundos


def _host_card(valor: str | None) -> str:
    if valor is None or not valor.strip():
        return HOST_CARD_PADRAO
    host = valor.strip()
    if any(c in host for c in "/?#@ \t\r\n"):
        raise ConfigError("A2A_CARD_HOST invalido: use apenas o nome do host ou IP.")
    return host


def carregar_config(env: Mapping[str, str] | None = None) -> Config:
    ambiente = os.environ if env is None else env
    return Config(
        a2a_port=_porta(ambiente.get("A2A_PORT")),
        mcp_url=_mcp_url(ambiente.get("MCP_URL")),
        mcp_timeout_s=_timeout(ambiente.get("MCP_TIMEOUT_S")),
        card_host=_host_card(ambiente.get("A2A_CARD_HOST")),
    )
