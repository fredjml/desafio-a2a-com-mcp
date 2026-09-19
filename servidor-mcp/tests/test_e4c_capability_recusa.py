"""Integracao E4c (processo REAL): capability de elicitation (-32021, T-09) e decline/cancel (T-08).

Tambem cobre o multi-rodada: se as alternativas mudam entre rodadas o servidor pergunta de novo.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from .mrtr_util import (
    MSG_SEM_ALT,
    RECUSADO,
    REQUER_FORM,
    aceitar,
    args,
    chamar,
    consultar,
    erro_de,
    ocupar,
    pedir,
    reservas_em,
    retomar,
    wire_json,
)
from .servidor_proc import Servidor, montar_meta, texto_de

G = args("sala-garagem", "14:00", "15:00")

SEM_FORM: list[Any] = [
    pytest.param({}, id="vazio"),
    pytest.param({"elicitation": {}}, id="elicitation-vazio"),
    pytest.param({"elicitation": {"url": {}}}, id="so-url"),
    pytest.param({"elicitation": None}, id="elicitation-null"),
    pytest.param({"sampling": {}, "roots": {}}, id="outras-capabilities"),
]


def exigir_32021(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 400, (resp.status_code, resp.text[:300])
    erro = erro_de(resp)
    assert erro["code"] == -32021
    assert erro["data"] == {"requiredCapabilities": REQUER_FORM}  # exatamente isto
    return erro


# ---------------------------------------------------------------- T-09: sem elicitation.form (AC-11)
@pytest.mark.parametrize("caps", SEM_FORM)
def test_conflito_sem_elicitation_form_devolve_32021_http_400(
    fresco: Servidor, caps: dict[str, Any]
) -> None:
    resp = chamar(fresco, G, meta=montar_meta(caps=caps), id_="sem-caps-1")
    erro = exigir_32021(resp)
    assert resp.json()["id"] == "sem-caps-1" and "result" not in resp.json()
    assert "form elicitation" in erro["message"]
    # nenhuma reserva, nenhum estado emitido
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == []
    assert reservas_em(fresco, "sala-garagem", "14:00", "15:00") == ["res-0001"]


def test_resposta_e_igual_ao_wire_06_exceto_o_nome_do_resolvedor(fresco: Servidor) -> None:
    wire = wire_json("06-erro-32021-sem-elicitation.json")
    corpo = wire["request"]["body"]
    assert corpo["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {}
    resp = chamar(
        fresco,
        corpo["params"]["arguments"],
        meta=corpo["params"]["_meta"],
        id_=corpo["id"],
    )
    assert resp.status_code == wire["response"]["httpStatus"] == 400
    obtido = copy.deepcopy(resp.json())
    esperado = copy.deepcopy(wire["response"]["body"])
    prefixo = "Client did not declare the form elicitation capability required by resolver '"
    assert obtido["error"]["message"].startswith(prefixo)
    assert esperado["error"]["message"].startswith(prefixo)
    obtido["error"]["message"] = esperado["error"]["message"] = "MSG"
    assert obtido == esperado


@pytest.mark.parametrize("caps", SEM_FORM)
def test_sem_conflito_nao_exige_a_capability(fresco: Servidor, caps: dict[str, Any]) -> None:
    resp = chamar(fresco, args("sala-porao", "09:00", "10:00"), meta=montar_meta(caps=caps))
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["resultType"] == "complete" and res["structuredContent"]["reservado"] is True


@pytest.mark.parametrize(
    "caps",
    [
        {"elicitation": {"form": {}}},
        {"elicitation": {"form": {}, "url": {}}},  # o que o SDK cliente do agente declara (S1-d)
        {"elicitation": {"form": {}}, "sampling": {}, "roots": {}},
    ],
    ids=["form", "form+url", "form+outras"],
)
def test_form_declarado_recebe_a_elicitation(fresco: Servidor, caps: dict[str, Any]) -> None:
    res = chamar(fresco, G, meta=montar_meta(caps=caps)).json()["result"]
    assert res["resultType"] == "input_required"


def test_erro_de_dominio_vem_antes_da_capability(fresco: Servidor) -> None:
    resp = chamar(fresco, args("sala-delorean", "09:00", "10:00"), meta=montar_meta(caps={}))
    assert resp.status_code == 200
    assert texto_de(resp.json()["result"]) == "Sala inexistente: sala-delorean"


def test_retry_de_accept_tambem_exige_a_capability_em_cada_request(fresco: Servidor) -> None:
    """Sem inferir de request anterior (P0-9): o retry sem a capability e recusado e nao reserva."""
    chave, estado, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, aceitar("sala-fusca"), estado, meta=montar_meta(caps={}))
    exigir_32021(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []
    ok = retomar(
        fresco, G, chave, aceitar("sala-fusca"), estado
    )  # com a capability, o mesmo estado vale
    assert ok.json()["result"]["structuredContent"]["reservado"] is True


def test_decline_nao_depende_da_capability(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, {"action": "decline"}, estado, meta=montar_meta(caps={}))
    assert resp.status_code == 200 and resp.json()["result"]["structuredContent"] == RECUSADO


def test_32021_aparece_no_log_com_status_400_sem_vazar_nada(fresco: Servidor) -> None:
    chamar(fresco, G, meta=montar_meta(caps={}), id_="log-32021")
    (linha,) = fresco.linhas_de_log(id="log-32021")
    assert linha["status"] == 400 and linha["method"] == "tools/call"
    assert "Marty" not in "\n".join(fresco.stderr)


# ---------------------------------------------------------------- T-08: decline / cancel (AC-17)
@pytest.mark.parametrize("acao", ["decline", "cancel"])
def test_decline_e_cancel_concluem_sem_reservar_e_sem_iserror(fresco: Servidor, acao: str) -> None:
    chave, estado, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, {"action": acao}, estado)
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["resultType"] == "complete"
    assert res.get("isError") in (False, None)  # check 19
    assert res["structuredContent"] == RECUSADO  # chaves e ordem do wire 11
    assert list(res["structuredContent"]) == list(RECUSADO)
    assert texto_de(res) == json.dumps(RECUSADO, indent=2)
    assert "inputRequests" not in res and "requestState" not in res
    # nenhuma reserva criada: o contador continua em res-0003
    for sala in ("sala-fusca", "sala-mirante"):
        assert reservas_em(fresco, sala, "14:00", "15:00") == []
    ocupar(fresco, "sala-aquario", "09:00", "10:00")
    assert reservas_em(fresco, "sala-aquario", "09:00", "10:00") == ["res-0003"]


def test_recusa_e_identica_ao_wire_11(fresco: Servidor) -> None:
    wire = wire_json("11-tools-call-retry-recusa.json")
    corpo = wire["request"]["body"]
    chave, estado, alts = pedir(fresco, corpo["params"]["arguments"])
    assert alts == ["sala-garagem", "sala-mirante"]  # fusca 16-17 (res-0002): garagem e mirante
    resp = retomar(
        fresco,
        corpo["params"]["arguments"],
        chave,
        corpo["params"]["inputResponses"]["__main__:escolha_de_sala"],
        estado,
        id_=corpo["id"],
    )
    assert resp.status_code == wire["response"]["httpStatus"] == 200
    assert resp.json() == wire["response"]["body"]  # byte a byte
    assert reservas_em(fresco, "sala-fusca", "16:00", "17:00") == ["res-0002"]


def test_decline_com_content_extra_continua_sendo_recusa(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    resposta = {"action": "decline", "content": {"sala": "sala-fusca"}}
    res = retomar(fresco, G, chave, resposta, estado).json()["result"]
    assert res["structuredContent"] == RECUSADO
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_cancel_e_decline_repetidos_com_o_mesmo_estado(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    for acao in ("decline", "cancel", "decline"):
        res = retomar(fresco, G, chave, {"action": acao}, estado).json()["result"]
        assert res["structuredContent"] == RECUSADO


# ---------------------------------------------------------------- multi-rodada
def test_multi_rodada_alternativas_mudam_e_o_servidor_pergunta_de_novo(fresco: Servidor) -> None:
    chave1, estado1, alts1 = pedir(fresco, G)
    assert alts1 == ["sala-fusca", "sala-mirante"]
    ocupar(fresco, "sala-fusca", "14:00", "15:00")  # muda a lista entre as rodadas

    r2 = retomar(fresco, G, chave1, aceitar("sala-fusca"), estado1).json()["result"]
    assert r2["resultType"] == "input_required"  # a resposta (fusca) ficou obsoleta
    (chave2,) = r2["inputRequests"]
    campo = r2["inputRequests"][chave2]["params"]["requestedSchema"]["properties"]["sala"]
    assert campo["const"] == "sala-mirante"
    estado2 = r2["requestState"]
    assert estado2 != estado1
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == []

    ocupar(fresco, "sala-mirante", "14:00", "15:00")  # e muda de novo: nao sobra alternativa
    r3 = retomar(fresco, G, chave2, aceitar("sala-mirante"), estado2).json()["result"]
    assert r3["isError"] is True and texto_de(r3) == MSG_SEM_ALT
    assert len(consultar(fresco, "sala-mirante", "14:00", "15:00")["conflitos"]) == 1


def test_estado_da_rodada_2_tambem_e_protegido(fresco: Servidor) -> None:
    chave1, estado1, _ = pedir(fresco, G)
    ocupar(fresco, "sala-fusca", "14:00", "15:00")
    r2 = retomar(fresco, G, chave1, aceitar("sala-fusca"), estado1).json()["result"]
    (chave2,) = r2["inputRequests"]
    estado2 = r2["requestState"]
    ruim = estado2[:-6] + "AAAAAA"
    resp = retomar(fresco, G, chave2, aceitar("sala-mirante"), ruim)
    assert resp.status_code == 400 and erro_de(resp)["code"] == -32602
    fim = retomar(fresco, G, chave2, aceitar("sala-mirante"), estado2).json()["result"]
    assert fim["structuredContent"]["sala"] == "sala-mirante"
