"""Montagem do servidor: MCPServer + app ASGI + bind em 2 sockets (127.0.0.1 e ::1)."""

from __future__ import annotations

import os
import socket
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer, RequestStateSecurity
from starlette.types import ASGIApp

from .config import Config
from .data import Dados, carregar_dados
from .log import RegistroDeRequests, emitir
from .tools import registrar_tools

# O nome entra no `aud` do requestState selado: FIXO, senao um restart/rename invalida os estados.
NOME_SERVIDOR = "central-de-salas"
VERSAO_SERVIDOR = "1.0.0"
TTL_REQUEST_STATE_S = 600.0  # 10 min, dentro da faixa 5-30 min do enunciado
COMANDO = "python -m app"


def criar_servidor(config: Config, dados: Dados) -> MCPServer:
    """MCPServer com request_state_security SEMPRE explicito.

    Sem `keys=` o SDK sela com uma chave process-local aleatoria: o estado nao sobreviveria a um
    restart e REQUEST_STATE_SECRET seria ignorado (armadilha R19; o SDK nao avisa).
    """
    mcp = MCPServer(
        NOME_SERVIDOR,
        version=VERSAO_SERVIDOR,
        request_state_security=RequestStateSecurity(
            keys=[config.request_state_secret], ttl=TTL_REQUEST_STATE_S
        ),
        log_level="WARNING",  # o SDK nao loga request; evita ruido no stderr
    )
    registrar_tools(mcp, dados)
    return mcp


def criar_app(config: Config) -> ASGIApp:
    dados = carregar_dados(config.dados_dir)
    mcp = criar_servidor(config, dados)
    # json_response=True: sempre JSON (nunca SSE), mesmo com Accept "application/json, text/event-stream".
    return RegistroDeRequests(mcp.streamable_http_app(json_response=True))


def abrir_sockets(porta: int) -> list[socket.socket]:
    """Um socket IPv4 (127.0.0.1) e um IPv6 (::1). NUNCA todas as interfaces.

    So 127.0.0.1 faz `localhost` custar ~2 s por request em clientes que tentam ::1 primeiro (urllib
    do validador); `host="::"` no Windows escuta so IPv6. Por isso dois sockets.
    """
    abertos: list[socket.socket] = []
    for familia, endereco in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(familia, socket.SOCK_STREAM)
        except OSError:
            emitir("aviso", mensagem=f"familia de enderecos indisponivel: {endereco}")
            continue
        try:
            if os.name != "nt":  # no Windows SO_REUSEADDR permite sequestrar porta em uso
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if familia == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((endereco, porta))
            s.listen(100)
        except OSError as exc:
            s.close()
            if familia == socket.AF_INET:  # IPv4 e obrigatorio
                for outro in abertos:
                    outro.close()
                raise
            emitir("aviso", mensagem=f"nao foi possivel escutar em {endereco}: {exc}")
            continue
        s.setblocking(False)
        abertos.append(s)
    return abertos


def servir(config: Config) -> None:
    app = criar_app(config)
    sockets = abrir_sockets(config.porta)
    escutando = [str(s.getsockname()[0]) for s in sockets]
    emitir(
        "boot",
        comando=COMANDO,
        porta=config.porta,
        endpoint="/mcp",
        escutando=escutando,
        servidor=NOME_SERVIDOR,
        versao=VERSAO_SERVIDOR,
        request_state="selado com keys explicitas de REQUEST_STATE_SECRET",
        request_state_ttl_s=TTL_REQUEST_STATE_S,
        python=sys.version.split()[0],
    )
    # access_log=False: o access log do uvicorn vai a stdout e nao traz metodo/id/traceparent.
    servidor = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, http="h11", ws="none")
    )
    servidor.run(sockets=sockets)
