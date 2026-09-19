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
# Retencao em memoria (M2): teto de Tasks e validade do estado de uma Task pausada. 660 s = os 600 s do
# requestState no servidor + 60 s de margem; se REQUEST_STATE_TTL_S do servidor for maior, aumente.
MAX_TASKS_PADRAO = 1000
PAUSA_TTL_PADRAO_S = 660.0


class ConfigError(Exception):
    """Configuracao invalida."""


@dataclass(frozen=True)
class Config:
    a2a_port: int
    mcp_url: str
    mcp_timeout_s: float
    card_host: str
    max_tasks: int = MAX_TASKS_PADRAO
    pausa_ttl_s: float = PAUSA_TTL_PADRAO_S

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


def _max_tasks(valor: str | None) -> int:
    if valor is None or not valor.strip():
        return MAX_TASKS_PADRAO
    try:
        maximo = int(valor.strip())
    except ValueError:
        raise ConfigError("A2A_MAX_TASKS invalido: use um inteiro entre 1 e 1000000.") from None
    if not 1 <= maximo <= 1_000_000:
        raise ConfigError("A2A_MAX_TASKS invalido: use um inteiro entre 1 e 1000000.")
    return maximo


def _pausa_ttl(valor: str | None) -> float:
    if valor is None or not valor.strip():
        return PAUSA_TTL_PADRAO_S
    try:
        segundos = float(valor.strip())
    except ValueError:
        raise ConfigError("A2A_PAUSA_TTL_S invalido: use um numero de segundos.") from None
    if not 1.0 <= segundos <= 86400.0:  # tambem rejeita nan/inf
        raise ConfigError("A2A_PAUSA_TTL_S invalido: use entre 1 e 86400 segundos.")
    return segundos


def carregar_config(env: Mapping[str, str] | None = None) -> Config:
    ambiente = os.environ if env is None else env
    return Config(
        a2a_port=_porta(ambiente.get("A2A_PORT")),
        mcp_url=_mcp_url(ambiente.get("MCP_URL")),
        mcp_timeout_s=_timeout(ambiente.get("MCP_TIMEOUT_S")),
        card_host=_host_card(ambiente.get("A2A_CARD_HOST")),
        max_tasks=_max_tasks(ambiente.get("A2A_MAX_TASKS")),
        pausa_ttl_s=_pausa_ttl(ambiente.get("A2A_PAUSA_TTL_S")),
    )
