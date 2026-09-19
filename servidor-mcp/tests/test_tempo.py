"""Unit: parse de instantes ISO 8601 proprio (compativel com 3.10). T-32 (parte de formato)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.tempo import FUSO_SP, FormatoInvalido, formatar_instante, interpretar_instante


def test_instante_com_offset_menos_tres_e_preservado() -> None:
    i = interpretar_instante("2026-11-03T14:00:00-03:00")
    assert i.utcoffset() == timedelta(hours=-3)
    assert formatar_instante(i) == "2026-11-03T14:00:00-03:00"


def test_z_e_aceito_e_normalizado_para_menos_tres() -> None:
    i = interpretar_instante("2026-11-03T17:00:00Z")
    assert i == interpretar_instante("2026-11-03T14:00:00-03:00")
    assert formatar_instante(i) == "2026-11-03T14:00:00-03:00"


def test_outro_offset_e_normalizado() -> None:
    i = interpretar_instante("2026-11-03T19:00:00+02:00")  # 17:00Z = 14:00-03:00
    assert formatar_instante(i) == "2026-11-03T14:00:00-03:00"
    assert i.tzinfo == FUSO_SP


def test_segundos_sao_opcionais_e_fracao_e_aceita() -> None:
    assert interpretar_instante("2026-11-03T14:00-03:00") == interpretar_instante(
        "2026-11-03T14:00:00-03:00"
    )
    assert interpretar_instante("2026-11-03T14:00:00.5-03:00").microsecond == 500000
    assert interpretar_instante("2026-11-03T14:00:00.123456789-03:00").microsecond == 123456


def test_virada_de_dia_na_normalizacao() -> None:
    i = interpretar_instante("2026-11-04T01:30:00Z")  # 22:30 do dia 03 em -03:00
    assert formatar_instante(i) == "2026-11-03T22:30:00-03:00"


@pytest.mark.parametrize(
    "texto",
    [
        "",
        "lixo",
        "2026-11-03T14:00:00",  # sem fuso
        "2026-11-03 14:00:00-03:00",  # separador que nao e T
        "2026-11-03T14:00:00-0300",  # offset sem dois pontos
        "2026-11-03T14:00:00+03",
        "2026-11-03",
        "2026-13-03T14:00:00-03:00",  # mes 13
        "2026-02-30T14:00:00-03:00",  # dia inexistente
        "2026-11-03T24:00:00-03:00",  # hora 24
        "2026-11-03T14:60:00-03:00",
        "2026-11-03T14:00:60-03:00",
        "2026-11-03T14:00:00+24:00",
        "2026-11-03T14:00:00+03:99",
        "2026-11-03T14:00:00z",  # z minusculo
        "2026-11-03T14:00:00-03:00\n",  # newline no fim (fullmatch, nao match/$)
        " 2026-11-03T14:00:00-03:00",
        "٢٠٢٦-11-03T14:00:00-03:00",  # digitos arabe-indicos
        "0001-01-01T00:00:00+23:59",  # estouraria a conversao
    ],
)
def test_formatos_invalidos(texto: str) -> None:
    with pytest.raises(FormatoInvalido) as exc:
        interpretar_instante(texto)
    assert str(exc.value) == "Formato invalido: inicio e fim devem ser ISO 8601 com fuso"


def test_tipo_nao_string_e_formato_invalido() -> None:
    with pytest.raises(FormatoInvalido):
        interpretar_instante(123)  # type: ignore[arg-type]
