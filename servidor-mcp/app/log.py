"""Log JSON-lines em STDERR, uma linha por request HTTP, via wrapper ASGI proprio.

Por que nao o middleware do SDK: ele ve um `tools/list` fantasma a cada `tools/call` com argumentos
(validacao interna de Mcp-Param-*), o que polui a evidencia "tools/list antes do primeiro tools/call".
Por que nao o access log do uvicorn: ele escreve em stdout e nao traz metodo/id/traceparent.

Nunca entram no log: requestState, inputResponses, argumentos das tools, segredo.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROCESSO = "servidor-mcp"
# W3C Trace Context (nivel 1): version-traceid-parentid-flags, tudo minusculo; ids nao podem ser zero.
_TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_META_INFO = "io.modelcontextprotocol/clientInfo"
_MAX_TEXTO = 128
# Acima disto o corpo nao e parseado para o log (mas segue intacto para o SDK).
_MAX_CORPO_LOG = 256 * 1024


def traceparent_valido(valor: object) -> str | None:
    """Devolve o traceparent se seguir o formato W3C; senao None (anti log-forging)."""
    if not isinstance(valor, str) or not _TRACEPARENT.fullmatch(valor):
        return None
    _, trace_id, parent_id, _ = valor.split("-")
    if trace_id == "0" * 32 or parent_id == "0" * 16 or valor.startswith("ff-"):
        return None
    return valor


def _texto(valor: object) -> str | None:
    return valor[:_MAX_TEXTO] if isinstance(valor, str) else None


def emitir(evento: str, **campos: Any) -> None:
    """Escreve uma linha JSON em stderr. json.dumps escapa quebras de linha e controles."""
    linha = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proc": PROCESSO,
        "evento": evento,
        **campos,
    }
    print(json.dumps(linha, ensure_ascii=False), file=sys.stderr, flush=True)


def campos_do_request(corpo: bytes, cabecalhos: dict[str, str]) -> dict[str, Any]:
    """Extrai do request apenas o que pode ser logado: metodo, id, traceparent, Mcp-Name, cliente."""
    campos: dict[str, Any] = {
        "method": None,
        "id": None,
        "traceparent": None,
        "mcp_name": _texto(cabecalhos.get("mcp-name")),
        "client": None,
    }
    if not corpo or len(corpo) > _MAX_CORPO_LOG:
        return campos
    try:
        msg = json.loads(corpo)
    except ValueError:
        return campos
    if not isinstance(msg, dict):
        return campos
    campos["method"] = _texto(msg.get("method"))
    ident = msg.get("id")
    if isinstance(ident, str):
        campos["id"] = ident[:_MAX_TEXTO]
    elif isinstance(ident, int) and not isinstance(ident, bool):
        campos["id"] = ident
    params = msg.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict):
        campos["traceparent"] = traceparent_valido(meta.get("traceparent"))
        info = meta.get(_META_INFO)
        if isinstance(info, dict):
            campos["client"] = _texto(info.get("name"))
    return campos


class RegistroDeRequests:
    """Wrapper ASGI: le o corpo, registra 1 linha por request HTTP e repassa o corpo intacto."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        buffer: list[Message] = []
        tamanho = 0
        while True:
            msg = await receive()
            buffer.append(msg)
            if msg["type"] != "http.request":
                break
            tamanho += len(msg.get("body", b""))
            if not msg.get("more_body", False) or tamanho > _MAX_CORPO_LOG:
                break
        completo = (
            bool(buffer)
            and buffer[-1]["type"] == "http.request"
            and not buffer[-1].get("more_body", False)
        )
        corpo = b"".join(m.get("body", b"") for m in buffer if m["type"] == "http.request")
        cabecalhos = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        campos = campos_do_request(corpo if completo else b"", cabecalhos)
        campos["http"] = f"{scope['method']} {scope['path']}"[:_MAX_TEXTO]

        pendentes = list(buffer)
        registrado = False

        async def receber() -> Message:
            if pendentes:
                return pendentes.pop(0)
            return await receive()

        async def enviar(message: Message) -> None:
            nonlocal registrado
            if message["type"] == "http.response.start" and not registrado:
                registrado = True
                emitir("request", status=message["status"], **campos)
            await send(message)

        try:
            await self.app(scope, receber, enviar)
        finally:
            if (
                not registrado
            ):  # a app falhou antes de responder: ainda assim o request foi recebido
                emitir("request", status=None, **campos)
