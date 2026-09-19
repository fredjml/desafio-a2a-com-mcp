"""Unit (T-02): regra das alternativas. Fixture com as 5 salas do enunciado + variacoes."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.alternatives import MAXIMO_ALTERNATIVAS, alternativas
from app.data import Reserva, Sala
from app.policy import Intervalo
from app.tempo import interpretar_instante

SALAS = [
    Sala("sala-aquario", "Aquario", 4, ("tv",)),
    Sala("sala-porao", "Porao", 6, ("quadro",)),
    Sala("sala-garagem", "Garagem", 12, ("tv", "quadro")),
    Sala("sala-fusca", "Fusca", 12, ("tv",)),
    Sala("sala-mirante", "Mirante", 20, ("tv", "quadro", "camera")),
]


def t(hora: str) -> datetime:
    return interpretar_instante(f"2026-11-03T{hora}:00-03:00")


def reserva(id_: str, sala: str, ini: str, fim: str) -> Reserva:
    return Reserva(id_, sala, t(ini), t(fim), "Alguem")


def ids(lista: list[Sala]) -> list[str]:
    return [s.id for s in lista]


def oferta(
    pedida: str, reservas: list[Reserva], ini: str = "14:00", fim: str = "15:00"
) -> list[str]:
    return ids(alternativas(SALAS, reservas, pedida, Intervalo(t(ini), t(fim))))


def test_garagem_ocupada_14_15_oferece_fusca_e_mirante_nessa_ordem() -> None:
    seed = [reserva("res-0001", "sala-garagem", "14:00", "15:00")]
    assert oferta("sala-garagem", seed) == ["sala-fusca", "sala-mirante"]  # check 14


def test_capacidade_menor_que_a_pedida_nunca_aparece() -> None:
    seed = [reserva("res-0001", "sala-garagem", "14:00", "15:00")]
    ofertadas = oferta("sala-garagem", seed)
    assert "sala-aquario" not in ofertadas and "sala-porao" not in ofertadas


def test_capacidade_igual_e_aceita_e_a_pedida_nunca_e_oferecida() -> None:
    seed = [reserva("res-0001", "sala-garagem", "14:00", "15:00")]
    ofertadas = oferta("sala-garagem", seed)
    assert "sala-fusca" in ofertadas  # 12 == 12
    assert "sala-garagem" not in ofertadas


def test_maximo_de_tres_capacidade_asc_e_desempate_por_id() -> None:
    seed = [reserva("res-0001", "sala-aquario", "14:00", "15:00")]
    # livres >= 4: porao(6), fusca(12), garagem(12), mirante(20) -> corta em 3
    assert oferta("sala-aquario", seed) == ["sala-porao", "sala-fusca", "sala-garagem"]
    assert MAXIMO_ALTERNATIVAS == 3


def test_empate_de_capacidade_e_resolvido_por_id_alfabetico() -> None:
    assert oferta("sala-mirante", [reserva("r", "sala-mirante", "14:00", "15:00")]) == []
    salas = [
        Sala("b", "B", 10, ()),
        Sala("a", "A", 10, ()),
        Sala("z", "Z", 10, ()),
        Sala("p", "P", 10, ()),
    ]
    r = [reserva("r", "p", "14:00", "15:00")]
    obtido = ids(alternativas(salas, r, "p", Intervalo(t("14:00"), t("15:00"))))
    assert obtido == ["a", "b", "z"]


def test_sala_ocupada_no_intervalo_nao_e_alternativa() -> None:
    reservas = [
        reserva("r1", "sala-garagem", "14:00", "15:00"),
        reserva("r2", "sala-fusca", "14:30", "15:30"),  # sobreposicao parcial tambem ocupa
    ]
    assert oferta("sala-garagem", reservas) == ["sala-mirante"]


def test_reserva_encostada_nao_ocupa_intervalo_semiaberto() -> None:
    reservas = [
        reserva("r1", "sala-garagem", "14:00", "15:00"),
        reserva("r2", "sala-fusca", "15:00", "16:00"),  # fim do pedido == inicio dela
        reserva("r3", "sala-mirante", "13:00", "14:00"),  # fim dela == inicio do pedido
    ]
    assert oferta("sala-garagem", reservas) == ["sala-fusca", "sala-mirante"]


def test_uma_alternativa_devolve_lista_de_um() -> None:
    reservas = [
        reserva("r1", "sala-garagem", "14:00", "15:00"),
        reserva("r2", "sala-fusca", "14:00", "15:00"),
    ]
    assert oferta("sala-garagem", reservas) == ["sala-mirante"]


def test_nenhuma_alternativa_devolve_lista_vazia() -> None:
    assert oferta("sala-mirante", [reserva("r", "sala-mirante", "14:00", "15:00")]) == []
    todas_ocupadas = [
        reserva("r1", "sala-garagem", "14:00", "15:00"),
        reserva("r2", "sala-fusca", "14:00", "15:00"),
        reserva("r3", "sala-mirante", "14:00", "15:00"),
    ]
    assert oferta("sala-garagem", todas_ocupadas) == []


def test_reserva_em_outro_dia_nao_ocupa() -> None:
    outro_dia = Reserva(
        "r9",
        "sala-fusca",
        interpretar_instante("2026-11-04T14:00:00-03:00"),
        interpretar_instante("2026-11-04T15:00:00-03:00"),
        "X",
    )
    reservas = [reserva("r1", "sala-garagem", "14:00", "15:00"), outro_dia]
    assert oferta("sala-garagem", reservas) == ["sala-fusca", "sala-mirante"]


def test_sala_pedida_desconhecida_nao_tem_alternativa() -> None:
    assert oferta("sala-delorean", []) == []


@pytest.mark.parametrize("maximo", [1, 2])
def test_maximo_configuravel(maximo: int) -> None:
    seed = [reserva("res-0001", "sala-aquario", "14:00", "15:00")]
    obtido = alternativas(SALAS, seed, "sala-aquario", Intervalo(t("14:00"), t("15:00")), maximo)
    assert len(obtido) == maximo


def test_nao_altera_as_entradas() -> None:
    reservas = [reserva("res-0001", "sala-garagem", "14:00", "15:00")]
    antes = list(SALAS), list(reservas)
    oferta("sala-garagem", reservas)
    assert (SALAS, reservas) == antes
