"""Log JSON-lines em STDERR. Nunca escreve `requestState` (opaco) nem respostas de usuario em claro.

`emitir` descarta qualquer campo cujo nome indique estado/segredo, como ultima barreira: quem loga
tambem nao deve passar esses campos.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .trace import trace_id_de

PROCESSO = "agente"
_PROIBIDOS = frozenset(
    {
        "request_state",
        "requeststate",
        "requestState",
        "state",
        "input_responses",
        "inputresponses",
        "inputResponses",
        "secret",
        "segredo",
    }
)


def emitir(evento: str, **campos: Any) -> None:
    """Escreve uma linha JSON em stderr. json.dumps escapa quebras de linha e controles."""
    seguros = {k: v for k, v in campos.items() if k not in _PROIBIDOS}
    linha = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proc": PROCESSO,
        "evento": evento,
        **seguros,
    }
    print(json.dumps(linha, ensure_ascii=True, default=str), file=sys.stderr, flush=True)


def silenciar_ruido() -> None:
    """Silencia o ruido de OpenTelemetry/asyncio que o a2a-sdk deixa no stderr (spike S2).

    `Failed to detach context ... was created in a different Context` e inofensivo para o protocolo,
    mas polui o stderr estruturado.
    """
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)
    logging.getLogger("a2a").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)


_MAX_TEXTO = 128
# Acima disto o corpo nao e parseado para o log (mas segue intacto para o SDK).
_MAX_CORPO_LOG = 256 * 1024


def _texto(valor: object) -> str | None:
    return valor[:_MAX_TEXTO] if isinstance(valor, str) else None


def campos_do_request(corpo: bytes, cabecalhos: dict[str, str]) -> dict[str, Any]:
    """Do request A2A so o que pode ser logado: metodo JSON-RPC, id, trace-id, A2A-Version.

    Nunca o texto da mensagem, e nunca `requestState` (o agente nem o recebe do cliente).
    """
    campos: dict[str, Any] = {
        "method": None,
        "id": None,
        "trace_id": trace_id_de(cabecalhos.get("traceparent")),
        "a2a_version": _texto(cabecalhos.get("a2a-version")),
    }
    if not corpo or len(corpo) > _MAX_CORPO_LOG:
        return campos
    try:
        msg = json.loads(corpo)
    except (ValueError, RecursionError):  # RecursionError: JSON muito aninhado (100 KB de "[")
        return campos
    if not isinstance(msg, dict):
        return campos
    campos["method"] = _texto(msg.get("method"))
    ident = msg.get("id")
    if isinstance(ident, str):
        campos["id"] = ident[:_MAX_TEXTO]
    elif isinstance(ident, int) and not isinstance(ident, bool):
        campos["id"] = ident
    return campos


class RegistroDeRequests:
    """Wrapper ASGI: 1 linha JSON em stderr por request HTTP, corpo repassado intacto."""

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
            ):  # a app falhou antes de responder: o request foi recebido mesmo assim
                emitir("request", status=None, **campos)
