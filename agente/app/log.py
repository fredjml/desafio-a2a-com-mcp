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
    print(json.dumps(linha, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


def silenciar_ruido() -> None:
    """Silencia o ruido de OpenTelemetry/asyncio que o a2a-sdk deixa no stderr (spike S2).

    `Failed to detach context ... was created in a different Context` e inofensivo para o protocolo,
    mas polui o stderr estruturado.
    """
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)
    logging.getLogger("a2a").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
