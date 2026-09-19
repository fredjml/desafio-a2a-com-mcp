"""Integracao E4a (processo REAL): conflito em reservar_sala => input_required (T-02 integrado, T-03)."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from .mrtr_util import (
    MSG_ELICITATION,
    MSG_SEM_ALT,
    args,
    chamar,
    consultar,
    ocupar,
    pedir,
    reservas_em,
    wire_json,
)
from .servidor_proc import Servidor, montar_meta, texto_de

APP = Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------- conflito com alternativa (AC-09, AC-10)
def test_conflito_devolve_input_required_com_uma_elicitation_form(fresco: Servidor) -> None:
    resp = chamar(fresco, args("sala-garagem", "14:00", "15:00"))
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["resultType"] == "input_required"
    assert len(res["inputRequests"]) == 1  # check 13
    (chave,) = res["inputRequests"]
    assert isinstance(chave, str) and chave  # atribuida pelo SDK; o cliente devolve a mesma
    pedido = res["inputRequests"][chave]
    assert pedido["method"] == "elicitation/create"
    assert pedido["params"]["mode"] == "form"
    assert pedido["params"]["message"] == MSG_ELICITATION
    assert isinstance(res["requestState"], str) and res["requestState"].startswith("v1.")
    # conflito NAO e erro de execucao: nada de content/isError/structuredContent
    assert not {"content", "isError", "structuredContent"} & set(res)


def test_requested_schema_e_plano_com_a_ordem_das_alternativas(fresco: Servidor) -> None:
    res = chamar(fresco, args("sala-garagem", "14:00", "15:00")).json()["result"]
    (pedido,) = res["inputRequests"].values()
    schema = pedido["params"]["requestedSchema"]
    assert schema["type"] == "object" and schema["required"] == ["sala"]
    assert set(schema) == {"type", "properties", "required"}  # plano: sem $defs/$ref/allOf
    assert list(schema["properties"]) == ["sala"]
    campo = schema["properties"]["sala"]
    assert campo["type"] == "string"
    assert campo["enum"] == ["sala-fusca", "sala-mirante"]  # check 14: cap asc, id asc
    assert "$ref" not in str(schema) and "anyOf" not in str(schema)


def test_resposta_de_conflito_e_igual_ao_wire_03_exceto_chave_e_estado(fresco: Servidor) -> None:
    wire = wire_json("03-tools-call-conflito-input-required.json")
    corpo = wire["request"]["body"]
    resp = chamar(fresco, corpo["params"]["arguments"], id_=corpo["id"])
    assert resp.status_code == wire["response"]["httpStatus"] == 200
    obtido = copy.deepcopy(resp.json())
    esperado = copy.deepcopy(wire["response"]["body"])
    (k_obtida,) = obtido["result"]["inputRequests"]
    (k_wire,) = esperado["result"]["inputRequests"]
    obtido["result"]["inputRequests"] = {"K": obtido["result"]["inputRequests"][k_obtida]}
    esperado["result"]["inputRequests"] = {"K": esperado["result"]["inputRequests"][k_wire]}
    assert obtido["result"].pop("requestState").startswith("v1.")
    esperado["result"].pop("requestState")
    assert obtido == esperado  # inclusive a ordem/forma de description, enum, title, type


def test_conflito_nao_cria_reserva(fresco: Servidor) -> None:
    chamar(fresco, args("sala-garagem", "14:00", "15:00"))
    assert reservas_em(fresco, "sala-garagem", "14:00", "15:00") == ["res-0001"]
    for sala in ("sala-fusca", "sala-mirante"):
        assert reservas_em(fresco, sala, "14:00", "15:00") == []
    ocupar(fresco, "sala-aquario", "09:00", "10:00")  # a proxima reserva ainda e res-0003
    assert reservas_em(fresco, "sala-aquario", "09:00", "10:00") == ["res-0003"]


def test_sobreposicao_parcial_tambem_e_conflito(fresco: Servidor) -> None:
    _, _, alternativas = pedir(fresco, args("sala-garagem", "14:30", "15:30"))
    assert alternativas == ["sala-fusca", "sala-mirante"]


def test_encostada_nao_e_conflito(fresco: Servidor) -> None:
    res = chamar(fresco, args("sala-garagem", "15:00", "16:00")).json()["result"]
    assert res["resultType"] == "complete" and res["isError"] is False


# ---------------------------------------------------------------- alternativas (T-02 integrado)
def test_alternativa_ocupada_no_intervalo_nao_e_oferecida(fresco: Servidor) -> None:
    ocupar(fresco, "sala-fusca", "14:00", "15:00")
    _, _, alternativas = pedir(fresco, args("sala-garagem", "14:00", "15:00"))
    assert alternativas == ["sala-mirante"]


def test_uma_so_alternativa_vira_const(fresco: Servidor) -> None:
    ocupar(fresco, "sala-fusca", "14:00", "15:00")
    res = chamar(fresco, args("sala-garagem", "14:00", "15:00")).json()["result"]
    (pedido,) = res["inputRequests"].values()
    campo = pedido["params"]["requestedSchema"]["properties"]["sala"]
    assert campo["const"] == "sala-mirante" and campo["type"] == "string"
    assert "enum" not in campo


def test_maximo_de_tres_e_empate_por_capacidade_e_id(fresco: Servidor) -> None:
    ocupar(fresco, "sala-aquario", "09:00", "10:00")
    _, _, alternativas = pedir(fresco, args("sala-aquario", "09:00", "10:00"))
    assert alternativas == ["sala-porao", "sala-fusca", "sala-garagem"]  # mirante (4a) fica de fora


def test_capacidade_menor_nunca_e_oferecida(fresco: Servidor) -> None:
    ocupar(fresco, "sala-porao", "09:00", "10:00")  # 6 lugares
    _, _, alternativas = pedir(fresco, args("sala-porao", "09:00", "10:00"))
    assert "sala-aquario" not in alternativas and "sala-porao" not in alternativas
    assert alternativas == ["sala-fusca", "sala-garagem", "sala-mirante"]


def test_conflito_no_fuso_z_usa_o_mesmo_intervalo(fresco: Servidor) -> None:
    corpo = args("sala-garagem", "14:00", "15:00")
    corpo["inicio"], corpo["fim"] = "2026-11-03T17:00:00Z", "2026-11-03T18:00:00Z"  # = 14-15 -03:00
    res = chamar(fresco, corpo).json()["result"]
    assert res["resultType"] == "input_required"


# ---------------------------------------------------------------- sem alternativa (T-03, check 20)
def test_sem_alternativa_devolve_iserror_exato_sem_elicitation(fresco: Servidor) -> None:
    ocupar(fresco, "sala-mirante", "11:00", "12:00")  # 20 lugares: nada maior existe
    resp = chamar(fresco, args("sala-mirante", "11:00", "12:00"))
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["resultType"] == "complete" and res["isError"] is True
    assert res["content"] == [{"type": "text", "text": MSG_SEM_ALT}]  # igual, sem prefixo do SDK
    assert not {"inputRequests", "requestState", "structuredContent"} & set(res)


def test_todas_as_maiores_ocupadas_tambem_e_sem_alternativa(fresco: Servidor) -> None:
    ocupar(fresco, "sala-fusca", "14:00", "15:00")
    ocupar(fresco, "sala-mirante", "14:00", "15:00")
    res = chamar(fresco, args("sala-garagem", "14:00", "15:00")).json()["result"]
    assert res["isError"] is True and texto_de(res) == MSG_SEM_ALT
    assert "inputRequests" not in res and "requestState" not in res


def test_sem_alternativa_nao_exige_a_capability_de_elicitation(fresco: Servidor) -> None:
    """Sem elicitation nao ha o que exigir: cliente sem a capability recebe o isError, nao -32021."""
    ocupar(fresco, "sala-mirante", "11:00", "12:00")
    resp = chamar(fresco, args("sala-mirante", "11:00", "12:00"), meta=montar_meta(caps={}))
    assert resp.status_code == 200
    assert texto_de(resp.json()["result"]) == MSG_SEM_ALT


def test_sem_alternativa_nao_cria_reserva(fresco: Servidor) -> None:
    ocupar(fresco, "sala-mirante", "11:00", "12:00")
    chamar(fresco, args("sala-mirante", "11:00", "12:00", "Outro"))
    assert len(consultar(fresco, "sala-mirante", "11:00", "12:00")["conflitos"]) == 1


# ---------------------------------------------------------------- contrato do tools/list e validacoes
def test_input_schema_da_reserva_nao_expoe_o_parametro_do_resolvedor(servidor: Servidor) -> None:
    tools = {t["name"]: t for t in servidor.rpc("tools/list").json()["result"]["tools"]}
    entrada = tools["reservar_sala"]["inputSchema"]
    assert list(entrada["properties"]) == ["sala", "inicio", "fim", "responsavel"]
    assert entrada["required"] == ["sala", "inicio", "fim", "responsavel"]
    assert "escolha" not in str(tools["reservar_sala"])  # nem no input nem no output schema


@pytest.mark.parametrize(
    ("sala", "inicio", "fim", "esperado"),
    [
        ("sala-delorean", "09:00", "10:00", "Sala inexistente: sala-delorean"),
        (
            "sala-garagem",
            "07:00",
            "08:00",
            "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00",
        ),
        (
            "sala-garagem",
            "09:00",
            "12:00",
            "Duracao acima do limite: a politica permite no maximo 2 horas",
        ),
        ("sala-garagem", "10:00", "09:00", "Intervalo invalido: fim deve ser posterior a inicio"),
    ],
)
def test_validacao_da_politica_vem_antes_do_conflito(
    fresco: Servidor, sala: str, inicio: str, fim: str, esperado: str
) -> None:
    res = chamar(fresco, args(sala, inicio, fim)).json()["result"]
    assert res["isError"] is True and res["content"] == [{"type": "text", "text": esperado}]
    assert "inputRequests" not in res


def test_sala_livre_continua_reservando_direto(fresco: Servidor) -> None:
    res = chamar(fresco, args("sala-porao", "09:00", "10:00")).json()["result"]
    assert res["resultType"] == "complete" and res["structuredContent"]["reservado"] is True
    assert "inputRequests" not in res


# ---------------------------------------------------------------- estatico: sem restos provisorios
def test_app_sem_marcadores_provisorios_e_sem_input_required_manual() -> None:
    for arq in APP.glob("*.py"):
        texto = arq.read_text(encoding="utf-8")
        assert "PROVISORIO" not in texto and "TODO(E4a)" not in texto, arq.name
    # DEC-24: nunca misturar InputRequiredResult manual com Elicit/Resolve no mesmo tool
    for arq in APP.glob("*.py"):
        assert not re.search(r"import .*InputRequiredResult", arq.read_text(encoding="utf-8")), (
            arq.name
        )
