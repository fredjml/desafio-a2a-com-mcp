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


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


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


def registrar_tools(mcp: MCPServer, dados: Dados) -> None:
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
