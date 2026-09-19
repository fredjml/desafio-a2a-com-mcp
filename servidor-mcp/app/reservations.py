"""Agenda de reservas em memoria (nao persiste; reservas.json so semeia o boot)."""

from __future__ import annotations

from collections.abc import Iterable

from .availability import conflitos_em
from .data import Reserva
from .policy import Intervalo


class Agenda:
    """Lista em memoria. Acessada so pelo event loop (tools async sem await): sem corrida de escrita.

    Concorrencia de escrita esta fora de escopo no enunciado; ainda assim check-then-insert nunca
    cede o controle ao loop, entao duas chamadas nao se intercalam no meio.
    """

    def __init__(self, iniciais: Iterable[Reserva] = ()) -> None:
        self._reservas: list[Reserva] = list(iniciais)

    @property
    def reservas(self) -> tuple[Reserva, ...]:
        return tuple(self._reservas)

    def conflitos(self, sala: str, intervalo: Intervalo) -> list[Reserva]:
        return conflitos_em(self._reservas, sala, intervalo.inicio, intervalo.fim)
