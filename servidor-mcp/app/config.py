"""Configuracao do processo: tudo vem do ambiente, com falha rapida (fail-fast) no boot.

O segredo do requestState (`REQUEST_STATE_SECRET`) so entra por variavel de ambiente: nunca do
codigo, nunca de arquivo lido por este processo, nunca impresso (nem o valor, nem o tamanho).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

PORTA_PADRAO = 7301
# Placeholder do .env.example: colar o exemplo sem gerar a chave nao pode subir o servidor.
PLACEHOLDER_SEGREDO = "<cole-aqui-64-hex-gerados-localmente>"
MIN_BYTES_SEGREDO = 32
# Barreira contra descuido (nao contra operador malicioso): o segredo real vem de token_hex(32).
MIN_BYTES_DISTINTOS = 16
PERIODO_MAX_REPETIDO = 8
# TTL do requestState: 10 min (faixa 5-30 min do enunciado). REQUEST_STATE_TTL_S existe para os testes de
# expiracao (TTL curto); o limite superior impede alongar a janela alem do que o enunciado admite.
TTL_PADRAO_S = 600.0
TTL_MIN_S = 1.0
TTL_MAX_S = 1800.0
ARQUIVOS_DADOS = ("salas.json", "reservas.json", "politica-de-uso.md")

_HEX = re.compile(r"[0-9a-fA-F]+")

# app/config.py -> app/ -> servidor-mcp/ -> raiz do fork; os dados vivem em <raiz>/dados.
DADOS_PADRAO = Path(__file__).resolve().parents[2] / "dados"


class ConfigError(Exception):
    """Configuracao invalida. A mensagem nunca contem o valor do segredo."""


@dataclass(frozen=True)
class Config:
    porta: int
    dados_dir: Path
    # repr=False: um `print(config)` acidental nao pode vazar a chave.
    request_state_secret: str = field(repr=False)
    request_state_ttl_s: float = TTL_PADRAO_S


def _sem_aleatoriedade(bruto: bytes) -> bool:
    """Poucos bytes distintos, padrao periodico curto (ex.: `deadbeef` x8) ou progressao aritmetica."""
    if len(set(bruto)) < MIN_BYTES_DISTINTOS:
        return True
    for periodo in range(1, PERIODO_MAX_REPETIDO + 1):
        if all(bruto[i] == bruto[i + periodo] for i in range(len(bruto) - periodo)):
            return True
    passos = {(b - a) % 256 for a, b in pairwise(bruto)}
    return len(passos) == 1  # ex.: 00 01 02 ... 1f


def validar_segredo(valor: str | None) -> str:
    """Devolve o segredo normalizado ou levanta ConfigError (sem ecoar o valor)."""
    if valor is None or not valor.strip():
        raise ConfigError(
            "REQUEST_STATE_SECRET ausente: exporte 64 caracteres hexadecimais (32 bytes). "
            'Gere com: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    segredo = valor.strip()
    if segredo == PLACEHOLDER_SEGREDO:
        raise ConfigError(
            "REQUEST_STATE_SECRET ainda e o placeholder do .env.example: gere um valor real."
        )
    if not _HEX.fullmatch(segredo) or len(segredo) % 2 != 0:
        raise ConfigError(
            "REQUEST_STATE_SECRET invalido: use somente hexadecimal, em quantidade par de caracteres."
        )
    bruto = bytes.fromhex(segredo)
    if len(bruto) < MIN_BYTES_SEGREDO:
        raise ConfigError(
            f"REQUEST_STATE_SECRET curto demais: sao necessarios {MIN_BYTES_SEGREDO} bytes "
            f"({MIN_BYTES_SEGREDO * 2} caracteres hexadecimais)."
        )
    if _sem_aleatoriedade(bruto):
        raise ConfigError("REQUEST_STATE_SECRET sem aleatoriedade suficiente: gere um valor novo.")
    return segredo


def _porta(valor: str | None) -> int:
    if valor is None or not valor.strip():
        return PORTA_PADRAO
    try:
        porta = int(valor.strip())
    except ValueError:
        raise ConfigError("MCP_PORT invalida: use um inteiro entre 1 e 65535.") from None
    if not 1 <= porta <= 65535:
        raise ConfigError("MCP_PORT invalida: use um inteiro entre 1 e 65535.")
    return porta


def _ttl(valor: str | None) -> float:
    if valor is None or not valor.strip():
        return TTL_PADRAO_S
    try:
        ttl = float(valor.strip())
    except ValueError:
        raise ConfigError("REQUEST_STATE_TTL_S invalido: use um numero de segundos.") from None
    if not TTL_MIN_S <= ttl <= TTL_MAX_S:  # tambem rejeita nan/inf
        raise ConfigError(f"REQUEST_STATE_TTL_S invalido: use de {TTL_MIN_S:g} a {TTL_MAX_S:g} s.")
    return ttl


def _dados_dir(valor: str | None) -> Path:
    pasta = Path(valor.strip()).resolve() if valor and valor.strip() else DADOS_PADRAO
    faltando = [nome for nome in ARQUIVOS_DADOS if not (pasta / nome).is_file()]
    if faltando:
        raise ConfigError(f"pasta de dados invalida ({pasta}): faltam {', '.join(faltando)}.")
    return pasta


def carregar_config(env: Mapping[str, str] | None = None) -> Config:
    """Le e valida o ambiente. `env` injetavel para teste; padrao os.environ."""
    ambiente = os.environ if env is None else env
    return Config(
        porta=_porta(ambiente.get("MCP_PORT")),
        dados_dir=_dados_dir(ambiente.get("DADOS_DIR")),
        request_state_secret=validar_segredo(ambiente.get("REQUEST_STATE_SECRET")),
        request_state_ttl_s=_ttl(ambiente.get("REQUEST_STATE_TTL_S")),
    )
