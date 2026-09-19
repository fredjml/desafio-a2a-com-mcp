"""Unit: sobreposicao semiaberta [inicio, fim) e agenda em memoria."""

from __future__ import annotations

import pytest

from app.availability import conflitos_em, sobrepoe
from app.data import Reserva
from app.policy import Intervalo
from app.reservations import Agenda
from app.tempo import interpretar_instante


def reserva(id_: str, sala: str, ini: str, fim: str, quem: str = "X") -> Reserva:
    return Reserva(
        id_,
        sala,
        interpretar_instante(f"2026-11-03T{ini}:00-03:00"),
        interpretar_instante(f"2026-11-03T{fim}:00-03:00"),
        quem,
    )


def iv(ini: str, fim: str) -> Intervalo:
    return Intervalo(
        interpretar_instante(f"2026-11-03T{ini}:00-03:00"),
        interpretar_instante(f"2026-11-03T{fim}:00-03:00"),
    )


@pytest.mark.parametrize(
    ("b_ini", "b_fim", "esperado"),
    [
        ("14:00", "15:00", True),  # identico
        ("14:30", "15:30", True),  # sobreposicao parcial
        ("13:00", "14:30", True),
        ("14:15", "14:45", True),  # contido
        ("13:00", "16:00", True),  # contem
        ("15:00", "16:00", False),  # fim == inicio do outro: NAO conflita
        ("13:00", "14:00", False),  # inicio == fim do outro: NAO conflita
        ("16:00", "17:00", False),
        ("10:00", "11:00", False),
    ],
)
def test_sobreposicao_semiaberta(b_ini: str, b_fim: str, esperado: bool) -> None:
    a = iv("14:00", "15:00")
    b = iv(b_ini, b_fim)
    assert sobrepoe(a.inicio, a.fim, b.inicio, b.fim) is esperado
    assert sobrepoe(b.inicio, b.fim, a.inicio, a.fim) is esperado  # simetrica


def test_conflitos_so_da_mesma_sala_e_em_ordem_estavel() -> None:
    reservas = [
        reserva("res-0003", "sala-a", "14:30", "15:30"),
        reserva("res-0001", "sala-a", "14:00", "15:00"),
        reserva("res-0002", "sala-b", "14:00", "15:00"),
        reserva("res-0004", "sala-a", "15:00", "16:00"),  # encosta, nao conflita
    ]
    achadas = conflitos_em(
        reservas, "sala-a", iv("14:00", "15:00").inicio, iv("14:00", "15:00").fim
    )
    assert [r.id for r in achadas] == ["res-0001", "res-0003"]


def test_agenda_consulta_reservas_iniciais() -> None:
    agenda = Agenda([reserva("res-0001", "sala-a", "14:00", "15:00", "Marty")])
    assert [r.responsavel for r in agenda.conflitos("sala-a", iv("14:30", "15:30"))] == ["Marty"]
    assert agenda.conflitos("sala-a", iv("15:00", "16:00")) == []
    assert agenda.conflitos("sala-b", iv("14:00", "15:00")) == []
