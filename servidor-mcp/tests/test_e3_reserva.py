"""Integracao E3 (processo REAL): reservar_sala no caminho feliz e validacoes. O conflito (MRTR) e da E4."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]  # dep transitiva do mcp

from app.config import DADOS_PADRAO

from .servidor_proc import Servidor, texto_de

DIA = "2026-11-03"
WIRE = DADOS_PADRAO.parent / "exemplos" / "wire"
CHAVES_DA_RESERVA = [
    "reserva",
    "reservado",
    "sala",
    "inicio",
    "fim",
    "responsavel",
    "politica",
    "motivo",
]


def h(hora: str, fuso: str = "-03:00") -> str:
    return f"{DIA}T{hora}:00{fuso}"


def reservar(
    srv: Servidor, sala: str, inicio: str, fim: str, responsavel: str = "Doc"
) -> dict[str, Any]:
    return srv.tool(
        "reservar_sala",
        {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel},
    )


def consultar(srv: Servidor, sala: str, inicio: str, fim: str) -> dict[str, Any]:
    return srv.tool("consultar_disponibilidade", {"sala": sala, "inicio": inicio, "fim": fim})


def wire_json(nome: str) -> dict[str, Any]:
    dado: dict[str, Any] = json.loads((WIRE / nome).read_text(encoding="utf-8"))
    return dado


@pytest.fixture
def servidor_fresco(segredo: str) -> Iterator[Servidor]:
    """Processo novo por teste (o estado das reservas e em memoria)."""
    srv = Servidor(segredo=segredo)
    try:
        srv.esperar_pronto()
        yield srv
    finally:
        srv.parar()


# ---------------------------------------------------------------- tools/list completo (check 01, AC-01/02)
def test_tools_list_identico_ao_wire_01_com_as_tres_tools(servidor: Servidor) -> None:
    res = servidor.rpc("tools/list", id_=1).json()
    esperado = wire_json("01-tools-list.json")["response"]["body"]
    assert {t["name"] for t in res["result"]["tools"]} == {
        "listar_salas",
        "consultar_disponibilidade",
        "reservar_sala",
    }
    obtidas = {t["name"]: t for t in res["result"]["tools"]}
    for t in esperado["result"]["tools"]:
        assert obtidas[t["name"]] == t, t["name"]
    assert res == esperado  # o envelope inteiro (cacheScope, resultType, ttlMs, _meta) tambem


def test_input_e_output_schemas_sao_json_schema_validos(servidor: Servidor) -> None:
    for tool in servidor.rpc("tools/list").json()["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        Draft202012Validator.check_schema(tool["inputSchema"])
        Draft202012Validator.check_schema(tool["outputSchema"])


# ---------------------------------------------------------------- caminho feliz = wire 02 (byte a byte)
def test_reserva_livre_e_identica_ao_wire_02(servidor_fresco: Servidor) -> None:
    wire = wire_json("02-tools-call-livre.json")
    corpo = wire["request"]["body"]
    resp = servidor_fresco.rpc(
        "tools/call",
        {"name": corpo["params"]["name"], "arguments": corpo["params"]["arguments"]},
        id_=corpo["id"],
    )
    assert resp.status_code == wire["response"]["httpStatus"] == 200
    assert resp.json() == wire["response"]["body"]


def test_structured_content_exato_e_bloco_de_texto_igual(servidor: Servidor) -> None:
    res = reservar(servidor, "sala-porao", h("09:00"), h("10:00"), "Marty")
    assert res["resultType"] == "complete" and res["isError"] is False
    sc = res["structuredContent"]
    assert list(sc) == CHAVES_DA_RESERVA  # exatamente estas 8 chaves, nesta ordem
    assert sc["reserva"].startswith("res-") and sc["reservado"] is True
    assert (sc["sala"], sc["inicio"], sc["fim"], sc["responsavel"]) == (
        "sala-porao",
        "2026-11-03T09:00:00-03:00",
        "2026-11-03T10:00:00-03:00",
        "Marty",
    )
    assert sc["politica"] == "2026-11-01" and sc["motivo"] is None
    assert len(res["content"]) == 1 and res["content"][0]["type"] == "text"
    assert texto_de(res) == json.dumps(sc, indent=2)  # indent=2, como nos wire
    assert json.loads(texto_de(res)) == sc


def test_structured_content_valida_contra_o_output_schema(servidor: Servidor) -> None:
    tools = {t["name"]: t for t in servidor.rpc("tools/list").json()["result"]["tools"]}
    res = reservar(servidor, "sala-porao", h("11:00"), h("12:00"))
    Draft202012Validator(tools["reservar_sala"]["outputSchema"]).validate(res["structuredContent"])
    lista = servidor.tool("listar_salas")
    Draft202012Validator(tools["listar_salas"]["outputSchema"]).validate(lista["structuredContent"])
    cons = consultar(servidor, "sala-porao", h("11:00"), h("12:00"))
    Draft202012Validator(tools["consultar_disponibilidade"]["outputSchema"]).validate(
        cons["structuredContent"]
    )


# ---------------------------------------------------------------- ids sequenciais e visibilidade (T-24)
def test_ids_sequenciais_a_partir_de_res_0003_e_visiveis_na_consulta(
    servidor_fresco: Servidor,
) -> None:
    srv = servidor_fresco
    a = reservar(srv, "sala-aquario", h("09:00"), h("10:00"), "Doc")["structuredContent"]
    b = reservar(srv, "sala-aquario", h("10:00"), h("11:00"), "Biff")["structuredContent"]
    assert (a["reserva"], b["reserva"]) == ("res-0003", "res-0004")

    cons = consultar(srv, "sala-aquario", h("09:30"), h("10:30"))["structuredContent"]
    assert cons["livre"] is False
    assert cons["conflitos"] == [
        {
            "id": "res-0003",
            "inicio": "2026-11-03T09:00:00-03:00",
            "fim": "2026-11-03T10:00:00-03:00",
            "responsavel": "Doc",
        },
        {
            "id": "res-0004",
            "inicio": "2026-11-03T10:00:00-03:00",
            "fim": "2026-11-03T11:00:00-03:00",
            "responsavel": "Biff",
        },
    ]
    assert consultar(srv, "sala-aquario", h("11:00"), h("12:00"))["structuredContent"]["livre"]


def test_reservas_do_seed_sao_carregadas_no_boot(servidor_fresco: Servidor) -> None:
    cons = consultar(servidor_fresco, "sala-fusca", h("16:00"), h("17:00"))["structuredContent"]
    assert [c["id"] for c in cons["conflitos"]] == ["res-0002"]


def test_reservas_nao_sobrevivem_a_restart(segredo: str) -> None:
    ids = []
    for _ in range(2):
        srv = Servidor(segredo=segredo)
        try:
            srv.esperar_pronto()
            ids.append(
                reservar(srv, "sala-aquario", h("09:00"), h("10:00"))["structuredContent"][
                    "reserva"
                ]
            )
        finally:
            srv.parar()
    assert ids == ["res-0003", "res-0003"]  # memoria apenas: o segundo processo comeca do seed


# ---------------------------------------------------------------- mesmas validacoes do E2 (textos exatos)
@pytest.mark.parametrize(
    ("sala", "inicio", "fim", "esperado"),
    [
        ("sala-delorean", h("09:00"), h("10:00"), "Sala inexistente: sala-delorean"),  # check 09
        (
            "sala-aquario",
            h("07:00"),
            h("08:00"),
            "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00",
        ),
        (
            "sala-aquario",
            h("09:00"),
            h("12:00"),
            "Duracao acima do limite: a politica permite no maximo 2 horas",
        ),
        (
            "sala-aquario",
            h("10:00"),
            h("09:00"),
            "Intervalo invalido: fim deve ser posterior a inicio",
        ),
        (
            "sala-aquario",
            "2026-11-03T09:00:00",
            h("10:00"),
            "Formato invalido: inicio e fim devem ser ISO 8601 com fuso",
        ),
    ],
)
def test_validacoes_da_reserva_iguais_as_da_consulta_e_sem_efeito_colateral(
    servidor_fresco: Servidor, sala: str, inicio: str, fim: str, esperado: str
) -> None:
    resp = servidor_fresco.rpc(
        "tools/call",
        {
            "name": "reservar_sala",
            "arguments": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": "Doc"},
        },
    )
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["isError"] is True and res["resultType"] == "complete"
    assert res["content"][0]["text"] == esperado and len(res["content"]) == 1  # igual, sem prefixo
    assert "structuredContent" not in res
    # nenhuma reserva foi criada: a proxima ainda e res-0003
    nova = reservar(servidor_fresco, "sala-mirante", h("09:00"), h("10:00"))["structuredContent"]
    assert nova["reserva"] == "res-0003"


def test_mesma_regra_de_erro_nas_duas_tools(servidor: Servidor) -> None:
    casos = [
        ("sala-delorean", h("09:00"), h("10:00")),
        ("sala-aquario", h("07:00"), h("08:00")),
        ("sala-aquario", h("09:00"), h("12:00")),
        ("sala-aquario", h("10:00"), h("09:00")),
        ("sala-aquario", "lixo", h("09:00")),
    ]
    for sala, ini, fim in casos:
        a = servidor.rpc(
            "tools/call",
            {
                "name": "consultar_disponibilidade",
                "arguments": {"sala": sala, "inicio": ini, "fim": fim},
            },
        ).json()["result"]
        b = servidor.rpc(
            "tools/call",
            {
                "name": "reservar_sala",
                "arguments": {"sala": sala, "inicio": ini, "fim": fim, "responsavel": "X"},
            },
        ).json()["result"]
        assert a["isError"] is b["isError"] is True
        assert texto_de(a) == texto_de(b)


# ---------------------------------------------------------------- regras de intervalo na reserva
def test_pontas_da_janela_sao_aceitas_na_reserva(servidor_fresco: Servidor) -> None:
    assert reservar(servidor_fresco, "sala-aquario", h("08:00"), h("10:00"))["isError"] is False
    assert reservar(servidor_fresco, "sala-aquario", h("18:00"), h("20:00"))["isError"] is False


def test_reserva_encostada_nao_conflita_semiaberto(servidor_fresco: Servidor) -> None:
    """garagem tem 14:00-15:00 (seed): 15:00-16:00 e 13:00-14:00 sao livres."""
    assert reservar(servidor_fresco, "sala-garagem", h("15:00"), h("16:00"))["isError"] is False
    assert reservar(servidor_fresco, "sala-garagem", h("13:00"), h("14:00"))["isError"] is False


def test_reserva_com_z_e_normalizada_para_menos_tres(servidor_fresco: Servidor) -> None:
    sc = reservar(servidor_fresco, "sala-aquario", "2026-11-03T12:00:00Z", "2026-11-03T13:00:00Z")[
        "structuredContent"
    ]
    assert (sc["inicio"], sc["fim"]) == ("2026-11-03T09:00:00-03:00", "2026-11-03T10:00:00-03:00")
    # e conflita quando pedida de novo no equivalente em -03:00
    outra = servidor_fresco.rpc(
        "tools/call",
        {
            "name": "reservar_sala",
            "arguments": {
                "sala": "sala-aquario",
                "inicio": h("09:30"),
                "fim": h("10:30"),
                "responsavel": "Y",
            },
        },
    ).json()["result"]
    # conflito => MRTR (E4a): pergunta a alternativa; nada foi reservado
    assert outra["resultType"] == "input_required" and "structuredContent" not in outra


def test_reservar_no_passado_e_permitido(servidor_fresco: Servidor) -> None:
    res = reservar(
        servidor_fresco, "sala-aquario", "2001-01-01T09:00:00-03:00", "2001-01-01T10:00:00-03:00"
    )
    assert res["isError"] is False and res["structuredContent"]["reservado"] is True


# ---------------------------------------------------------------- atomicidade e privacidade do log
def test_reservas_concorrentes_do_mesmo_intervalo_criam_uma_so(servidor_fresco: Servidor) -> None:
    srv = servidor_fresco

    def tentar(i: int) -> bool:
        resp = srv.rpc(
            "tools/call",
            {
                "name": "reservar_sala",
                "arguments": {
                    "sala": "sala-mirante",
                    "inicio": h("09:00"),
                    "fim": h("10:00"),
                    "responsavel": f"P{i}",
                },
            },
            id_=f"conc-{i}",
        )
        return resp.json()["result"]["isError"] is False

    with ThreadPoolExecutor(max_workers=12) as pool:
        resultados = list(pool.map(tentar, range(24)))
    assert sum(resultados) == 1
    cons = consultar(srv, "sala-mirante", h("09:00"), h("10:00"))["structuredContent"]
    assert len(cons["conflitos"]) == 1


def test_log_nao_registra_argumentos_da_reserva(servidor: Servidor) -> None:
    reservar(servidor, "sala-fusca", h("09:00"), h("10:00"), "NOME-PRIVADO-XYZ")
    servidor.linhas_de_log()  # sincroniza a thread leitora
    assert "NOME-PRIVADO-XYZ" not in "\n".join(servidor.stderr)
