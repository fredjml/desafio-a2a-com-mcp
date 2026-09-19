"""Integracao E4b (processo REAL): requestState selado, retry, reuso, expiracao, restart, revalidacao.

T-04 (tamper), T-05 (expiracao), T-06 (restart), T-07 (args divergentes), T-25 (reuso), T-31
(alternativa obsoleta), T-38 (estado ausente/null/chave errada/outra tool), T-11 (id novo no retry).
O segredo e novo em memoria a cada execucao; nada e impresso.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from .mrtr_util import (
    MSG_SEM_ALT,
    RECUSADO,
    aceitar,
    args,
    assert_estado_invalido,
    chamar,
    consultar,
    erro_de,
    ocupar,
    pedir,
    reservas_em,
    retomar,
    trocar_um_char,
    wire_json,
)
from .servidor_proc import Servidor, texto_de

G = args(
    "sala-garagem", "14:00", "15:00"
)  # conflita com o seed res-0001; alternativas fusca, mirante
ALFABETO = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def decodificar(b64: str) -> bytes:
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))


def mutar(estado: str, indice: int) -> str:
    """Troca 1 caractere do requestState garantindo que o VALOR muda (nao so bits de preenchimento)."""
    i = indice % len(estado)
    if i < 3:  # prefixo "v1."
        return trocar_um_char(estado, i)
    original = decodificar(estado[3:])
    for c in ALFABETO:
        if c == estado[i]:
            continue
        candidato = estado[:i] + c + estado[i + 1 :]
        try:
            if decodificar(candidato[3:]) != original:
                return candidato
        except (binascii.Error, ValueError):
            continue
    raise AssertionError("nao achei mutacao que altere o valor")


# ---------------------------------------------------------------- retry completo (AC-12, wire 04)
def test_retry_conclui_e_e_identico_ao_wire_04(fresco: Servidor) -> None:
    ocupar(fresco, "sala-aquario", "09:00", "10:00", "Doc")  # a reserva do wire 04 e res-0004
    wire = wire_json("04-tools-call-retry.json")
    corpo = wire["request"]["body"]
    chave, estado, _ = pedir(fresco, corpo["params"]["arguments"])
    resp = retomar(
        fresco,
        corpo["params"]["arguments"],
        chave,
        corpo["params"]["inputResponses"]["__main__:escolha_de_sala"],
        estado,
        id_=corpo["id"],
    )
    assert resp.status_code == wire["response"]["httpStatus"] == 200
    assert (
        resp.json() == wire["response"]["body"]
    )  # byte a byte (texto, isError, structured, _meta)


def test_retry_reserva_a_alternativa_escolhida_no_intervalo_e_responsavel_originais(
    fresco: Servidor,
) -> None:
    chave, estado, alts = pedir(fresco, G)
    assert alts == ["sala-fusca", "sala-mirante"]
    res = retomar(fresco, G, chave, aceitar("sala-mirante"), estado).json()["result"]
    assert res["resultType"] == "complete" and res["isError"] is False
    assert "inputRequests" not in res and "requestState" not in res
    sc = res["structuredContent"]
    assert sc == {
        "reserva": "res-0003",
        "reservado": True,
        "sala": "sala-mirante",
        "inicio": G["inicio"],
        "fim": G["fim"],
        "responsavel": "Marty",
        "politica": "2026-11-01",
        "motivo": None,
    }
    assert texto_de(res).startswith("{") and '"sala-mirante"' in texto_de(res)
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == ["res-0003"]
    assert reservas_em(fresco, "sala-garagem", "14:00", "15:00") == ["res-0001"]  # pedida intacta


def test_id_do_retry_e_diferente_do_inicial_e_ambos_aparecem_no_log(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G, id_="req-original-1")
    retomar(fresco, G, chave, aceitar("sala-fusca"), estado, id_="req-retry-2")
    linhas = fresco.linhas_de_log(method="tools/call", mcp_name="reservar_sala")
    ids = [ln["id"] for ln in linhas]
    assert "req-original-1" in ids and "req-retry-2" in ids
    assert len(set(ids)) == len(ids) and all(ln["status"] == 200 for ln in linhas)


def test_escolha_fora_das_alternativas_nao_reserva(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    for sala in ("sala-porao", "sala-aquario", "sala-garagem", "sala-delorean", ""):
        resp = retomar(fresco, G, chave, aceitar(sala), estado)
        assert resp.status_code < 500, (sala, resp.text[:200])
        res = resp.json().get("result", {})
        assert res.get("structuredContent", {}).get("reservado") is not True, sala
    assert [reservas_em(fresco, s, "14:00", "15:00") for s in ("sala-porao", "sala-aquario")] == [
        [],
        [],
    ]
    assert reservas_em(fresco, "sala-garagem", "14:00", "15:00") == ["res-0001"]


@pytest.mark.parametrize(
    "resposta",
    [
        {"action": "accept"},  # sem content
        {"action": "accept", "content": {}},
        {"action": "accept", "content": {"outro": "x"}},
        {"action": "accept", "content": {"sala": 7}},
        {"action": "explode"},
        {},
    ],
    ids=["sem-content", "content-vazio", "campo-errado", "tipo-errado", "action-invalida", "vazia"],
)
def test_resposta_malformada_nunca_e_500_nem_reserva(
    fresco: Servidor, resposta: dict[str, Any]
) -> None:
    chave, estado, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, resposta, estado)
    assert resp.status_code < 500, resp.text[:300]
    res = resp.json().get("result", {})
    assert res.get("structuredContent", {}).get("reservado") is not True
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == []


# ---------------------------------------------------------------- T-04: adulteracao (AC-13)
@pytest.mark.parametrize(
    "posicao",
    [0, 1, 2, 3, 10, "meio", -20, -2, -1],
    ids=[
        "prefixo-0",
        "prefixo-1",
        "prefixo-2",
        "inicio-3",
        "inicio-payload-10",
        "meio",
        "fim-20",
        "penultimo",
        "ultimo",
    ],
)
def test_um_caractere_trocado_em_qualquer_posicao_e_rejeitado(
    fresco: Servidor, posicao: Any
) -> None:
    chave, estado, _ = pedir(fresco, G)
    indice = len(estado) // 2 if posicao == "meio" else posicao
    ruim = mutar(estado, indice)
    assert ruim != estado and len(ruim) == len(estado)
    assert_estado_invalido(retomar(fresco, G, chave, aceitar("sala-fusca"), ruim))
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == []


def test_todas_as_posicoes_da_assinatura_e_do_corpo_sao_protegidas(fresco: Servidor) -> None:
    """Varre ~40 posicoes espalhadas (incluindo o fim, onde vive a tag GCM): nenhuma passa."""
    chave, estado, _ = pedir(fresco, G)
    passo = max(1, len(estado) // 40)
    for i in list(range(0, len(estado), passo)) + [len(estado) - 1]:
        resp = retomar(fresco, G, chave, aceitar("sala-fusca"), mutar(estado, i))
        assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_ultimos_seis_caracteres_como_o_validador(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    ruim = estado[:-6] + "AAAAAA"
    assert_estado_invalido(retomar(fresco, G, chave, aceitar("sala-fusca"), ruim))


def lixos(estado: str) -> list[Any]:
    return [
        "",
        " ",
        "lixo",
        "v1.",
        "v1." + "A" * 2000,
        "v2." + estado[3:],
        "V1." + estado[3:],
        estado[:20],
        estado + "A",
        estado + "=",
        estado.replace("v1.", "", 1),
        "v1." + estado[3:][::-1],
        "\x00" + estado,
        "a" * 200_000,
    ]


def test_estado_vazio_lixo_truncado_ou_estendido_e_rejeitado(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    for ruim in lixos(estado):
        resp = retomar(fresco, G, chave, aceitar("sala-fusca"), ruim)
        assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


@pytest.mark.parametrize("valor", [123, 1.5, True, [], {}, ["v1.x"], {"a": 1}], ids=repr)
def test_estado_de_tipo_errado_e_erro_de_protocolo_nunca_500(fresco: Servidor, valor: Any) -> None:
    chave, _, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, aceitar("sala-fusca"), valor)
    assert resp.status_code == 400, resp.text[:300]
    assert erro_de(resp)["code"] == -32602
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


# ---------------------------------------------------------------- T-38: ausente, null, chave errada
def test_retry_sem_request_state_e_rejeitado_e_nao_reserva(fresco: Servidor) -> None:
    chave, _, _ = pedir(fresco, G)
    resp = chamar(fresco, G, respostas={chave: aceitar("sala-fusca")})  # campo ausente
    assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_retry_com_request_state_null_e_rejeitado_e_nao_reserva(fresco: Servidor) -> None:
    chave, _, _ = pedir(fresco, G)
    resp = retomar(fresco, G, chave, aceitar("sala-fusca"), None)  # `null` no fio
    assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_pedido_novo_com_state_null_ou_vazio_de_respostas_e_um_pedido_normal(
    fresco: Servidor,
) -> None:
    variantes: list[dict[str, Any]] = [{"estado": None}, {"estado": None, "respostas": {}}]
    for extra in variantes:
        resp = chamar(fresco, G, **extra)
        assert resp.status_code == 200 and resp.json()["result"]["resultType"] == "input_required"


def test_state_valido_sem_input_responses_pergunta_de_novo_sem_reservar(fresco: Servidor) -> None:
    _, estado, _ = pedir(fresco, G)
    resp = chamar(fresco, G, estado=estado)
    assert resp.status_code == 200 and resp.json()["result"]["resultType"] == "input_required"
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_chave_errada_em_input_responses_pergunta_de_novo_sem_reservar(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    for errada in ("escolha_de_sala", "outra:chave", "", chave + "x", "__main__:escolha_de_sala"):
        resp = retomar(fresco, G, errada, aceitar("sala-fusca"), estado)
        assert resp.status_code == 200, (errada, resp.text[:200])
        assert resp.json()["result"]["resultType"] == "input_required"
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


def test_estado_de_outra_tool_ou_argumentos_e_rejeitado(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    outras = [
        ("consultar_disponibilidade", {"sala": G["sala"], "inicio": G["inicio"], "fim": G["fim"]}),
        ("listar_salas", {}),
    ]
    for nome, argumentos in outras:
        resp = fresco.rpc(
            "tools/call",
            {
                "name": nome,
                "arguments": argumentos,
                "requestState": estado,
                "inputResponses": {chave: aceitar("sala-fusca")},
            },
        )
        assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []


# ---------------------------------------------------------------- T-07: argumentos divergentes (AC-14)
@pytest.mark.parametrize(
    "mudanca",
    [
        {"sala": "sala-mirante"},
        {"inicio": "2026-11-03T13:00:00-03:00"},
        {"fim": "2026-11-03T15:30:00-03:00"},
        {"responsavel": "Biff"},
        {
            "sala": "sala-mirante",
            "inicio": "2026-11-03T13:00:00-03:00",
            "fim": "2026-11-03T14:00:00-03:00",
            "responsavel": "Biff",
        },
        {"responsavel": "marty"},
        {"responsavel": "Marty "},
    ],
    ids=["sala", "inicio", "fim", "responsavel", "todos", "caixa", "espaco"],
)
def test_argumentos_divergentes_no_retry_sao_rejeitados_sem_efeito(
    fresco: Servidor, mudanca: dict[str, Any]
) -> None:
    chave, estado, _ = pedir(fresco, G)
    adulterados = {**G, **mudanca}
    assert_estado_invalido(retomar(fresco, adulterados, chave, aceitar("sala-fusca"), estado))
    # nenhuma reserva com valores adulterados (nem com os selados): nada foi criado
    for sala in ("sala-fusca", "sala-mirante", "sala-porao"):
        for janela in (("13:00", "15:00"), ("14:00", "16:00")):
            assert reservas_em(fresco, sala, *janela) == [], (sala, janela)
    ocupar(fresco, "sala-aquario", "09:00", "10:00")
    assert reservas_em(fresco, "sala-aquario", "09:00", "10:00") == ["res-0003"]  # contador intacto


def test_check_18_argumentos_adulterados_como_o_validador(fresco: Servidor) -> None:
    ocupar(fresco, "sala-garagem", "09:00", "10:00", "Ocupante")
    doc = args("sala-garagem", "09:00", "10:00", "Doc")
    chave, estado, _ = pedir(fresco, doc)
    ruim = args("sala-mirante", "13:00", "14:00", "Biff")
    resp = retomar(fresco, ruim, chave, aceitar("sala-fusca"), estado)
    assert_estado_invalido(resp)
    assert reservas_em(fresco, "sala-mirante", "13:00", "14:00") == []
    assert reservas_em(fresco, "sala-fusca", "09:00", "10:00") == []


# ---------------------------------------------------------------- T-25: estado reutilizavel (checks 17 -> 19)
def test_estado_adulterado_e_depois_o_original_ainda_vale(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    assert_estado_invalido(retomar(fresco, G, chave, aceitar("sala-fusca"), mutar(estado, 40)))
    res = retomar(fresco, G, chave, aceitar("sala-fusca"), estado).json()["result"]
    assert res["resultType"] == "complete" and res["structuredContent"]["sala"] == "sala-fusca"


def test_decline_e_depois_accept_com_o_mesmo_estado(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    rec = retomar(fresco, G, chave, {"action": "decline"}, estado).json()["result"]
    assert rec["structuredContent"]["reservado"] is False
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == []
    res = retomar(fresco, G, chave, aceitar("sala-fusca"), estado).json()["result"]
    assert res["structuredContent"]["reservado"] is True  # o estado nao foi consumido
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == ["res-0003"]


def test_check_16_a_19_em_sequencia_como_o_validador(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)  # 13
    ruim = estado[:-6] + "AAAAAA"
    assert_estado_invalido(retomar(fresco, G, chave, aceitar("sala-fusca"), ruim))  # 17
    rec = retomar(fresco, G, chave, {"action": "decline"}, estado).json()["result"]  # 19
    assert rec["resultType"] == "complete" and not rec.get("isError")
    assert rec["structuredContent"]["reservado"] is False


def test_o_mesmo_accept_repetido_nao_cria_reserva_sobreposta(fresco: Servidor) -> None:
    """Sem uso unico o estado pode ser reenviado; a revalidacao impede o duplo agendamento."""
    chave, estado, _ = pedir(fresco, G)
    primeira = retomar(fresco, G, chave, aceitar("sala-mirante"), estado).json()["result"]
    assert primeira["structuredContent"]["reservado"] is True
    segunda = retomar(fresco, G, chave, aceitar("sala-mirante"), estado)
    assert segunda.status_code == 200
    assert segunda.json()["result"]["resultType"] == "input_required"  # mirante ja foi
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == ["res-0003"]


# ---------------------------------------------------------------- T-31: alternativa obsoleta (DEC-20)
def test_segunda_task_com_alternativa_obsoleta_e_perguntada_de_novo(fresco: Servidor) -> None:
    chave_a, estado_a, _ = pedir(fresco, G, id_="A")
    chave_b, estado_b, alts_b = pedir(fresco, G, id_="B")
    assert alts_b == ["sala-fusca", "sala-mirante"]
    a = retomar(fresco, G, chave_a, aceitar("sala-mirante"), estado_a).json()["result"]
    assert a["structuredContent"]["sala"] == "sala-mirante"  # a 1a concluiu

    resp_b = retomar(fresco, G, chave_b, aceitar("sala-mirante"), estado_b)  # obsoleta
    assert resp_b.status_code == 200
    res_b = resp_b.json()["result"]
    assert res_b["resultType"] == "input_required" and "structuredContent" not in res_b
    (pedido,) = res_b["inputRequests"].values()
    assert pedido["params"]["requestedSchema"]["properties"]["sala"]["const"] == "sala-fusca"
    assert res_b["requestState"] != estado_b  # novo estado, alternativas recalculadas
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == ["res-0003"]  # sem sobreposicao

    (nova_chave,) = res_b["inputRequests"]
    fim = retomar(fresco, G, nova_chave, aceitar("sala-fusca"), res_b["requestState"]).json()[
        "result"
    ]
    assert fim["structuredContent"]["sala"] == "sala-fusca"  # multi-rodada converge
    assert reservas_em(fresco, "sala-fusca", "14:00", "15:00") == ["res-0004"]


def test_decline_de_pergunta_obsoleta_conclui_sem_perguntar_de_novo(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    ocupar(fresco, "sala-mirante", "14:00", "15:00")  # a lista de alternativas mudou
    for acao in ("decline", "cancel"):
        res = retomar(fresco, G, chave, {"action": acao}, estado).json()["result"]
        assert res["resultType"] == "complete" and res["structuredContent"] == RECUSADO
        assert not res.get("isError")
    assert len(consultar(fresco, "sala-mirante", "14:00", "15:00")["conflitos"]) == 1


def test_alternativa_obsoleta_sem_nenhuma_outra_devolve_sem_alternativas(fresco: Servidor) -> None:
    chave, estado, _ = pedir(fresco, G)
    ocupar(fresco, "sala-mirante", "14:00", "15:00")  # outro cliente pega as duas alternativas
    ocupar(fresco, "sala-fusca", "14:00", "15:00")
    resp = retomar(fresco, G, chave, aceitar("sala-mirante"), estado)
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["isError"] is True and res["content"] == [{"type": "text", "text": MSG_SEM_ALT}]
    assert "inputRequests" not in res and "requestState" not in res
    assert len(consultar(fresco, "sala-mirante", "14:00", "15:00")["conflitos"]) == 1
    assert len(consultar(fresco, "sala-fusca", "14:00", "15:00")["conflitos"]) == 1


def test_escolha_valida_na_pergunta_antiga_mas_ocupada_agora_nunca_sobrepoe(
    fresco: Servidor,
) -> None:
    """Estado antigo aceita mirante; mirante ocupado depois: nao ha reserva sobreposta nem 500."""
    chave, estado, _ = pedir(fresco, G)
    ocupar(fresco, "sala-mirante", "14:30", "15:30")  # sobreposicao parcial
    resp = retomar(fresco, G, chave, aceitar("sala-mirante"), estado)
    assert resp.status_code == 200
    assert resp.json()["result"].get("structuredContent", {}).get("sala") != "sala-mirante"
    assert reservas_em(fresco, "sala-mirante", "14:00", "15:00") == ["res-0003"]  # so a do ocupante


def test_oito_tasks_pausadas_escolhem_a_mesma_sala_ao_mesmo_tempo(fresco: Servidor) -> None:
    pausadas = [
        pedir(fresco, args("sala-garagem", "14:00", "15:00", f"P{i}"), id_=f"p{i}")
        for i in range(8)
    ]

    def concluir(i: int) -> Any:
        chave, estado, _ = pausadas[i]
        resp = retomar(
            fresco,
            args("sala-garagem", "14:00", "15:00", f"P{i}"),
            chave,
            aceitar("sala-mirante"),
            estado,
        )
        assert resp.status_code == 200, resp.text[:200]
        return resp.json()["result"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(concluir, range(8)))
    concluidas = [r for r in resultados if r.get("structuredContent", {}).get("reservado") is True]
    assert len(concluidas) == 1
    assert all(r["resultType"] == "input_required" for r in resultados if r not in concluidas)
    assert len(consultar(fresco, "sala-mirante", "14:00", "15:00")["conflitos"]) == 1


# ---------------------------------------------------------------- revalidacao quando o conflito sumiu
@pytest.mark.parametrize("acao", ["accept", "decline", "cancel"])
def test_conflito_que_sumiu_reserva_a_pedida_ou_respeita_a_recusa(segredo: str, acao: str) -> None:
    """Restart entre pedido e retry: a reserva que conflitava so existia em memoria e sumiu."""
    pedido = args("sala-garagem", "10:00", "11:00", "Doc")
    a = Servidor(segredo=segredo)
    try:
        a.esperar_pronto()
        ocupar(a, "sala-garagem", "10:00", "11:00", "Ocupante")
        chave, estado, _ = pedir(a, pedido)
    finally:
        a.parar()
    resposta = aceitar("sala-fusca") if acao == "accept" else {"action": acao}
    b = Servidor(segredo=segredo)
    try:
        b.esperar_pronto()
        res = retomar(b, pedido, chave, resposta, estado).json()["result"]
        assert res["resultType"] == "complete" and res["isError"] is False
        if acao == "accept":  # reserva a sala PEDIDA (agora livre), nunca a alternativa
            assert res["structuredContent"]["sala"] == "sala-garagem"
            assert len(reservas_em(b, "sala-garagem", "10:00", "11:00")) == 1
        else:  # a recusa continua valendo: nada e reservado
            assert res["structuredContent"] == RECUSADO
            assert reservas_em(b, "sala-garagem", "10:00", "11:00") == []
        assert reservas_em(b, "sala-fusca", "10:00", "11:00") == []
    finally:
        b.parar()


# ---------------------------------------------------------------- T-06: restart real
def test_restart_com_o_mesmo_segredo_conclui_e_com_outro_e_rejeitado(segredo: str) -> None:
    primeiro = Servidor(segredo=segredo)
    try:
        primeiro.esperar_pronto()
        chave, estado, _ = pedir(primeiro, G)
    finally:
        primeiro.parar()  # processo morto: nada em memoria sobrevive

    mesmo = Servidor(segredo=segredo)  # OUTRO processo (outra porta), MESMO segredo
    try:
        mesmo.esperar_pronto()
        assert mesmo.porta != primeiro.porta
        res = retomar(mesmo, G, chave, aceitar("sala-fusca"), estado).json()["result"]
        assert res["resultType"] == "complete" and res["structuredContent"]["reservado"] is True
        assert res["structuredContent"]["reserva"] == "res-0003"  # a agenda deste processo e nova
        # e o estado segue reutilizavel depois do restart
        rec = retomar(mesmo, G, chave, {"action": "decline"}, estado).json()["result"]
        assert rec["structuredContent"]["reservado"] is False
    finally:
        mesmo.parar()

    outro_segredo = secrets.token_hex(32)
    assert outro_segredo != segredo
    diferente = Servidor(segredo=outro_segredo)
    try:
        diferente.esperar_pronto()
        assert_estado_invalido(retomar(diferente, G, chave, aceitar("sala-fusca"), estado))
        assert reservas_em(diferente, "sala-fusca", "14:00", "15:00") == []
        assert reservas_em(diferente, "sala-garagem", "14:00", "15:00") == ["res-0001"]
    finally:
        diferente.parar()


def test_restart_na_mesma_porta_tambem_conclui(segredo: str) -> None:
    primeiro = Servidor(segredo=segredo)
    porta = primeiro.porta
    try:
        primeiro.esperar_pronto()
        chave, estado, _ = pedir(primeiro, G)
    finally:
        primeiro.parar()
    segundo = Servidor(segredo=segredo, porta=porta)
    try:
        segundo.esperar_pronto()
        res = retomar(segundo, G, chave, aceitar("sala-mirante"), estado).json()["result"]
        assert res["structuredContent"]["sala"] == "sala-mirante"
    finally:
        segundo.parar()


# ---------------------------------------------------------------- T-05: expiracao (TTL curto so em teste)
def test_estado_expirado_e_rejeitado_e_o_recem_emitido_vale(segredo: str) -> None:
    srv = Servidor(segredo=segredo, env_extra={"REQUEST_STATE_TTL_S_SOMENTE_TESTE": "2"})
    try:
        srv.esperar_pronto()
        chave, estado, _ = pedir(srv, G)
        chave_velha, estado_velho, _ = pedir(srv, G)
        ok = retomar(srv, G, chave, {"action": "decline"}, estado)  # dentro do TTL: vale
        assert ok.json()["result"]["resultType"] == "complete"
        time.sleep(3.5)  # ultrapassa o TTL de 2 s
        assert_estado_invalido(retomar(srv, G, chave_velha, aceitar("sala-fusca"), estado_velho))
        assert_estado_invalido(retomar(srv, G, chave, {"action": "decline"}, estado))  # o 1o tambem
        assert reservas_em(srv, "sala-fusca", "14:00", "15:00") == []
        chave_nova, novo, _ = pedir(srv, G)  # um estado NOVO, emitido agora, volta a valer
        res = retomar(srv, G, chave_nova, aceitar("sala-fusca"), novo).json()["result"]
        assert res["structuredContent"]["reservado"] is True
    finally:
        srv.parar()


def _boot(srv: Servidor) -> dict[str, Any]:
    return dict(json.loads(next(ln for ln in srv.stderr if '"evento": "boot"' in ln)))


def test_ttl_padrao_e_600_s_e_aparece_no_boot(servidor: Servidor) -> None:
    boot = _boot(servidor)
    assert boot["request_state_ttl_s"] == 600.0 and boot["ttl_de_teste"] is False
    assert not any('"ttl_de_teste": true' in ln for ln in servidor.stderr)  # sem aviso de teste


def test_ttl_de_teste_e_sinalizado_no_boot_com_aviso(segredo: str) -> None:
    srv = Servidor(segredo=segredo, env_extra={"REQUEST_STATE_TTL_S_SOMENTE_TESTE": "7"})
    try:
        srv.esperar_pronto()
        boot = _boot(srv)
        assert boot["request_state_ttl_s"] == 7.0 and boot["ttl_de_teste"] is True
        avisos = [json.loads(ln) for ln in srv.stderr if '"evento": "aviso"' in ln]
        assert any(a.get("ttl_de_teste") is True and "TESTE" in a["mensagem"] for a in avisos)
    finally:
        srv.parar()


@pytest.mark.parametrize("valor", ["299", "1801", "0", "nan", "abc"])
def test_ttl_normal_fora_da_faixa_derruba_o_boot_com_exit_2(segredo: str, valor: str) -> None:
    srv = Servidor(segredo=segredo, env_extra={"REQUEST_STATE_TTL_S": valor})
    try:
        assert srv.esperar_saida() == 2
        assert any("REQUEST_STATE_TTL_S" in ln and "erro_boot" in ln for ln in srv.stderr)
    finally:
        srv.parar()


# ---------------------------------------------------------------- privacidade (R-MRTR-05, CLAUDE.md 9)
def test_request_state_e_respostas_nunca_vao_para_o_log(fresco: Servidor, segredo: str) -> None:
    chave, estado, _ = pedir(fresco, G)
    retomar(fresco, G, chave, aceitar("sala-fusca"), estado)
    retomar(fresco, G, chave, aceitar("sala-fusca"), mutar(estado, 30))
    retomar(fresco, G, chave, aceitar("sala-fusca"), "lixo-de-estado-QWERTY")
    fresco.linhas_de_log()  # sincroniza a thread leitora
    todo = "\n".join(fresco.stderr + fresco.stdout)
    assert estado not in todo and estado[3:40] not in todo
    assert "lixo-de-estado-QWERTY" not in todo and segredo not in todo
    assert "Marty" not in todo  # argumentos da tool tambem nao
    assert fresco.stdout == []
