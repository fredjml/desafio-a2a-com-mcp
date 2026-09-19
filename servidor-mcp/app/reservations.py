"""Agenda de reservas em memoria (nao persiste; reservas.json so semeia o boot)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .availability import conflitos_em
from .data import Reserva
from .policy import Intervalo

_ID = re.compile(r"res-([0-9]+)")


class Agenda:
    """Lista em memoria. Acessada so pelo event loop (tools async sem await): sem corrida de escrita.

    Concorrencia de escrita esta fora de escopo no enunciado; ainda assim check-then-insert nunca
    cede o controle ao loop, entao duas chamadas nao se intercalam no meio.
    """

    def __init__(self, iniciais: Iterable[Reserva] = ()) -> None:
        self._reservas: list[Reserva] = list(iniciais)
        # proximo id = maior numero ja usado + 1 (as duas do seed dao res-0003 na primeira criada)
        usados = [int(m.group(1)) for r in self._reservas if (m := _ID.fullmatch(r.id))]
        self._proximo = max(usados, default=0) + 1

    @property
    def reservas(self) -> tuple[Reserva, ...]:
        return tuple(self._reservas)

    def conflitos(self, sala: str, intervalo: Intervalo) -> list[Reserva]:
        return conflitos_em(self._reservas, sala, intervalo.inicio, intervalo.fim)

    def criar(self, sala: str, intervalo: Intervalo, responsavel: str) -> Reserva:
        """Grava a reserva (o chamador ja validou politica e ausencia de conflito). Ids res-0003, ..."""
        reserva = Reserva(
            f"res-{self._proximo:04d}", sala, intervalo.inicio, intervalo.fim, responsavel
        )
        self._proximo += 1
        self._reservas.append(reserva)
        return reserva
