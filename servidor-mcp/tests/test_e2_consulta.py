"""Integracao E2 (processo REAL): consultar_disponibilidade, erros exatos, resource, -32020, tool inexistente."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import DADOS_PADRAO

from .servidor_proc import Servidor, montar_meta, texto_de

DIA = "2026-11-03"
WIRE = DADOS_PADRAO.parent / "exemplos" / "wire"

ERRO_SALA = "Sala inexistente: sala-delorean"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_FORMATO = "Formato invalido: inicio e fim devem ser ISO 8601 com fuso"


def h(hora: str, fuso: str = "-03:00") -> str:
    return f"{DIA}T{hora}:00{fuso}"


def consultar(srv: Servidor, sala: str, inicio: str, fim: str, **kw: Any) -> dict[str, Any]:
    return srv.tool("consultar_disponibilidade", {"sala": sala, "inicio": inicio, "fim": fim}, **kw)


def wire_json(nome: str) -> dict[str, Any]:
    dado: dict[str, Any] = json.loads((WIRE / nome).read_text(encoding="utf-8"))
    return dado


# ---------------------------------------------------------------- tools/list (AC-01/02, wire 01)
def test_tools_list_das_tools_do_e2_e_identico_ao_wire_01(servidor: Servidor) -> None:
    obtidas = {t["name"]: t for t in servidor.rpc("tools/list").json()["result"]["tools"]}
    esperadas = {
        t["name"]: t for t in wire_json("01-tools-list.json")["response"]["body"]["result"]["tools"]
    }
    for nome in ("listar_salas", "consultar_disponibilidade"):
        assert obtidas[nome] == esperadas[nome], nome
    assert all(t["inputSchema"]["type"] == "object" for t in obtidas.values())


# ---------------------------------------------------------------- consultar_disponibilidade
def test_intervalo_livre(servidor: Servidor) -> None:
    res = consultar(servidor, "sala-aquario", h("09:00"), h("10:00"))
    assert res["isError"] is False and res["resultType"] == "complete"
    assert res["structuredContent"] == {"sala": "sala-aquario", "livre": True, "conflitos": []}
    assert json.loads(texto_de(res)) == res["structuredContent"]
    assert texto_de(res) == json.dumps(res["structuredContent"], indent=2)


def test_intervalo_ocupado_lista_os_conflitos_do_seed(servidor: Servidor) -> None:
    res = consultar(servidor, "sala-garagem", h("14:30"), h("15:30"))
    assert res["structuredContent"] == {
        "sala": "sala-garagem",
        "livre": False,
        "conflitos": [
            {
                "id": "res-0001",
                "inicio": "2026-11-03T14:00:00-03:00",
                "fim": "2026-11-03T15:00:00-03:00",
                "responsavel": "Marty",
            }
        ],
    }


def test_fim_igual_ao_inicio_da_reserva_nao_conflita(servidor: Servidor) -> None:
    """Semiaberto: garagem tem 14:00-15:00; 15:00-16:00 e 13:00-14:00 estao livres."""
    assert (
        consultar(servidor, "sala-garagem", h("15:00"), h("16:00"))["structuredContent"]["livre"]
        is True
    )
    assert (
        consultar(servidor, "sala-garagem", h("13:00"), h("14:00"))["structuredContent"]["livre"]
        is True
    )
    assert (
        consultar(servidor, "sala-garagem", h("13:59"), h("14:01"))["structuredContent"]["livre"]
        is False
    )


def test_outra_sala_no_mesmo_horario_esta_livre(servidor: Servidor) -> None:
    assert (
        consultar(servidor, "sala-mirante", h("14:00"), h("15:00"))["structuredContent"]["livre"]
        is True
    )


def test_instantes_em_outros_fusos_sao_normalizados_para_o_conflito(servidor: Servidor) -> None:
    # 17:30Z = 14:30-03:00 conflita com res-0001 (14:00-15:00 -03:00); a saida sai em -03:00
    res = consultar(servidor, "sala-garagem", "2026-11-03T17:30:00Z", "2026-11-03T18:30:00Z")
    conflitos = res["structuredContent"]["conflitos"]
    assert res["structuredContent"]["livre"] is False
    assert conflitos[0]["inicio"] == "2026-11-03T14:00:00-03:00"


def test_reservar_no_passado_e_consultar_e_permitido(servidor: Servidor) -> None:
    res = consultar(
        servidor, "sala-aquario", "2001-01-01T09:00:00-03:00", "2001-01-01T10:00:00-03:00"
    )
    assert res["isError"] is False and res["structuredContent"]["livre"] is True


# ---------------------------------------------------------------- erros de execucao: texto IGUAL (T-01, R-MCP-14)
@pytest.mark.parametrize(
    ("sala", "inicio", "fim", "esperado"),
    [
        ("sala-delorean", h("09:00"), h("10:00"), ERRO_SALA),
        ("sala-aquario", h("07:00"), h("08:00"), ERRO_JANELA),  # check 10
        ("sala-aquario", h("09:00"), h("12:00"), ERRO_DURACAO),  # check 11
        ("sala-aquario", h("10:00"), h("09:00"), ERRO_INTERVALO),  # check 12
        ("sala-aquario", h("09:00"), h("09:00"), ERRO_INTERVALO),  # vazio
        ("sala-aquario", h("19:30"), h("20:30"), ERRO_JANELA),  # fim fora
        ("sala-aquario", h("07:30"), h("09:00"), ERRO_JANELA),  # inicio fora
        ("sala-aquario", "2026-11-03T09:00:00", h("10:00"), ERRO_FORMATO),  # sem fuso
        ("sala-aquario", "lixo", h("10:00"), ERRO_FORMATO),
        ("sala-aquario", h("09:00"), "", ERRO_FORMATO),
    ],
)
def test_erros_de_execucao_com_texto_exato(
    servidor: Servidor, sala: str, inicio: str, fim: str, esperado: str
) -> None:
    resp = servidor.rpc(
        "tools/call",
        {
            "name": "consultar_disponibilidade",
            "arguments": {"sala": sala, "inicio": inicio, "fim": fim},
        },
    )
    assert resp.status_code == 200  # erro de EXECUCAO, nao de protocolo
    res = resp.json()["result"]
    assert res["resultType"] == "complete" and res["isError"] is True
    assert len(res["content"]) == 1
    assert res["content"][0]["text"] == esperado  # IGUAL (sem prefixo "Error executing tool")
    assert "structuredContent" not in res


def test_z_e_aceito_e_e_avaliado_em_menos_tres(servidor: Servidor) -> None:
    ok = consultar(
        servidor, "sala-aquario", "2026-11-03T11:00:00Z", "2026-11-03T12:00:00Z"
    )  # 08:00-09:00
    assert ok["isError"] is False
    resp = servidor.rpc(
        "tools/call",
        {
            "name": "consultar_disponibilidade",
            "arguments": {
                "sala": "sala-aquario",
                "inicio": "2026-11-03T10:59:00Z",
                "fim": "2026-11-03T11:30:00Z",
            },
        },
    )
    assert resp.json()["result"]["content"][0]["text"] == ERRO_JANELA  # 07:59-03:00


def test_ordem_das_validacoes_no_wire(servidor: Servidor) -> None:
    def texto(sala: str, ini: str, fim: str) -> str:
        r = servidor.rpc(
            "tools/call",
            {
                "name": "consultar_disponibilidade",
                "arguments": {"sala": sala, "inicio": ini, "fim": fim},
            },
        )
        return str(r.json()["result"]["content"][0]["text"])

    assert texto("nao-existe", "lixo", "lixo") == "Sala inexistente: nao-existe"
    assert texto("sala-aquario", "lixo", h("09:00")) == ERRO_FORMATO
    assert texto("sala-aquario", h("22:00"), h("21:00")) == ERRO_INTERVALO
    assert texto("sala-aquario", h("07:00"), h("10:00")) == ERRO_JANELA


# ---------------------------------------------------------------- argumentos invalidos: nunca 500/stack trace
@pytest.mark.parametrize(
    "argumentos",
    [
        {},
        {"sala": "sala-aquario"},
        {"sala": 1, "inicio": h("09:00"), "fim": h("10:00")},
        {"sala": None},
    ],
    ids=["vazio", "faltando", "tipo-errado", "nulo"],
)
def test_argumentos_invalidos_sao_recusados_sem_500(
    servidor: Servidor, argumentos: dict[str, Any]
) -> None:
    resp = servidor.rpc(
        "tools/call", {"name": "consultar_disponibilidade", "arguments": argumentos}
    )
    assert resp.status_code < 500
    corpo = resp.json()
    recusou = "error" in corpo or corpo["result"]["isError"] is True
    assert recusou
    assert "Traceback" not in resp.text


# ---------------------------------------------------------------- tool inexistente (AC-05, check 06)
def test_tool_inexistente_e_recusada(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/call", {"name": "voar_delorean", "arguments": {}})
    corpo = resp.json()
    assert resp.status_code < 500
    assert corpo.get("error", {}).get("code") == -32602 or corpo["result"]["isError"] is True


# ---------------------------------------------------------------- resource politica://uso (AC-06)
def test_resource_devolve_o_texto_literal_da_politica(servidor: Servidor) -> None:
    resp = servidor.rpc("resources/read", {"uri": "politica://uso"})
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["resultType"] == "complete"
    assert len(res["contents"]) == 1
    conteudo = res["contents"][0]
    assert conteudo["uri"] == "politica://uso"
    assert conteudo["mimeType"] == "text/markdown"
    arquivo = (DADOS_PADRAO / "politica-de-uso.md").read_bytes().decode("utf-8")
    assert conteudo["text"] == arquivo.replace("\r\n", "\n")  # literal, com LF em qualquer checkout
    assert conteudo["text"].startswith("versao: 2026-11-01\n")
    assert "\r" not in conteudo["text"]


def test_resource_igual_ao_wire_05(servidor: Servidor) -> None:
    obtido = servidor.rpc("resources/read", {"uri": "politica://uso"}, id_=5).json()
    assert obtido == wire_json("05-resources-read-politica.json")["response"]["body"]
    assert obtido["result"]["ttlMs"] == 0 and obtido["result"]["cacheScope"] == "private"


@pytest.mark.parametrize(
    "uri",
    ["politica://inexistente", "politica://uso/extra", "http://x/y", "politica://USO", "nada"],
)
def test_resource_inexistente_devolve_32602_e_nunca_contents_vazio(
    servidor: Servidor, uri: str
) -> None:
    resp = servidor.rpc("resources/read", {"uri": uri}, nome=uri)
    corpo = resp.json()
    assert corpo["error"]["code"] == -32602
    assert resp.status_code == 400
    assert "result" not in corpo


def test_resources_list_expoe_o_resource(servidor: Servidor) -> None:
    res = servidor.rpc("resources/list").json()["result"]
    assert [r["uri"] for r in res["resources"]] == ["politica://uso"]
    assert res["resources"][0]["mimeType"] == "text/markdown"


# ---------------------------------------------------------------- Mcp-Method/Mcp-Name vs corpo -> -32020 (T-29, R-MCP-13)
def test_mcp_method_divergente_do_corpo_gera_32020(servidor: Servidor) -> None:
    resp = servidor.rpc(
        "tools/call", {"name": "listar_salas", "arguments": {}}, mcp_method="tools/list"
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32020


def test_mcp_name_divergente_do_corpo_gera_32020(servidor: Servidor) -> None:
    resp = servidor.rpc(
        "tools/call", {"name": "listar_salas", "arguments": {}}, nome="reservar_sala"
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32020
    resp = servidor.rpc("resources/read", {"uri": "politica://uso"}, nome="politica://outro")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32020


def test_mcp_name_ausente_em_tools_call_gera_32020(servidor: Servidor) -> None:
    corpo = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "listar_salas", "arguments": {}, "_meta": montar_meta()},
    }
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
    }
    resp = servidor.cliente.post(servidor.url, json=corpo, headers=hdrs)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32020


def test_mcp_protocol_version_header_divergente_e_recusado_com_400(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/list", cabecalhos={"MCP-Protocol-Version": "2025-06-18"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] in (-32020, -32600, -32602)


# ---------------------------------------------------------------- log dos erros (stderr)
def test_log_registra_a_consulta_com_erro_de_execucao(servidor: Servidor) -> None:
    servidor.rpc(
        "tools/call",
        {
            "name": "consultar_disponibilidade",
            "arguments": {"sala": "sala-delorean", "inicio": h("09:00"), "fim": h("10:00")},
        },
        id_="e2-log",
    )
    linha = servidor.linhas_de_log(id="e2-log")[0]
    assert linha["method"] == "tools/call" and linha["mcp_name"] == "consultar_disponibilidade"
    assert linha["status"] == 200
