"""Tools MCP. Erros de EXECUCAO viram CallToolResult(is_error=True) com o texto EXATO da regra.

Por que nao `raise`: o SDK prefixa a excecao com "Error executing tool <nome>: " (e com ValueError
some com a mensagem). O enunciado pede o texto exato; devolver o CallToolResult evita o prefixo.
"""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel

from .data import Dados
from .policy import ErroDeDominio, validar_pedido
from .reservations import Agenda
from .tempo import formatar_instante


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


# Forma unica do structuredContent da reserva: concluida (wire 02/04) ou recusada (wire 11).
# Sem docstring de proposito: o pydantic a publicaria como `description` no outputSchema e o
# tools/list deixaria de ser identico ao wire 01.
class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def resultado_ok(saida: BaseModel) -> CallToolResult:
    """structuredContent + o MESMO JSON serializado em bloco de texto (compatibilidade, check 03)."""
    estruturado = saida.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(estruturado, indent=2))],
        structured_content=estruturado,
    )


def resultado_erro(mensagem: str) -> CallToolResult:
    """Erro de execucao (isError:true) com o texto exato, sem prefixo do SDK."""
    return CallToolResult(content=[TextContent(type="text", text=mensagem)], is_error=True)


def registrar_tools(mcp: MCPServer, dados: Dados, agenda: Agenda) -> None:
    ids_das_salas = frozenset(s.id for s in dados.salas)

    @mcp.tool(name="listar_salas", description="Lista todas as salas com capacidade e recursos.")
    async def listar_salas() -> Annotated[CallToolResult, ListaDeSalas]:
        return resultado_ok(
            ListaDeSalas(
                salas=[
                    SalaOut(
                        id=s.id, nome=s.nome, capacidade=s.capacidade, recursos=list(s.recursos)
                    )
                    for s in dados.salas
                ]
            )
        )

    @mcp.tool(
        name="consultar_disponibilidade",
        description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
    )
    async def consultar_disponibilidade(
        sala: str, inicio: str, fim: str
    ) -> Annotated[CallToolResult, Disponibilidade]:
        try:
            intervalo = validar_pedido(sala, inicio, fim, ids_das_salas)
        except ErroDeDominio as erro:
            return resultado_erro(erro.mensagem)
        conflitos = agenda.conflitos(sala, intervalo)
        return resultado_ok(
            Disponibilidade(
                sala=sala,
                livre=not conflitos,
                conflitos=[
                    ConflitoOut(
                        id=r.id,
                        inicio=formatar_instante(r.inicio),
                        fim=formatar_instante(r.fim),
                        responsavel=r.responsavel,
                    )
                    for r in conflitos
                ],
            )
        )
