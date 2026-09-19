"""Disponibilidade: sobreposicao de intervalos SEMIABERTOS [inicio, fim)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .data import Reserva


def sobrepoe(a_inicio: datetime, a_fim: datetime, b_inicio: datetime, b_fim: datetime) -> bool:
    """Semiaberto: fim == inicio do outro NAO conflita (14:00-15:00 e 15:00-16:00 convivem)."""
    return a_inicio < b_fim and b_inicio < a_fim


def conflitos_em(
    reservas: Iterable[Reserva], sala: str, inicio: datetime, fim: datetime
) -> list[Reserva]:
    """Reservas da sala que se sobrepoem ao intervalo, em ordem estavel (inicio, fim, id)."""
    achadas = [r for r in reservas if r.sala == sala and sobrepoe(r.inicio, r.fim, inicio, fim)]
    return sorted(achadas, key=lambda r: (r.inicio, r.fim, r.id))
