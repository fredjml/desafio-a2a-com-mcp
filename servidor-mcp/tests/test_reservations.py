"""Unit: agenda em memoria e ids sequenciais (T-24)."""

from __future__ import annotations

from app.data import Reserva
from app.policy import Intervalo
from app.reservations import Agenda
from app.tempo import formatar_instante, interpretar_instante


def iv(ini: str, fim: str) -> Intervalo:
    return Intervalo(
        interpretar_instante(f"2026-11-03T{ini}:00-03:00"),
        interpretar_instante(f"2026-11-03T{fim}:00-03:00"),
    )


def seed(id_: str) -> Reserva:
    i = iv("14:00", "15:00")
    return Reserva(id_, "sala-garagem", i.inicio, i.fim, "Marty")


def test_ids_continuam_depois_das_reservas_do_seed() -> None:
    agenda = Agenda([seed("res-0001"), seed("res-0002")])
    a = agenda.criar("sala-aquario", iv("09:00", "10:00"), "Doc")
    b = agenda.criar("sala-aquario", iv("10:00", "11:00"), "Doc")
    assert (a.id, b.id) == ("res-0003", "res-0004")


def test_ids_partem_de_res_0001_sem_seed() -> None:
    assert Agenda().criar("sala-a", iv("09:00", "10:00"), "X").id == "res-0001"


def test_ids_respeitam_lacunas_e_ids_fora_do_padrao_do_seed() -> None:
    agenda = Agenda([seed("res-0007"), seed("outro-formato")])
    assert agenda.criar("sala-a", iv("09:00", "10:00"), "X").id == "res-0008"


def test_reserva_criada_fica_visivel_para_a_consulta_seguinte() -> None:
    agenda = Agenda()
    assert agenda.conflitos("sala-a", iv("09:30", "10:30")) == []
    criada = agenda.criar("sala-a", iv("09:00", "10:00"), "Doc")
    assert agenda.conflitos("sala-a", iv("09:30", "10:30")) == [criada]
    assert agenda.conflitos("sala-a", iv("10:00", "11:00")) == []  # semiaberto
    assert agenda.conflitos("sala-b", iv("09:30", "10:30")) == []
    assert formatar_instante(criada.inicio) == "2026-11-03T09:00:00-03:00"


def test_reservas_expoe_copia_imutavel() -> None:
    agenda = Agenda([seed("res-0001")])
    assert isinstance(agenda.reservas, tuple) and len(agenda.reservas) == 1
