"""W3C Trace Context: valida o `traceparent` do A2A e monta os do MCP (mesmo trace-id, span novo).

R-HOST-03 / DEC-19: o trace-id de uma Task e fixado no 1o pedido e reusado em todos os requests MCP
dela; cada request MCP leva um span-id novo. Header invalido e ignorado (gera-se um trace-id novo).
"""

from __future__ import annotations

import re
import secrets

_TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_TRACE_ID = re.compile(r"[0-9a-f]{32}")


def trace_id_de(valor: object) -> str | None:
    """Trace-id de um `traceparent` valido (W3C nivel 1, minusculo, ids nao-zero); senao None."""
    if not isinstance(valor, str) or not _TRACEPARENT.fullmatch(valor):
        return None
    versao, trace_id, parent_id, _ = valor.split("-")
    if versao == "ff" or trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return trace_id


def trace_id_valido(valor: object) -> bool:
    return isinstance(valor, str) and _TRACE_ID.fullmatch(valor) is not None and valor != "0" * 32


def novo_trace_id() -> str:
    while True:
        candidato = secrets.token_hex(16)
        if candidato != "0" * 32:
            return candidato


def traceparent_para(trace_id: str) -> str:
    """`traceparent` com o trace-id dado e um span-id novo (flags 01 = amostrado)."""
    if not trace_id_valido(trace_id):
        raise ValueError("trace-id invalido")
    while True:
        span = secrets.token_hex(8)
        if span != "0" * 16:
            return f"00-{trace_id}-{span}-01"


def trace_da_task(guardado: str, header: object) -> tuple[str, bool]:
    """DEC-19: o trace-id da Task e o fixado no 1o pedido; um `traceparent` novo na continuacao NAO o troca.

    Devolve `(trace_id_da_task, header_ignorado)`; `header_ignorado` e True quando o header era um
    `traceparent` valido com OUTRO trace-id (so para o log). Header ausente/invalido: usa o guardado.
    """
    novo = trace_id_de(header)
    return guardado, novo is not None and novo != guardado
