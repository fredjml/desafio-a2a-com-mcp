"""Resource `politica://uso`: o texto literal de dados/politica-de-uso.md, como text/markdown."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .data import Dados

URI_POLITICA = "politica://uso"


def registrar_resources(mcp: MCPServer, dados: Dados) -> None:
    @mcp.resource(
        URI_POLITICA,
        name="politica-de-uso",
        description="Politica de uso das salas: versao, janela de uso, duracao maxima e sobreposicao.",
        mime_type="text/markdown",
    )
    def politica_de_uso() -> str:
        return dados.politica_texto
