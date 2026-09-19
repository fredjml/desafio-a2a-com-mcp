"""Instantes ISO 8601 com fuso, sem depender de `datetime.fromisoformat` (piso Python 3.10).

`fromisoformat` so aceita 'Z' e formas livres a partir do 3.11; aqui o parser e proprio e igual em
qualquer versao. Decisao: 'Z' e aceito (e um designador de fuso ISO valido, UTC) e o instante e
normalizado para -03:00, o fuso da politica. Sem fuso, separador que nao seja 'T', digitos nao ASCII
ou lixo => FormatoInvalido.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

FUSO_SP = timezone(timedelta(hours=-3))
MSG_FORMATO = "Formato invalido: inicio e fim devem ser ISO 8601 com fuso"

# [0-9] explicito: \d casaria digitos Unicode de outros alfabetos.
_ISO = re.compile(
    r"(?P<ano>[0-9]{4})-(?P<mes>[0-9]{2})-(?P<dia>[0-9]{2})"
    r"T(?P<hora>[0-9]{2}):(?P<min>[0-9]{2})(?::(?P<seg>[0-9]{2})(?:\.(?P<frac>[0-9]{1,9}))?)?"
    r"(?:(?P<z>Z)|(?P<sinal>[+-])(?P<oh>[0-9]{2}):(?P<om>[0-9]{2}))"
)


class FormatoInvalido(ValueError):
    """Texto que nao e um instante ISO 8601 com fuso valido."""

    def __init__(self) -> None:
        super().__init__(MSG_FORMATO)


def interpretar_instante(texto: str) -> datetime:
    """Converte 'YYYY-MM-DDTHH:MM[:SS[.f]](Z|+HH:MM|-HH:MM)' em datetime normalizado para -03:00."""
    achado = _ISO.fullmatch(texto) if isinstance(texto, str) else None
    if achado is None:
        raise FormatoInvalido()
    g = achado.groupdict()
    try:
        if g["z"]:
            fuso = timezone.utc
        else:
            desloc = timedelta(hours=int(g["oh"]), minutes=int(g["om"]))
            if int(g["om"]) > 59:
                raise ValueError("minutos de fuso")
            fuso = timezone(-desloc if g["sinal"] == "-" else desloc)
        micro = int((g["frac"] or "0").ljust(6, "0")[:6])
        instante = datetime(
            int(g["ano"]),
            int(g["mes"]),
            int(g["dia"]),
            int(g["hora"]),
            int(g["min"]),
            int(g["seg"] or 0),
            micro,
            tzinfo=fuso,
        )
        return instante.astimezone(FUSO_SP)
    except (ValueError, OverflowError):
        raise FormatoInvalido() from None


def formatar_instante(instante: datetime) -> str:
    """ISO 8601 em -03:00, como nos wire (ex.: 2026-11-03T09:00:00-03:00)."""
    return instante.astimezone(FUSO_SP).isoformat()
