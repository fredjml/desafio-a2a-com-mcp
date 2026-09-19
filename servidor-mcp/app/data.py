"""Carga dos dados do dominio (somente leitura, no boot). Sem ORM, sem banco: JSON e Markdown."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .tempo import interpretar_instante


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: tuple[str, ...]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: datetime  # sempre normalizado para -03:00 (ver tempo.interpretar_instante)
    fim: datetime
    responsavel: str


@dataclass(frozen=True)
class Dados:
    salas: tuple[Sala, ...]
    reservas: tuple[Reserva, ...]
    politica_texto: str
    politica_versao: str


class DadosError(Exception):
    """Arquivo de dados ausente ou malformado."""


def _ler_json(caminho: Path) -> list[dict[str, Any]]:
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DadosError(f"nao foi possivel ler {caminho.name}: {exc}") from exc
    if not isinstance(bruto, list) or not all(isinstance(item, dict) for item in bruto):
        raise DadosError(f"{caminho.name} deve ser uma lista de objetos")
    return bruto


def versao_da_politica(texto: str) -> str:
    """A primeira linha e `versao: <valor>`; o valor e o que o agente devolve no artifact."""
    primeira = texto.splitlines()[0] if texto.splitlines() else ""
    chave, separador, valor = primeira.partition(":")
    if chave.strip().lower() != "versao" or not separador or not valor.strip():
        raise DadosError("a primeira linha da politica deve ser 'versao: <valor>'")
    return valor.strip()


def carregar_dados(pasta: Path) -> Dados:
    try:
        salas = tuple(
            Sala(
                id=str(item["id"]),
                nome=str(item["nome"]),
                capacidade=int(item["capacidade"]),
                recursos=tuple(str(r) for r in item["recursos"]),
            )
            for item in _ler_json(pasta / "salas.json")
        )
        reservas = tuple(
            Reserva(
                id=str(item["id"]),
                sala=str(item["sala"]),
                inicio=interpretar_instante(str(item["inicio"])),
                fim=interpretar_instante(str(item["fim"])),
                responsavel=str(item["responsavel"]),
            )
            for item in _ler_json(pasta / "reservas.json")
        )
        # read_text traduz CRLF -> LF: o texto servido e o do wire 05 em qualquer checkout (Windows/Linux).
        politica = (pasta / "politica-de-uso.md").read_text(encoding="utf-8")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise DadosError(f"dados invalidos em {pasta}: {exc!r}") from exc
    return Dados(salas, reservas, politica, versao_da_politica(politica))
