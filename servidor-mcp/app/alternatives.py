"""Alternativas de sala para um pedido em conflito (R-MRTR-02): unica fonte da regra.

Uma alternativa e uma sala que (1) nao e a pedida, (2) esta livre no intervalo, (3) tem capacidade
maior ou igual a da pedida. Ordem: capacidade crescente, empate por id. No maximo `MAXIMO_ALTERNATIVAS`.
"""

from __future__ import annotations

from collections.abc import Iterable

from .availability import conflitos_em
from .data import Reserva, Sala
from .policy import Intervalo

MAXIMO_ALTERNATIVAS = 3


def alternativas(
    salas: Iterable[Sala],
    reservas: Iterable[Reserva],
    pedida: str,
    intervalo: Intervalo,
    maximo: int = MAXIMO_ALTERNATIVAS,
) -> list[Sala]:
    """Salas oferecidas no lugar de `pedida`. Lista vazia = nao ha alternativa (sem elicitation)."""
    todas = list(salas)
    reservadas = list(reservas)
    alvo = next((s for s in todas if s.id == pedida), None)
    if alvo is None:
        return []
    livres = [
        s
        for s in todas
        if s.id != pedida
        and s.capacidade >= alvo.capacidade
        and not conflitos_em(reservadas, s.id, intervalo.inicio, intervalo.fim)
    ]
    livres.sort(key=lambda s: (s.capacidade, s.id))
    return livres[:maximo]
