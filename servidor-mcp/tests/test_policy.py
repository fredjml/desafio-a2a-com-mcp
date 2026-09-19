"""Unit: politica. T-01 (mensagens EXATAS por igualdade) e T-32 (janela, duracao, fim==inicio, fusos)."""

from __future__ import annotations

import pytest

from app import policy
from app.policy import ErroDeDominio, Intervalo, validar_pedido
from app.tempo import formatar_instante

SALAS = frozenset({"sala-aquario", "sala-porao", "sala-garagem", "sala-fusca", "sala-mirante"})
DIA = "2026-11-03"


def h(hora: str, fuso: str = "-03:00", dia: str = DIA) -> str:
    return f"{dia}T{hora}:00{fuso}"


def erro_de(sala: str, inicio: str, fim: str) -> str:
    with pytest.raises(ErroDeDominio) as exc:
        validar_pedido(sala, inicio, fim, SALAS)
    return exc.value.mensagem


# ---- T-01: as 5 mensagens do enunciado, literais (nao derivadas das constantes do codigo)
def test_mensagens_exatas_do_enunciado_por_igualdade() -> None:
    assert policy.msg_sala_inexistente("sala-delorean") == "Sala inexistente: sala-delorean"
    assert (
        policy.MSG_JANELA
        == "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    )
    assert policy.MSG_DURACAO == "Duracao acima do limite: a politica permite no maximo 2 horas"
    assert policy.MSG_INTERVALO == "Intervalo invalido: fim deve ser posterior a inicio"
    assert policy.MSG_SEM_ALTERNATIVAS == "Sem alternativas disponiveis no intervalo"
    assert policy.MSG_FORMATO == "Formato invalido: inicio e fim devem ser ISO 8601 com fuso"


def test_cada_regra_devolve_a_sua_mensagem_exata() -> None:
    assert erro_de("sala-delorean", h("09:00"), h("10:00")) == "Sala inexistente: sala-delorean"
    assert (
        erro_de("sala-aquario", h("07:00"), h("08:00"))
        == "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    )
    assert (
        erro_de("sala-aquario", h("09:00"), h("12:00"))
        == "Duracao acima do limite: a politica permite no maximo 2 horas"
    )
    assert (
        erro_de("sala-aquario", h("10:00"), h("09:00"))
        == "Intervalo invalido: fim deve ser posterior a inicio"
    )
    assert (
        erro_de("sala-aquario", "lixo", h("09:00"))
        == "Formato invalido: inicio e fim devem ser ISO 8601 com fuso"
    )


def test_sala_inexistente_ecoa_o_id_informado_literalmente() -> None:
    assert erro_de("Sala X", h("09:00"), h("10:00")) == "Sala inexistente: Sala X"
    assert erro_de("", h("09:00"), h("10:00")) == "Sala inexistente: "


# ---- ordem das validacoes: sala -> formato -> intervalo -> janela -> duracao
def test_sala_vem_antes_de_todas_as_outras() -> None:
    assert erro_de("nao-existe", "lixo", "lixo") == "Sala inexistente: nao-existe"
    assert erro_de("nao-existe", h("10:00"), h("09:00")) == "Sala inexistente: nao-existe"


def test_formato_vem_antes_de_intervalo_janela_e_duracao() -> None:
    assert erro_de("sala-aquario", "2026-11-03T10:00:00", h("09:00")) == policy.MSG_FORMATO
    assert erro_de("sala-aquario", h("09:00"), "amanha") == policy.MSG_FORMATO


def test_intervalo_invertido_vem_antes_da_janela_e_da_duracao() -> None:
    assert erro_de("sala-aquario", h("22:00"), h("21:00")) == policy.MSG_INTERVALO  # fora da janela
    assert erro_de("sala-aquario", h("15:00"), h("09:00")) == policy.MSG_INTERVALO  # 6 h invertidas


def test_janela_vem_antes_da_duracao() -> None:
    assert (
        erro_de("sala-aquario", h("07:00"), h("10:00")) == policy.MSG_JANELA
    )  # 3 h e fora da janela
    assert erro_de("sala-aquario", h("18:00"), h("21:00")) == policy.MSG_JANELA


# ---- intervalo: fim == inicio e vazio => invalido
def test_fim_igual_ao_inicio_e_intervalo_invalido() -> None:
    assert erro_de("sala-aquario", h("09:00"), h("09:00")) == policy.MSG_INTERVALO


# ---- janela inclusiva nas duas pontas (T-32)
@pytest.mark.parametrize(
    ("inicio", "fim"),
    [("08:00", "09:00"), ("19:00", "20:00"), ("08:00", "10:00"), ("18:00", "20:00")],
)
def test_pontas_da_janela_sao_aceitas(inicio: str, fim: str) -> None:
    intervalo = validar_pedido("sala-aquario", h(inicio), h(fim), SALAS)
    assert isinstance(intervalo, Intervalo)


@pytest.mark.parametrize(
    ("inicio", "fim"),
    [
        ("07:59", "08:59"),  # comeca um minuto antes
        ("19:01", "20:01"),  # termina um minuto depois
        ("07:00", "08:00"),  # check 10 do validador
        ("20:00", "21:00"),
        ("06:00", "07:00"),
        ("23:00", "23:59"),
    ],
)
def test_fora_da_janela(inicio: str, fim: str) -> None:
    assert erro_de("sala-aquario", h(inicio), h(fim)) == policy.MSG_JANELA


def test_fim_no_dia_seguinte_esta_fora_da_janela() -> None:
    assert erro_de("sala-aquario", h("19:00"), h("01:00", dia="2026-11-04")) == policy.MSG_JANELA


# ---- duracao <= 2 h
@pytest.mark.parametrize(
    ("inicio", "fim"), [("09:00", "11:00"), ("09:00", "10:00"), ("09:00", "09:01")]
)
def test_duracao_ate_duas_horas_e_aceita(inicio: str, fim: str) -> None:
    validar_pedido("sala-aquario", h(inicio), h(fim), SALAS)


@pytest.mark.parametrize(
    ("inicio", "fim"), [("09:00", "11:01"), ("09:00", "12:00"), ("08:00", "20:00")]
)
def test_duracao_acima_de_duas_horas(inicio: str, fim: str) -> None:
    assert erro_de("sala-aquario", h(inicio), h(fim)) == policy.MSG_DURACAO


# ---- fusos: a janela e avaliada DEPOIS de normalizar para -03:00 (T-32)
def test_z_normaliza_para_menos_tres_antes_da_janela() -> None:
    # 11:00Z = 08:00-03:00 (dentro); 10:59Z = 07:59-03:00 (fora)
    ok = validar_pedido("sala-aquario", "2026-11-03T11:00:00Z", "2026-11-03T12:00:00Z", SALAS)
    assert formatar_instante(ok.inicio) == "2026-11-03T08:00:00-03:00"
    assert (
        erro_de("sala-aquario", "2026-11-03T10:59:00Z", "2026-11-03T12:00:00Z") == policy.MSG_JANELA
    )


def test_outro_offset_normaliza_antes_da_janela() -> None:
    # 13:00+02:00 = 08:00-03:00 (dentro); 09:00+00:00 = 06:00-03:00 (fora)
    validar_pedido("sala-aquario", "2026-11-03T13:00:00+02:00", "2026-11-03T14:00:00+02:00", SALAS)
    assert (
        erro_de("sala-aquario", "2026-11-03T09:00:00+00:00", "2026-11-03T10:00:00+00:00")
        == policy.MSG_JANELA
    )


def test_instante_utc_que_vira_o_dia_em_sao_paulo() -> None:
    # 2026-11-04T01:00Z = 2026-11-03T22:00-03:00: fora da janela do dia 03 (nao "01:00 do dia 04")
    assert (
        erro_de("sala-aquario", "2026-11-04T01:00:00Z", "2026-11-04T02:00:00Z") == policy.MSG_JANELA
    )


def test_intervalo_devolvido_esta_em_menos_tres() -> None:
    i = validar_pedido("sala-aquario", "2026-11-03T12:00:00Z", "2026-11-03T13:00:00Z", SALAS)
    assert formatar_instante(i.inicio) == "2026-11-03T09:00:00-03:00"
    assert formatar_instante(i.fim) == "2026-11-03T10:00:00-03:00"


# ---- nenhuma regra depende da data de hoje
@pytest.mark.parametrize("dia", ["1999-01-01", "2026-11-03", "2099-12-31"])
def test_passado_e_futuro_sao_permitidos(dia: str) -> None:
    validar_pedido("sala-aquario", h("09:00", dia=dia), h("10:00", dia=dia), SALAS)


# ---- formatos ISO
@pytest.mark.parametrize(
    "texto",
    ["", "lixo", "2026-11-03T09:00:00", "2026-11-03 09:00:00-03:00", "09:00", "2026-11-03", "9h"],
)
def test_formato_invalido_no_inicio_ou_no_fim(texto: str) -> None:
    assert erro_de("sala-aquario", texto, h("10:00")) == policy.MSG_FORMATO
    assert erro_de("sala-aquario", h("09:00"), texto) == policy.MSG_FORMATO


def test_formatos_validos_com_z_e_sem_segundos() -> None:
    validar_pedido("sala-aquario", "2026-11-03T12:00Z", "2026-11-03T13:00Z", SALAS)
    validar_pedido("sala-aquario", "2026-11-03T09:00-03:00", "2026-11-03T10:00-03:00", SALAS)
