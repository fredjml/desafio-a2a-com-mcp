"""Protecao de entrada do agente (M1/M2/M3 do review de seguranca), no mesmo molde do servidor MCP.

O agente escuta so em loopback e nao tem autenticacao (fora do escopo do enunciado), mas loopback nao
protege contra o NAVEGADOR do usuario: uma pagina qualquer consegue um POST "simples" (`text/plain`,
sem preflight) ou usar DNS rebinding. Por isso, como o SDK do servidor MCP faz (transport_security):

  * `POST` no endpoint JSON-RPC exige `Content-Type: application/json`           -> 400
  * `Host` so de loopback (`localhost`, `127.0.0.1`, `[::1]`, com ou sem porta) ou `A2A_CARD_HOST` -> 421
  * `Origin` ausente e aceito; presente, so se for de loopback                    -> 403
  * corpo acima de 4 MiB (como o MCP)                                             -> 413
  * corpo que nao e JSON valido (ou e aninhado demais: `RecursionError`)          -> 400, -32700

O corpo e lido em blocos, com contagem: nunca passa de `max_corpo` bytes em memoria. O JSON e
parseado aqui so para recusar cedo (o SDK de A2A imprime traceback no stderr em JSON invalido e devolve
o texto interno da excecao); o corpo segue intacto para o SDK.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_CORPO_BYTES = 4 * 1024 * 1024
HOSTS_LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]"})
_ORIGENS_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_ERRO_PARSE = {
    "jsonrpc": "2.0",
    "id": None,
    "error": {"code": -32700, "message": "Parse error: corpo nao e um JSON valido"},
}


def host_sem_porta(valor: str) -> str | None:
    """`Host` sem a porta, em minusculas (`[::1]` mantem os colchetes). None se malformado."""
    texto = valor.strip().lower()
    if not texto:
        return None
    if texto.startswith("["):
        fim = texto.find("]")
        if fim < 0:
            return None
        host, resto = texto[: fim + 1], texto[fim + 1 :]
    else:
        host, separador, porta = texto.partition(":")
        resto = separador + porta
    if resto and not (resto.startswith(":") and resto[1:].isascii() and resto[1:].isdigit()):
        return None
    return host


def origem_de_loopback(valor: str) -> bool:
    """`Origin` presente so vale se for http(s) em loopback (`null`, outros hosts e userinfo: nao)."""
    try:
        partes = urlsplit(valor)
        _ = partes.port  # levanta ValueError se a porta for invalida
    except ValueError:
        return False
    if "@" in partes.netloc:  # Origin nunca carrega credenciais; `http://x@localhost` e engano
        return False
    return partes.scheme in ("http", "https") and (partes.hostname or "") in _ORIGENS_LOOPBACK


def _normalizar_host_permitido(host: str) -> str:
    minusculo = host.strip().lower()
    return f"[{minusculo}]" if ":" in minusculo and not minusculo.startswith("[") else minusculo


class ProtecaoDeEntrada:
    """Middleware ASGI. `caminhos_rpc`: onde valem Content-Type, limite e validade do JSON."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        caminhos_rpc: Iterable[str],
        hosts_permitidos: Iterable[str] = (),
        max_corpo: int = MAX_CORPO_BYTES,
    ) -> None:
        self.app = app
        self.caminhos_rpc = frozenset(caminhos_rpc)
        self.hosts = HOSTS_LOOPBACK | {_normalizar_host_permitido(h) for h in hosts_permitidos}
        self.max_corpo = max_corpo

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cabecalhos = Headers(scope=scope)
        rpc_post = scope["method"] == "POST" and scope["path"] in self.caminhos_rpc

        # Mesma ordem do SDK do MCP: Content-Type (so POST), depois Host e Origin.
        tipo = cabecalhos.get("content-type", "").split(";")[0].strip().lower()
        # Igualdade exata do tipo de midia: mais estrito que o SDK do MCP (`application/jsonx` passa la).
        if rpc_post and tipo != "application/json":
            await PlainTextResponse("Invalid Content-Type header", status_code=400)(
                scope, receive, send
            )
            return
        host = host_sem_porta(cabecalhos.get("host", ""))
        if host is None or host not in self.hosts:
            await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
            return
        origem = cabecalhos.get("origin")
        if origem and not origem_de_loopback(origem):
            await PlainTextResponse("Invalid Origin header", status_code=403)(scope, receive, send)
            return

        declarado = cabecalhos.get("content-length")
        if (
            declarado is not None
            and declarado.isascii()
            and declarado.isdigit()
            and int(declarado) > self.max_corpo
        ):
            await self._grande(scope, receive, send)
            return
        if not rpc_post:
            await self.app(scope, receive, send)
            return

        corpo = bytearray()
        while True:  # contagem em streaming: nunca acumula mais que `max_corpo` bytes
            msg = await receive()
            if msg["type"] != "http.request":  # o cliente desconectou
                return
            corpo.extend(msg.get("body", b""))
            if len(corpo) > self.max_corpo:
                await self._grande(scope, receive, send)
                return
            if not msg.get("more_body", False):
                break
        try:
            json.loads(bytes(corpo))
        except (ValueError, RecursionError):  # invalido, ou aninhado demais para o parser
            await JSONResponse(_ERRO_PARSE, status_code=400)(scope, receive, send)
            return

        entregue = False

        async def reenviar() -> Message:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": bytes(corpo), "more_body": False}
            return await receive()

        await self.app(scope, reenviar, send)

    @staticmethod
    async def _grande(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
