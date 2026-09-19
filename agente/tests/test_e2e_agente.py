"""E2E: servidor MCP REAL + agente REAL (subprocess), o agente falando com o MCP por um proxy gravador.

Cobre R-HOST-01/03/04, R-A2A-03/04/06, R-ARQ-01/02, AC-18/19/23/24/25/26/27, T-19/T-26/T-34/T-40, T-17.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from .procs import AgenteReal, ProxyGravador, ServidorMcpReal, porta_livre, python_do_servidor

DIA = "2026-11-03"
TRACE_ID = secrets.token_hex(16)  # novo por execucao
TRACEPARENT = f"00-{TRACE_ID}-{secrets.token_hex(8)}-01"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SALA = "Sala inexistente: sala-delorean"


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


def pedido(sala: str, ini: str, fim: str, resp: str = "Doc") -> str:
    return f"reservar sala={sala} inicio={h(ini)} fim={h(fim)} responsavel={resp}"


@dataclass
class Ambiente:
    mcp: ServidorMcpReal
    proxy: ProxyGravador
    agente: AgenteReal
    http: httpx.Client

    def a2a(
        self,
        metodo: str,
        params: dict[str, Any],
        *,
        id_: Any = None,
        cabecalhos: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        hdr = {"Content-Type": "application/json"}  # como o validador: sem A2A-Version
        hdr.update(cabecalhos or {})
        corpo = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(6) if id_ is None else id_,
            "method": metodo,
            "params": params,
        }
        r = self.http.post(self.agente.rpc_url, content=json.dumps(corpo), headers=hdr)
        assert r.status_code == 200, r.text[:300]
        resposta: dict[str, Any] = r.json()
        return resposta

    def enviar(
        self, texto: str, task_id: str | None = None, cabecalhos: dict[str, str] | None = None
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "messageId": f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_USER",
            "parts": [{"text": texto}],
        }
        if task_id:
            msg["taskId"] = task_id
        return self.a2a("SendMessage", {"message": msg}, cabecalhos=cabecalhos)


def tarefa(resposta: dict[str, Any]) -> dict[str, Any]:
    resultado = resposta.get("result") or {}
    t: dict[str, Any] = resultado.get("task") or resultado
    return t


def estado(resposta: dict[str, Any]) -> str:
    return str((tarefa(resposta).get("status") or {}).get("state", ""))


def mensagem(resposta: dict[str, Any]) -> str:
    partes = ((tarefa(resposta).get("status") or {}).get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in partes)


def artifact(resposta: dict[str, Any]) -> dict[str, Any]:
    arts = tarefa(resposta).get("artifacts") or []
    assert arts, "sem artifact"
    a: dict[str, Any] = arts[0]
    assert a["name"] == "reserva"
    dados: dict[str, Any] = json.loads(" ".join(p["text"] for p in a["parts"]))
    return dados


@pytest.fixture
def amb(segredo: str) -> Iterator[Ambiente]:
    if python_do_servidor() is None:
        pytest.skip("servidor-mcp/.venv ausente")
    mcp = ServidorMcpReal(segredo=segredo)
    proxy = None
    agente = None
    http = httpx.Client(timeout=30.0)
    try:
        mcp.esperar_porta(mcp.porta)
        proxy = ProxyGravador(mcp.url)
        agente = AgenteReal(mcp_url=proxy.url)
        agente.esperar_porta(agente.porta)
        yield Ambiente(mcp, proxy, agente, http)
    finally:
        http.close()
        if agente is not None:
            agente.parar()
        if proxy is not None:
            proxy.parar()
        mcp.parar()


# ------------------------------------------------------------------------------ caminho feliz
def test_sala_livre_completa_com_artifact_e_getask(amb: Ambiente) -> None:
    r = amb.enviar(pedido("sala-porao", "09:00", "10:00"), cabecalhos={"traceparent": TRACEPARENT})
    assert estado(r) == "TASK_STATE_COMPLETED", r
    m = re.fullmatch(r"Reserva (res-\d{4}) confirmada na sala-porao\.", mensagem(r))
    assert m, mensagem(r)
    dados = artifact(r)
    assert dados == {
        "reserva": m.group(1),
        "sala": "sala-porao",
        "inicio": h("09:00"),
        "fim": h("10:00"),
        "responsavel": "Doc",
        "politica": "2026-11-01",
    }
    assert amb.a2a("GetTask", {"id": tarefa(r)["id"]}, id_=42)["id"] == 42  # id inteiro ecoado
    g = amb.a2a("GetTask", {"id": tarefa(r)["id"]})
    assert tarefa(g)["id"] == tarefa(r)["id"] and tarefa(g)["contextId"] == tarefa(r)["contextId"]
    assert estado(g) == "TASK_STATE_COMPLETED"


def test_agente_fala_mcp_na_ordem_certa_com_o_mesmo_trace_id_t19(amb: Ambiente) -> None:
    """AC-18/AC-19: tools/list antes do 1o tools/call; trace-id do A2A em TODOS os MCP da Task."""
    amb.enviar(pedido("sala-porao", "09:00", "10:00"), cabecalhos={"traceparent": TRACEPARENT})
    linhas = amb.mcp.requests()
    assert [linha["method"] for linha in linhas] == ["tools/list", "resources/read", "tools/call"]
    assert all((linha["traceparent"] or "").split("-")[1] == TRACE_ID for linha in linhas)
    spans = [linha["traceparent"].split("-")[2] for linha in linhas]
    assert len(set(spans)) == 3  # span-id novo por request
    assert len({linha["id"] for linha in linhas}) == 3
    assert {linha["client"] for linha in linhas} == {"agente-central-de-salas"}
    assert {linha["mcp_name"] for linha in linhas} == {None, "politica://uso", "reservar_sala"}

    # 2a Task, trace diferente: tools/list NAO se repete (cache do Client vivo); politica lida de novo
    outro = secrets.token_hex(16)
    amb.enviar(
        pedido("sala-porao", "11:00", "12:00"),
        cabecalhos={"traceparent": f"00-{outro}-{'a1' * 8}-01"},
    )
    novas = amb.mcp.requests()[3:]
    assert [linha["method"] for linha in novas] == ["resources/read", "tools/call"]
    assert all((linha["traceparent"] or "").split("-")[1] == outro for linha in novas)
    assert amb.agente.linhas_json(evento="mcp_cliente", acao="criado")[0]["criados"] == 1


def test_sem_sessao_no_fio_do_agente_t34(amb: Ambiente) -> None:
    amb.enviar(pedido("sala-porao", "09:00", "10:00"))
    assert amb.proxy.metodos() == ["tools/list", "resources/read", "tools/call"]
    assert set(amb.proxy.metodos_http) == {"POST"}
    for r in amb.proxy.requests:
        assert "mcp-session-id" not in r["headers"]
        assert (
            r["json"]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        )
        caps = r["json"]["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
        assert "form" in caps["elicitation"]


def test_request_sem_a2a_version_funciona_t40(amb: Ambiente) -> None:
    """O agente REAL aceita o request do validador (sem A2A-Version) e tambem 1.0 explicita."""
    r = amb.enviar(pedido("sala-porao", "09:00", "10:00"))
    assert estado(r) == "TASK_STATE_COMPLETED"
    r2 = amb.a2a(
        "SendMessage",
        {
            "message": {
                "messageId": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": pedido("sala-aquario", "09:00", "10:00")}],
            }
        },
        cabecalhos={"A2A-Version": "1.0"},
    )
    assert estado(r2) == "TASK_STATE_COMPLETED"


# ------------------------------------------------------------------------------ erros de execucao
def test_sala_inexistente_falha_com_a_mensagem_exata_no_status_e_no_historico(
    amb: Ambiente,
) -> None:
    r = amb.enviar(pedido("sala-delorean", "09:00", "10:00"))
    t = tarefa(r)
    assert estado(r) == "TASK_STATE_FAILED" and mensagem(r) == ERRO_SALA
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert t["history"][-1]["parts"] == [{"text": ERRO_SALA}]
    assert not t.get("artifacts")


@pytest.mark.parametrize(
    ("ini", "fim", "erro"),
    [
        ("07:00", "08:00", ERRO_JANELA),
        ("09:00", "12:00", ERRO_DURACAO),
        ("10:00", "09:00", ERRO_INTERVALO),
    ],
)
def test_erros_de_dominio_do_servidor_chegam_exatos(
    amb: Ambiente, ini: str, fim: str, erro: str
) -> None:
    """O agente nao decide nada: a mensagem vem do servidor MCP, sem prefixo nem moldura."""
    r = amb.enviar(pedido("sala-aquario", ini, fim))
    assert estado(r) == "TASK_STATE_FAILED" and mensagem(r) == erro


def test_pedido_invalido_nao_chega_ao_mcp(amb: Ambiente) -> None:
    r = amb.enviar("reservar sala=sala-aquario inicio=amanha fim=depois responsavel=Doc")
    assert estado(r) == "TASK_STATE_FAILED" and mensagem(r).startswith("Pedido invalido: ")
    assert amb.mcp.requests() == []  # nem tools/list: o MCP nao foi tocado


def test_determinismo_mesma_resposta_exceto_ids_t17(amb: Ambiente) -> None:
    def normal(r: dict[str, Any]) -> str:
        texto = json.dumps(r, sort_keys=True)
        texto = re.sub(
            r'"(id|messageId|taskId|contextId|artifactId|timestamp)": "[^"]*"', r'"\1": "_"', texto
        )
        return texto

    a = amb.enviar(pedido("sala-delorean", "09:00", "10:00"))
    b = amb.enviar(pedido("sala-delorean", "09:00", "10:00"))
    assert tarefa(a)["id"] != tarefa(b)["id"]
    assert normal(a) == normal(b)


def test_terminal_e_definitivo_no_agente_real(amb: Ambiente) -> None:
    r = amb.enviar(pedido("sala-porao", "09:00", "10:00"))
    tid = tarefa(r)["id"]
    r2 = amb.enviar("escolha=sala-mirante", task_id=tid)
    assert "error" in r2 and "result" not in r2
    assert estado(amb.a2a("GetTask", {"id": tid})) == "TASK_STATE_COMPLETED"
    assert len(amb.mcp.requests(method="tools/call")) == 1  # a mensagem recusada nao foi ao MCP


# ------------------------------------------------------------------------------ a pausa (E7a)
def test_conflito_pausa_e_nao_vaza_o_request_state_real(amb: Ambiente) -> None:
    """O requestState REAL que o servidor emitiu (visto no proxy) nao aparece em nenhuma resposta A2A
    nem em stdout/stderr do agente."""
    r = amb.enviar(
        pedido("sala-garagem", "14:00", "15:00", "Marty"), cabecalhos={"traceparent": TRACEPARENT}
    )
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem(r) == "alternativas: sala-fusca, sala-mirante"
    chamadas = [
        x["json"]
        for x in amb.proxy.respostas
        if x["json"] and (x["json"].get("result") or {}).get("resultType") == "input_required"
    ]
    assert len(chamadas) == 1
    request_state = chamadas[0]["result"]["requestState"]
    assert len(request_state) > 40
    g = amb.a2a("GetTask", {"id": tarefa(r)["id"]})
    lista = amb.a2a("ListTasks", {})
    for corpo in (r, g, lista):
        assert request_state[:40] not in json.dumps(corpo)
    time.sleep(0.5)  # a thread leitora do stderr e assincrona
    tudo = "\n".join(amb.agente.stderr + amb.agente.stdout)
    assert request_state[:40] not in tudo and request_state[-40:] not in tudo


# ------------------------------------------------------------------------------ R-ARQ-02
def test_mcp_fora_do_ar_falha_a_task_e_recupera_quando_volta(amb: Ambiente, segredo: str) -> None:
    """Servidor MCP cai e volta na mesma porta: a Task em curso termina com erro claro (nunca fica em
    WORKING) e a seguinte funciona (cliente recriado)."""
    ok = amb.enviar(pedido("sala-porao", "09:00", "10:00"))
    assert estado(ok) == "TASK_STATE_COMPLETED"
    porta = amb.mcp.porta
    amb.mcp.parar()

    caiu = amb.enviar(pedido("sala-porao", "11:00", "12:00"))
    assert estado(caiu) == "TASK_STATE_FAILED"
    assert "Nao foi possivel concluir a reserva: Servidor MCP" in mensagem(caiu)
    g = amb.a2a("GetTask", {"id": tarefa(caiu)["id"]})
    assert estado(g) == "TASK_STATE_FAILED"  # terminal, nao WORKING

    novo = ServidorMcpReal(segredo=segredo, porta=porta)
    try:
        novo.esperar_porta(porta)
        r = amb.enviar(pedido("sala-porao", "11:00", "12:00"))
        assert estado(r) == "TASK_STATE_COMPLETED", mensagem(r)
        metodos = [linha["method"] for linha in novo.requests()]
        assert (
            metodos[0] == "tools/list"
        )  # tools/list de novo antes do 1o tools/call do cliente novo
    finally:
        novo.parar()
    assert len(amb.agente.linhas_json(evento="mcp_cliente", acao="criado")) >= 2


def test_mcp_nunca_subiu_no_boot_do_agente_e_lazy(segredo: str) -> None:
    ag = AgenteReal(
        mcp_url=f"http://127.0.0.1:{porta_livre()}/mcp", env_extra={"MCP_TIMEOUT_S": "2"}
    )
    try:
        ag.esperar_porta(ag.porta)
        corpo = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m",
                    "role": "ROLE_USER",
                    "parts": [{"text": pedido("sala-porao", "09:00", "10:00")}],
                }
            },
        }
        r = httpx.post(ag.rpc_url, json=corpo, timeout=30).json()
        assert estado(r) == "TASK_STATE_FAILED" and "Servidor MCP" in mensagem(r)
    finally:
        ag.parar()
