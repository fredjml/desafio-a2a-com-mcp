"""Ponto de entrada: `python -m app` (a partir de agente/, com o venv do agente).

Sobe o servidor A2A em A2A_PORT (padrao 7300; `POST /a2a` + `/.well-known/agent-card.json`).
O MCP (MCP_URL, padrao http://localhost:7301/mcp) e contatado sob demanda: o boot NAO exige o MCP.
"""

from __future__ import annotations

import os
import socket
import sys

import uvicorn

from .a2a import criar_app
from .config import ConfigError, carregar_config
from .log import emitir, silenciar_ruido

COMANDO = "python -m app"


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


def main() -> int:
    silenciar_ruido()
    try:
        config = carregar_config()
    except ConfigError as exc:
        emitir("erro_boot", motivo=str(exc))
        return 2
    try:
        app = criar_app(config)
        sockets = abrir_sockets(config.a2a_port)
    except OSError as exc:
        emitir("erro_boot", motivo=f"nao foi possivel abrir a porta: {exc}")
        return 1
    emitir(
        "boot",
        comando=COMANDO,
        porta=config.a2a_port,
        endpoint="/a2a",
        card=config.card_url,
        escutando=[str(s.getsockname()[0]) for s in sockets],
        mcp_url=config.mcp_url,
        mcp_timeout_s=config.mcp_timeout_s,
        python=sys.version.split()[0],
    )
    # access_log=False: o access log do uvicorn vai a stdout e nao traz metodo/id/traceparent.
    servidor = uvicorn.Server(
        uvicorn.Config(app.asgi(), log_level="warning", access_log=False, http="h11", ws="none")
    )
    try:
        servidor.run(sockets=sockets)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
