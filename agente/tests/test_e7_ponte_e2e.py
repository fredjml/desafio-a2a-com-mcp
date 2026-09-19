"""E7/E8 ponta a ponta: servidor MCP REAL + agente REAL (subprocess), o agente falando com o MCP por um
proxy gravador (a "verdade do fio").

Cobre R-BR-01..07, R-A2A-05/07, R-HOST-03, AC-18/19/28..33, T-11, T-13, T-14 (as duas ordens), T-15,
T-16, T-19, T-30, T-31, T-33, T-41, T-42, T-43, T-44, T-17 (a pausa) e R-ARQ-02 na retomada.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

import pytest

from .procs import ServidorMcpReal
from .test_e2e_agente import (  # noqa: F401  (a fixture `amb` e usada por nome)
    Ambiente,
    amb,
    artifact,
    estado,
    h,
    mensagem,
    pedido,
    tarefa,
)

TRACE_ID = secrets.token_hex(16)  # novo por execucao
TRACEPARENT = f"00-{TRACE_ID}-{secrets.token_hex(8)}-01"
OUTRO_TRACE = secrets.token_hex(16)
OUTRO_TRACEPARENT = f"00-{OUTRO_TRACE}-{secrets.token_hex(8)}-01"
LISTA_GARAGEM = "alternativas: sala-fusca, sala-mirante"
ERRO_SALA = "Sala inexistente: sala-inexistente"


class Sessao:
    """Envolve o `Ambiente`: grava o TEXTO BRUTO de toda resposta A2A (para o grep de vazamento)."""

    def __init__(self, ambiente: Ambiente) -> None:
        self.amb = ambiente
        self.brutos: list[str] = []
        original: Callable[..., Any] = ambiente.http.post

        def gravar(*args: Any, **kwargs: Any) -> Any:
            resposta = original(*args, **kwargs)
            self.brutos.append(resposta.text)
            return resposta

        setattr(ambiente.http, "post", gravar)  # noqa: B010

    def enviar(
        self, texto: str, task_id: str | None = None, traceparent: str | None = None
    ) -> dict[str, Any]:
        cab = {"traceparent": traceparent} if traceparent else None
        return self.amb.enviar(texto, task_id=task_id, cabecalhos=cab)

    def pausar(self, sala: str, ini: str, fim: str, resp: str, tp: str | None = None) -> str:
        r = self.enviar(pedido(sala, ini, fim, resp), traceparent=tp)
        assert estado(r) == "TASK_STATE_INPUT_REQUIRED", r
        return str(tarefa(r)["id"])

    def get(self, task_id: str) -> dict[str, Any]:
        return self.amb.a2a("GetTask", {"id": task_id})

    def estados_reais_do_mcp(self) -> list[str]:
        """Todos os requestState que o servidor REAL emitiu (lidos do proxy, so para o grep)."""
        achados = []
        for x in self.amb.proxy.respostas:
            corpo = x["json"] or {}
            rs = (corpo.get("result") or {}).get("requestState")
            if isinstance(rs, str):
                achados.append(rs)
        return achados


@pytest.fixture
def s(amb: Ambiente) -> Sessao:  # noqa: F811
    return Sessao(amb)


# ------------------------------------------------------------------------------ o fluxo do avaliador (passos 7-8)
def test_ponte_completa_pausa_e_continuacao_sem_contextid(s: Sessao) -> None:
    r = s.enviar(pedido("sala-garagem", "14:00", "15:00", "Marty"), traceparent=TRACEPARENT)
    t = tarefa(r)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and mensagem(r) == LISTA_GARAGEM
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]  # wire 08
    assert t["history"][1]["parts"] == [{"text": LISTA_GARAGEM}]
    g = s.get(t["id"])
    assert set(g["result"]) == {"task"} and g["result"]["task"] == t  # wire 09

    # continuacao: so taskId + texto (T-41): sem contextId
    r2 = s.enviar("escolha=sala-mirante", task_id=t["id"])
    t2 = tarefa(r2)
    assert estado(r2) == "TASK_STATE_COMPLETED" and t2["contextId"] == t["contextId"]
    m = re.fullmatch(r"Reserva (res-\d{4}) confirmada na sala-mirante\.", mensagem(r2))
    assert m, mensagem(r2)
    assert artifact(r2) == {
        "reserva": m.group(1),
        "sala": "sala-mirante",
        "inicio": h("14:00"),
        "fim": h("15:00"),
        "responsavel": "Marty",
        "politica": "2026-11-01",
    }
    assert [x["role"] for x in t2["history"]] == [
        "ROLE_USER",
        "ROLE_AGENT",
        "ROLE_USER",
        "ROLE_AGENT",
    ]
    assert len({x["messageId"] for x in t2["history"]}) == 4  # sem duplicatas
    assert {x["contextId"] for x in t2["history"]} == {t["contextId"]}
    assert s.get(t["id"])["result"]["task"] == t2


def test_o_fio_do_retry_id_novo_mesmos_argumentos_chave_e_estado_ecoados_t11(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    s.enviar("escolha=sala-mirante", task_id=tid)
    proxy = s.amb.proxy
    assert proxy.metodos() == [
        "tools/list",
        "resources/read",
        "tools/call",
        "resources/read",
        "tools/call",
    ]
    inicial, retry = (i for i, m in enumerate(proxy.metodos()) if m == "tools/call")
    j_ini, j_ret = proxy.requests[inicial]["json"], proxy.requests[retry]["json"]
    assert j_ini["id"] != j_ret["id"]  # id JSON-RPC NOVO
    assert j_ret["params"]["name"] == j_ini["params"]["name"] == "reservar_sala"
    assert j_ret["params"]["arguments"] == j_ini["params"]["arguments"]  # MESMOS arguments
    input_required = proxy.respostas[inicial]["json"]["result"]
    assert input_required["resultType"] == "input_required"
    (chave,) = input_required["inputRequests"]
    assert list(j_ret["params"]["inputResponses"]) == [chave]  # MESMA chave
    assert j_ret["params"]["inputResponses"][chave] == {
        "action": "accept",
        "content": {"sala": "sala-mirante"},
    }
    assert j_ret["params"]["requestState"] == input_required["requestState"]  # byte a byte
    assert "inputResponses" not in j_ini["params"] and "requestState" not in j_ini["params"]
    # headers coerentes com o corpo (senao -32020), tambem no retry
    for i in (inicial, retry):
        cab = proxy.requests[i]["headers"]
        assert cab["mcp-method"] == "tools/call" and cab["mcp-name"] == "reservar_sala"
        assert cab["mcp-protocol-version"] == "2026-07-28" and "mcp-session-id" not in cab


def test_stderr_do_servidor_tools_list_antes_do_call_e_par_de_calls_com_ids_diferentes_t43(
    s: Sessao,
) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    s.enviar("escolha=sala-mirante", task_id=tid)
    linhas = s.amb.mcp.requests()
    metodos = [x["method"] for x in linhas]
    assert metodos.index("tools/list") < metodos.index("tools/call")  # AC-18
    calls = [x for x in linhas if x["method"] == "tools/call"]
    assert len(calls) == 2 and calls[0]["id"] != calls[1]["id"]  # passo 6 do avaliador
    assert len({x["id"] for x in linhas}) == len(linhas)  # nenhum id repetido no processo


# ------------------------------------------------------------------------------ escolha invalida / recusa
@pytest.mark.parametrize(
    "valor",
    [
        "",
        "sala-xyz",
        "sala-aquario",
        "SALA-MIRANTE",
        "Sala-Mirante",
        "sala mirante",
        "sala-mirante,",
    ],
)
def test_escolha_invalida_repete_a_lista_identica_e_a_task_continua_t15(
    s: Sessao, valor: str
) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    chamadas_antes = len(s.amb.mcp.requests(method="tools/call"))
    for _ in range(2):
        r = s.enviar(f"escolha={valor}", task_id=tid)
        assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and mensagem(r) == LISTA_GARAGEM
    assert len(s.amb.mcp.requests(method="tools/call")) == chamadas_antes  # nada foi ao MCP
    ok = s.enviar("escolha=sala-fusca", task_id=tid)  # e a Task ainda vale
    assert estado(ok) == "TASK_STATE_COMPLETED" and artifact(ok)["sala"] == "sala-fusca"


def test_texto_que_nao_e_escolha_numa_task_pausada(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty")
    r = s.enviar("oi, tudo bem?", task_id=tid)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem(r).startswith("Resposta invalida: ") and mensagem(r).endswith(LISTA_GARAGEM)


def test_recusar_cancela_e_nao_reserva_nada_ac30(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:30", "15:30", "Biff", TRACEPARENT)
    r = s.enviar("escolha=recusar", task_id=tid)
    assert estado(r) == "TASK_STATE_CANCELED" and mensagem(r)
    assert not tarefa(r).get("artifacts")
    retry = s.amb.proxy.requests[-1]["json"]["params"]
    (resposta,) = retry["inputResponses"].values()
    assert resposta == {"action": "decline"}  # sem content
    # nenhuma reserva: o mesmo conflito volta a perguntar com as duas alternativas
    outra = s.enviar(pedido("sala-garagem", "14:30", "15:30", "Biff"))
    assert estado(outra) == "TASK_STATE_INPUT_REQUIRED" and mensagem(outra) == LISTA_GARAGEM
    # e o terminal e definitivo
    assert "error" in s.enviar("escolha=sala-mirante", task_id=tid)
    assert estado(s.get(tid)) == "TASK_STATE_CANCELED"


def test_sala_inexistente_e_uma_task_failed_com_a_mensagem_no_status_e_no_historico(
    s: Sessao,
) -> None:
    """Passo 10 do avaliador."""
    r = s.enviar(pedido("sala-inexistente", "09:00", "10:00", "Doc"))
    t = tarefa(r)
    assert estado(r) == "TASK_STATE_FAILED" and mensagem(r) == ERRO_SALA
    assert t["history"][-1]["parts"] == [{"text": ERRO_SALA}]


# ------------------------------------------------------------------------------ isolamento (T-14, check 33)
@pytest.mark.parametrize("ordem", ["a-depois-b", "b-depois-a"])
def test_duas_tasks_pausadas_concluem_cada_uma_com_a_sua_reserva_t14(s: Sessao, ordem: str) -> None:
    # A: fusca 16-17 (alternativas incluem sala-mirante) · B: garagem 14-15 (idem); mirante nos 2 horarios
    a = s.pausar("sala-fusca", "16:00", "17:00", "Lorraine")
    b = s.pausar("sala-garagem", "14:00", "15:00", "George")
    assert a != b
    estados_mcp = s.estados_reais_do_mcp()
    assert len(set(estados_mcp)) == 2  # o servidor selou 2 estados diferentes
    respostas: dict[str, dict[str, Any]] = {}
    for nome, tid in (("a", a), ("b", b)) if ordem == "a-depois-b" else (("b", b), ("a", a)):
        respostas[nome] = s.enviar("escolha=sala-mirante", task_id=tid)
    ra, rb = respostas["a"], respostas["b"]
    assert estado(ra) == estado(rb) == "TASK_STATE_COMPLETED", (mensagem(ra), mensagem(rb))
    da, db = artifact(ra), artifact(rb)
    assert (da["responsavel"], da["inicio"], da["sala"]) == ("Lorraine", h("16:00"), "sala-mirante")
    assert (db["responsavel"], db["inicio"], db["sala"]) == ("George", h("14:00"), "sala-mirante")
    assert da["reserva"] != db["reserva"]
    # cada retry levou o estado da SUA Task (1o retry = estado do que foi respondido primeiro)
    retries = [
        x["json"]["params"]
        for x in s.amb.proxy.requests
        if (x["json"] or {}).get("method") == "tools/call" and "requestState" in x["json"]["params"]
    ]
    assert {r["requestState"] for r in retries} == set(estados_mcp)
    for r in retries:
        assert r["arguments"]["responsavel"] in {"Lorraine", "George"}


def test_duas_tasks_disputando_a_mesma_sala_a_2a_nao_cria_reserva_sobreposta_t31_t42(
    s: Sessao,
) -> None:
    """A e B pausam em garagem 14-15 (fusca, mirante). A pega o mirante. Na retomada de B (mirante
    agora ocupado) o SERVIDOR revalida e re-pergunta (multi-rodada real, agora com `const`): o agente so
    reflete. Nenhuma reserva sobreposta e criada."""
    a = s.pausar("sala-garagem", "14:00", "15:00", "Marty")
    b = s.pausar("sala-garagem", "14:00", "15:00", "Biff")
    ra = s.enviar("escolha=sala-mirante", task_id=a)
    assert estado(ra) == "TASK_STATE_COMPLETED" and artifact(ra)["sala"] == "sala-mirante"

    rb = s.enviar("escolha=sala-mirante", task_id=b)  # alternativa obsoleta
    assert estado(rb) == "TASK_STATE_INPUT_REQUIRED"  # NAO concluiu, NAO reservou mirante de novo
    assert not tarefa(rb).get("artifacts")
    assert mensagem(rb) == "alternativas: sala-fusca"  # a lista NOVA (const, 1 alternativa) - T-30
    # a resposta antiga nao vale mais; a nova sim
    assert mensagem(s.enviar("escolha=sala-mirante", task_id=b)) == "alternativas: sala-fusca"
    rb2 = s.enviar("escolha=sala-fusca", task_id=b)
    assert estado(rb2) == "TASK_STATE_COMPLETED" and artifact(rb2)["sala"] == "sala-fusca"
    assert artifact(rb2)["reserva"] != artifact(ra)["reserva"]
    # rodada 2 usou chave e estado NOVOS (do servidor), byte a byte; os argumentos originais
    calls = [x for x in s.amb.proxy.requests if (x["json"] or {}).get("method") == "tools/call"]
    rodadas_b = [
        c["json"]["params"]
        for c in calls
        if c["json"]["params"]["arguments"]["responsavel"] == "Biff"
    ]
    assert len(rodadas_b) == 3  # pausa, retry (re-pergunta), retry final
    assert len({r["requestState"] for r in rodadas_b[1:]}) == 2  # estado da rodada 1 e da rodada 2
    assert len({json.dumps(r["arguments"], sort_keys=True) for r in rodadas_b}) == 1
    # ids JSON-RPC todos diferentes
    ids = [c["json"]["id"] for c in calls]
    assert len(set(ids)) == len(ids)


# ------------------------------------------------------------------------------ terminal definitivo (T-16)
def test_task_terminal_recusa_mensagens_e_nao_regride_t16(s: Sessao) -> None:
    concluida = s.pausar("sala-garagem", "14:00", "15:00", "Marty")
    assert estado(s.enviar("escolha=sala-mirante", task_id=concluida)) == "TASK_STATE_COMPLETED"
    cancelada = s.pausar("sala-garagem", "14:30", "15:30", "Biff")
    assert estado(s.enviar("escolha=recusar", task_id=cancelada)) == "TASK_STATE_CANCELED"
    falhada = tarefa(s.enviar(pedido("sala-inexistente", "09:00", "10:00")))["id"]
    esperado = {
        concluida: "TASK_STATE_COMPLETED",
        cancelada: "TASK_STATE_CANCELED",
        falhada: "TASK_STATE_FAILED",
    }
    calls_antes = len(s.amb.mcp.requests(method="tools/call"))
    for tid, est in esperado.items():
        for texto in ("escolha=sala-fusca", "escolha=recusar"):
            r = s.enviar(texto, task_id=tid)
            assert "result" not in r and r["error"]["code"] == -32602
        assert estado(s.get(tid)) == est
    assert len(s.amb.mcp.requests(method="tools/call")) == calls_antes


# ------------------------------------------------------------------------------ requestState nunca vaza (T-13 / T-44)
def test_request_state_e_segredo_ausentes_de_respostas_card_e_logs_t13_t44(
    s: Sessao, segredo: str
) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    outra = s.pausar("sala-fusca", "16:00", "17:00", "Doc")
    s.enviar("escolha=xyz", task_id=tid)
    s.enviar("escolha=sala-mirante", task_id=tid)
    s.enviar("escolha=recusar", task_id=outra)
    s.get(tid)
    s.get(outra)
    s.amb.a2a("ListTasks", {})
    s.amb.a2a("ListTasks", {"pageSize": 100})
    card = s.amb.http.get(s.amb.agente.base + "/.well-known/agent-card.json").text
    time.sleep(0.5)  # as threads leitoras do stderr sao assincronas

    estados = s.estados_reais_do_mcp()
    assert len(estados) >= 2 and all(len(e) > 40 for e in estados)
    saidas_a2a = "\n".join([*s.brutos, card])
    logs_agente = "\n".join(s.amb.agente.stderr + s.amb.agente.stdout)
    logs_mcp = "\n".join(s.amb.mcp.stderr + s.amb.mcp.stdout)
    for rs in estados:
        for trecho in (rs, rs[:40], rs[-40:]):
            assert trecho not in saidas_a2a, "requestState em resposta A2A ou card"
            assert trecho not in logs_agente, "requestState no log do agente"
            assert trecho not in logs_mcp, "requestState no log do servidor"
    for texto in (saidas_a2a, logs_agente, logs_mcp):
        assert not re.search(r"v1\.[A-Za-z0-9_-]{20,}", texto)  # nenhum estado selado
        assert not re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", texto)  # nenhum hex64
        assert segredo not in texto
    assert "inputRequests" not in saidas_a2a and "requestState" not in saidas_a2a
    assert "app.mrtr:escolha_de_sala" not in saidas_a2a  # nem a chave do pedido


def test_o_agente_nunca_invoca_o_callback_de_elicitation_e_declara_a_capability(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty")
    s.enviar("escolha=recusar", task_id=tid)
    for x in s.amb.proxy.requests:
        caps = x["json"]["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
        assert "form" in caps["elicitation"]
    assert s.amb.agente.linhas_json(evento="erro", codigo="fachada_invocada") == []


# ------------------------------------------------------------------------------ determinismo (T-17)
def test_a_mesma_pausa_duas_vezes_e_identica_byte_a_byte_t17(s: Sessao) -> None:
    def sem_ids(r: dict[str, Any]) -> str:
        t = json.dumps(r, sort_keys=True)
        return re.sub(
            r'"(id|messageId|taskId|contextId|artifactId|timestamp)": "[^"]*"', r'"\1": "_"', t
        )

    s.enviar(pedido("sala-porao", "09:00", "10:00", "Doc"))  # ocupa o porao 9-10
    a = s.enviar(pedido("sala-porao", "09:00", "10:00", "Doc"))
    b = s.enviar(pedido("sala-porao", "09:00", "10:00", "Doc"))
    assert estado(a) == estado(b) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem(a).encode() == mensagem(b).encode() and mensagem(a).startswith("alternativas:")
    assert sem_ids(a) == sem_ids(b)


# ------------------------------------------------------------------------------ R-ARQ-02 na retomada
def test_servidor_reiniciado_com_a_mesma_chave_o_estado_selado_sobrevive_e_a_task_termina(
    s: Sessao, segredo: str
) -> None:
    """Restart do MCP entre a pausa e a escolha, MESMO segredo: o estado selado segue valido. O agente
    termina a Task de forma definitiva (COMPLETED, ou FAILED com erro claro se a conexao obsoleta
    falhar antes): NUNCA fica em WORKING."""
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    porta = s.amb.mcp.porta
    s.amb.mcp.parar()
    novo = ServidorMcpReal(segredo=segredo, porta=porta)
    try:
        novo.esperar_porta(porta)
        r = s.enviar("escolha=sala-mirante", task_id=tid)
        assert estado(r) in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}, r
        if estado(r) == "TASK_STATE_COMPLETED":
            assert artifact(r)["sala"] == "sala-mirante" and artifact(r)["responsavel"] == "Marty"
            assert "tools/call" in [x["method"] for x in novo.requests()]
        else:
            assert "Servidor MCP" in mensagem(r) or "conexao" in mensagem(r)
        assert estado(s.get(tid)) == estado(r)  # terminal
        assert "error" in s.enviar("escolha=sala-mirante", task_id=tid)  # e definitivo
        # o agente segue util: uma Task nova pausa e conclui
        tid2 = s.pausar("sala-garagem", "14:00", "15:00", "Doc")
        ok = s.enviar("escolha=sala-fusca", task_id=tid2)
        assert estado(ok) == "TASK_STATE_COMPLETED", mensagem(ok)
    finally:
        novo.parar()


def test_servidor_reiniciado_com_outra_chave_rejeita_o_estado_e_a_task_falha_clara(
    s: Sessao,
) -> None:
    """Segredo DIFERENTE apos o restart: o servidor rejeita o estado (-32602); o agente falha a Task
    com a mensagem do servidor (sem o estado) e nao a deixa em WORKING."""
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT)
    porta = s.amb.mcp.porta
    s.amb.mcp.parar()
    novo = ServidorMcpReal(segredo=secrets.token_hex(32), porta=porta)
    try:
        novo.esperar_porta(porta)
        r = s.enviar("escolha=sala-mirante", task_id=tid)
        assert estado(r) == "TASK_STATE_FAILED", r
        assert mensagem(r).startswith("Nao foi possivel concluir a reserva: ")
        assert estado(s.get(tid)) == "TASK_STATE_FAILED"
        for rs in s.estados_reais_do_mcp():
            assert rs not in json.dumps(r)
    finally:
        novo.parar()
