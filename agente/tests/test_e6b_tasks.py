"""E6b: SendMessage bloqueante, FSM da Task, traducao MCP->A2A (com host MCP falso, sem rede)."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import Message, Part, Role, Task, TaskState, TaskStatus
from mcp_types import ElicitRequest, InputRequiredResult

from app.a2a import App
from app.mcp_host import McpHostError, McpProtocolError
from app.tasks import (
    ESTADOS_TERMINAIS,
    MSG_CONTINUACAO_PROVISORIA,
    MSG_ERRO_INTERNO,
    MSG_PAUSA_PROVISORIA,
    PausedRegistry,
    PausedState,
    artifact_da_reserva,
    reserva_do_resultado,
    resolver_context_id,
    texto_da_tool,
)

from .a2a_util import (
    HostFalso,
    app_com,
    artifact_json,
    cliente_asgi,
    config,
    enviar,
    estado,
    h,
    mensagem_de,
    mensagem_usuario,
    pedido,
    reserva_ok,
    rpc,
    tarefa,
    texto_resultado,
)

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SALA_INEXISTENTE = "Sala inexistente: sala-delorean"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ESTADO_OPACO = "v1.ESTADO-OPACO-DO-MCP-QUE-NUNCA-PODE-VAZAR-0123456789abcdef"


def sem_ids(obj: Any) -> Any:
    """Remove o que muda entre execucoes (ids e timestamps) para comparar respostas."""
    if isinstance(obj, dict):
        return {
            k: sem_ids(v)
            for k, v in obj.items()
            if k not in {"id", "messageId", "taskId", "contextId", "artifactId", "timestamp"}
        }
    if isinstance(obj, list):
        return [sem_ids(x) for x in obj]
    return obj


def conflito() -> InputRequiredResult:
    req = ElicitRequest(
        method="elicitation/create",
        params={  # type: ignore[arg-type]
            "mode": "form",
            "message": "escolha",
            "requestedSchema": {
                "type": "object",
                "properties": {"sala": {"enum": ["sala-fusca", "sala-mirante"]}},
            },
        },
    )
    return InputRequiredResult(input_requests={"chave": req}, request_state=ESTADO_OPACO)


# ------------------------------------------------------------------------------ fluxo feliz
async def test_sala_livre_completa_a_task_no_formato_do_wire_10() -> None:
    host = HostFalso([reserva_ok(reserva="res-0005", sala="sala-porao")])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(
            c, pedido("sala-porao", "09:00", "10:00"), cabecalhos={"traceparent": TRACEPARENT}
        )
    assert set(r) == {"jsonrpc", "id", "result"} and set(r["result"]) == {"task"}
    t = r["result"]["task"]
    assert re.fullmatch(r"task-[0-9a-f]{12}", t["id"]) and re.fullmatch(
        r"ctx-[0-9a-f]{12}", t["contextId"]
    )
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    msg = t["status"]["message"]
    assert (
        msg["role"] == "ROLE_AGENT"
        and msg["taskId"] == t["id"]
        and msg["contextId"] == t["contextId"]
    )
    assert re.fullmatch(r"msg-[0-9a-f]{12}", msg["messageId"])
    assert msg["parts"] == [{"text": "Reserva res-0005 confirmada na sala-porao."}]
    # history: mensagem do usuario + a do agente (wire 08/10)
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert t["history"][0]["parts"] == [{"text": pedido("sala-porao", "09:00", "10:00")}]
    assert t["history"][1]["parts"] == msg["parts"]
    # artifact `reserva`, JSON como no wire 10 (texto identico ao json.dumps padrao)
    (art,) = t["artifacts"]
    assert re.fullmatch(r"art-[0-9a-f]{12}", art.pop("artifactId"))
    esperado = {
        "reserva": "res-0005",
        "sala": "sala-porao",
        "inicio": "2026-11-03T09:00:00-03:00",
        "fim": "2026-11-03T10:00:00-03:00",
        "responsavel": "Doc",
        "politica": "2026-11-01",
    }
    assert art == {"name": "reserva", "parts": [{"text": json.dumps(esperado)}]}


async def test_sequencia_mcp_politica_e_depois_tool_com_argumentos_verbatim_e_mesmo_trace() -> None:
    host = HostFalso([reserva_ok()])
    texto = pedido("sala-porao", "09:00", "10:00", "Emmett  Brown")
    async with cliente_asgi(app_com(host)) as c:
        await enviar(c, texto, cabecalhos={"traceparent": TRACEPARENT})
    assert host.leituras_de_politica == [TRACE_ID]  # politica lida A CADA Task, no trace da Task
    (chamada,) = host.chamadas
    assert chamada["nome"] == "reservar_sala" and chamada["trace_id"] == TRACE_ID
    assert chamada["argumentos"] == {
        "sala": "sala-porao",
        "inicio": h("09:00"),
        "fim": h("10:00"),
        "responsavel": "Emmett  Brown",
    }
    assert chamada["input_responses"] is None and chamada["request_state"] is None


async def test_politica_do_artifact_vem_do_resource_e_nao_do_resultado_da_tool() -> None:
    host = HostFalso([reserva_ok()], versao="2027-02-02")
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
    assert artifact_json(r)["politica"] == "2027-02-02"


async def test_politica_e_lida_a_cada_task() -> None:
    host = HostFalso([reserva_ok(), reserva_ok(reserva="res-0004")])
    async with cliente_asgi(app_com(host)) as c:
        await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        await enviar(c, pedido("sala-porao", "11:00", "12:00"))
    assert len(host.leituras_de_politica) == 2


@pytest.mark.parametrize("nome", ['Doc"; DROP TABLE x', "O'Brien", "a\\b", "Zé <b>", "x=y"])
async def test_responsavel_com_aspas_gera_artifact_json_valido(nome: str) -> None:
    host = HostFalso([reserva_ok(responsavel=nome)])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00", nome))
    assert host.chamadas[0]["argumentos"]["responsavel"] == nome
    assert artifact_json(r)["responsavel"] == nome


# ------------------------------------------------------------------------------ erros da tool
@pytest.mark.parametrize(
    ("texto_erro", "pedido_texto"),
    [
        (SALA_INEXISTENTE, pedido("sala-delorean", "09:00", "10:00")),
        (ERRO_JANELA, pedido("sala-porao", "07:00", "08:00")),
        (
            "Duracao acima do limite: a politica permite no maximo 2 horas",
            pedido("sala-porao", "09:00", "12:00"),
        ),
    ],
)
async def test_iserror_da_tool_vira_failed_com_a_mensagem_exata_no_status_e_no_historico(
    texto_erro: str, pedido_texto: str
) -> None:
    host = HostFalso([texto_resultado(texto_erro, erro=True)])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido_texto)
    t = tarefa(r)
    assert t["status"]["state"] == "TASK_STATE_FAILED"
    assert t["status"]["message"]["parts"] == [
        {"text": texto_erro}
    ]  # exata: sem prefixo nem moldura
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert t["history"][-1]["parts"] == [{"text": texto_erro}]
    assert "artifacts" not in t or t["artifacts"] == []


async def test_resposta_ok_sem_reservado_nao_vira_completed() -> None:
    host = HostFalso([texto_resultado("qualquer coisa")])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
    assert estado(r) == "TASK_STATE_FAILED" and "inesperada" in mensagem_de(r)


# ------------------------------------------------------------------------------ pedido invalido
@pytest.mark.parametrize(
    "texto",
    [
        "quero uma sala",
        "reservar sala=sala-porao",
        f"reservar sala=s inicio=amanha fim={h('10:00')} responsavel=Doc",
        f"reservar sala=s inicio={h('09:00')} fim={h('10:00')} responsavel=Doc\nsala=sala-delorean",
        "escolha=sala-fusca",
    ],
)
async def test_pedido_invalido_falha_a_task_sem_chamar_o_mcp(texto: str) -> None:
    host = HostFalso([])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, texto)
    assert estado(r) == "TASK_STATE_FAILED"
    assert mensagem_de(r).startswith("Pedido invalido: ")
    assert host.chamadas == [] and host.leituras_de_politica == []  # nem a politica foi lida


# ------------------------------------------------------------------------------ falhas de infra MCP
@pytest.mark.parametrize(
    "erro",
    [
        McpHostError("timeout", "Servidor MCP nao respondeu a tempo em tools/call"),
        McpHostError("conexao", "Servidor MCP indisponivel em tools/call"),
        McpProtocolError(-32602, "Invalid or expired requestState"),
    ],
)
async def test_falha_do_mcp_termina_a_task_com_erro_claro_nunca_em_working(
    erro: McpHostError,
) -> None:
    host = HostFalso([erro])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        g = await rpc(c, "GetTask", {"id": tarefa(r)["id"]})
    assert estado(r) == "TASK_STATE_FAILED"
    assert mensagem_de(r) == f"Nao foi possivel concluir a reserva: {erro.mensagem}"
    assert g["result"]["status"]["state"] == "TASK_STATE_FAILED"


async def test_falha_ao_ler_a_politica_termina_a_task_sem_chamar_a_tool() -> None:
    host = HostFalso([])
    host.versao = McpHostError("conexao", "Servidor MCP indisponivel em resources/read")  # type: ignore[assignment]
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
    assert estado(r) == "TASK_STATE_FAILED" and "indisponivel" in mensagem_de(r)
    assert host.chamadas == []


async def test_excecao_inesperada_no_executor_falha_a_task_nunca_deixa_em_working() -> None:
    class Quebrado(HostFalso):
        async def versao_da_politica(self, trace_id: str | None = None) -> str:
            raise RuntimeError("bug interno com detalhes que nao devem vazar: /etc/segredo")

    app = app_com(Quebrado())
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        g = await rpc(c, "GetTask", {"id": tarefa(r)["id"]})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_ERRO_INTERNO
    assert "segredo" not in json.dumps(r) and g["result"]["status"]["state"] == "TASK_STATE_FAILED"


# ------------------------------------------------------------------------------ FSM
class TaskStoreEspiao(InMemoryTaskStore):
    """Registra cada estado salvo (a sequencia de transicoes da Task)."""

    def __init__(self) -> None:
        super().__init__()
        self.vistos: list[tuple[str, int]] = []

    async def save(self, task: Task, context: ServerCallContext) -> None:
        self.vistos.append((task.id, task.status.state))
        await super().save(task, context)


async def test_transicoes_submitted_working_terminal_e_getask_reflete_o_corrente() -> None:
    host = HostFalso([reserva_ok()])
    host.espera = asyncio.Event()
    loja = TaskStoreEspiao()
    app = App(config(), host, task_store=loja)
    try:
        async with cliente_asgi(app) as c:
            envio = asyncio.create_task(enviar(c, pedido("sala-porao", "09:00", "10:00")))
            await asyncio.wait_for(host.dentro.wait(), 10)  # o executor esta dentro do tools/call
            for _ in range(200):  # o consumidor de eventos do SDK persiste de forma assincrona
                if any(e == TaskState.TASK_STATE_WORKING for _, e in loja.vistos):
                    break
                await asyncio.sleep(0.02)
            (task_id,) = {t for t, _ in loja.vistos}
            durante = await asyncio.wait_for(rpc(c, "GetTask", {"id": task_id}), 10)
            assert (
                durante["result"]["status"]["state"] == "TASK_STATE_WORKING"
            )  # GetTask = corrente
            assert not envio.done()  # SendMessage e bloqueante
            host.espera.set()
            r = await asyncio.wait_for(envio, 10)
            depois = await rpc(c, "GetTask", {"id": task_id})
    finally:
        host.espera.set()
    assert estado(r) == "TASK_STATE_COMPLETED"
    assert depois["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    estados = [e for _, e in loja.vistos]
    assert estados[0] == TaskState.TASK_STATE_SUBMITTED
    assert estados[1] == TaskState.TASK_STATE_WORKING
    assert estados[-1] == TaskState.TASK_STATE_COMPLETED
    primeiro_terminal = next(i for i, e in enumerate(estados) if e in ESTADOS_TERMINAIS)
    assert primeiro_terminal == len(estados) - 1  # nada depois do terminal; nunca regride


async def _guardar(app: Any, task_id: str, estado_: TaskState) -> None:
    task = Task(
        id=task_id,
        context_id="ctx-t",
        status=TaskStatus(
            state=estado_,
            message=Message(
                message_id="msg-t", role=Role.ROLE_AGENT, parts=[Part(text="x")], task_id=task_id
            ),
        ),
    )
    await app.task_store.save(task, ServerCallContext())


@pytest.mark.parametrize(
    "terminal",
    [TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED],
)
async def test_sendmessage_para_task_terminal_e_erro_e_o_estado_nao_regride_t16(
    terminal: TaskState,
) -> None:
    host = HostFalso([])
    app = app_com(host)
    await _guardar(app, "task-final", terminal)
    async with cliente_asgi(app) as c:
        r = await enviar(c, "escolha=sala-mirante", task_id="task-final")
        g = await rpc(c, "GetTask", {"id": "task-final"})
    assert "result" not in r and r["error"]["code"] == -32602
    assert g["result"]["status"]["state"] == TaskState.Name(terminal)  # segue no mesmo terminal
    assert host.chamadas == [] and host.leituras_de_politica == []


async def test_sendmessage_para_task_de_verdade_ja_concluida_e_erro() -> None:
    host = HostFalso([reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        r1 = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        r2 = await enviar(c, "escolha=sala-mirante", task_id=tarefa(r1)["id"])
        r3 = await enviar(c, pedido("sala-porao", "09:00", "10:00"), task_id=tarefa(r1)["id"])
        g = await rpc(c, "GetTask", {"id": tarefa(r1)["id"]})
    assert "error" in r2 and "error" in r3
    assert g["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(host.chamadas) == 1  # so a 1a mensagem chegou ao MCP


async def test_sendmessage_para_task_inexistente_e_erro() -> None:
    async with cliente_asgi(app_com(HostFalso([]))) as c:
        r = await enviar(c, "escolha=x", task_id="task-que-nao-existe")
    assert r["error"]["code"] == -32001


# ------------------------------------------------------------------------------ determinismo / concorrencia / ids
async def test_mesmo_pedido_duas_vezes_gera_a_mesma_resposta_exceto_ids_t17() -> None:
    host = HostFalso(lambda _c: texto_resultado(SALA_INEXISTENTE, erro=True))
    async with cliente_asgi(app_com(host)) as c:
        a = await enviar(c, pedido("sala-delorean", "09:00", "10:00"), id_="1")
        b = await enviar(c, pedido("sala-delorean", "09:00", "10:00"), id_="2")
    assert tarefa(a)["id"] != tarefa(b)["id"] and tarefa(a)["contextId"] != tarefa(b)["contextId"]
    assert sem_ids(a) == sem_ids(b)


async def test_varias_tasks_concorrentes_sao_independentes() -> None:
    host = HostFalso(
        lambda c: reserva_ok(
            reserva=f"res-{c['argumentos']['responsavel']}",
            responsavel=c["argumentos"]["responsavel"],
        )
    )
    async with cliente_asgi(app_com(host)) as c:
        rs = await asyncio.gather(
            *[enviar(c, pedido("sala-porao", "09:00", "10:00", f"P{i}")) for i in range(8)]
        )
    assert len({tarefa(r)["id"] for r in rs}) == 8
    assert {estado(r) for r in rs} == {"TASK_STATE_COMPLETED"}
    assert {artifact_json(r)["responsavel"] for r in rs} == {f"P{i}" for i in range(8)}


@pytest.mark.parametrize("ident", ["abc", "0", 7, 0])
async def test_id_jsonrpc_string_ou_inteiro_no_sendmessage(ident: Any) -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"), id_=ident)
    assert r["id"] == ident and type(r["id"]) is type(ident)


# ------------------------------------------------------------------------------ traceparent (R-HOST-03)
async def test_traceparent_valido_do_a2a_vira_o_trace_da_task_no_mcp() -> None:
    host = HostFalso([reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        await enviar(
            c, pedido("sala-porao", "09:00", "10:00"), cabecalhos={"traceparent": TRACEPARENT}
        )
    assert host.leituras_de_politica == [TRACE_ID] and host.chamadas[0]["trace_id"] == TRACE_ID


@pytest.mark.parametrize(
    "ruim",
    ["lixo; drop table", TRACEPARENT.upper(), "00-" + "0" * 32 + "-00f067aa0ba902b7-01", ""],
)
async def test_traceparent_invalido_e_ignorado_e_gera_um_novo(ruim: str) -> None:
    host = HostFalso([reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(
            c, pedido("sala-porao", "09:00", "10:00"), cabecalhos={"traceparent": ruim}
        )
    assert estado(r) == "TASK_STATE_COMPLETED"  # nao quebra
    novo = host.chamadas[0]["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", novo) and novo != "0" * 32
    assert host.leituras_de_politica == [novo]  # o mesmo trace-id em TODOS os requests MCP da Task
    assert not ruim or ruim not in json.dumps(r)


async def test_sem_traceparent_cada_task_recebe_seu_proprio_trace() -> None:
    host = HostFalso([reserva_ok(), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        await enviar(c, pedido("sala-porao", "11:00", "12:00"))
    a, b = (x["trace_id"] for x in host.chamadas)
    assert a != b and re.fullmatch(r"[0-9a-f]{32}", a)


# ------------------------------------------------------------------------------ ponte provisoria (E7a)
async def test_pausa_provisoria_todo_e7a_conflito_falha_a_task_e_nao_vaza_o_request_state() -> None:
    """TODO(E7a): trocar por INPUT_REQUIRED + 'alternativas: ...'. Ate la, FAILED provisorio."""
    host = HostFalso([conflito()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        g = await rpc(c, "GetTask", {"id": tarefa(r)["id"]})
        lista = await rpc(c, "ListTasks", {})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_PAUSA_PROVISORIA
    for corpo in (r, g, lista):
        assert ESTADO_OPACO[:20] not in json.dumps(corpo)
    assert len(app.pausadas) == 0 and host.invocacoes_da_fachada == 0


def test_paused_registry_fica_fora_do_task_store_e_nao_expoe_o_estado_no_repr() -> None:
    reg = PausedRegistry()
    st = PausedState(
        task_id="task-1",
        context_id="ctx-1",
        tool_name="reservar_sala",
        original_arguments={"sala": "s"},
        input_request_key="k",
        enum=["a", "b"],
        request_state=ESTADO_OPACO,
        trace_id=TRACE_ID,
    )
    reg.guardar(st)
    assert reg.obter("task-1") is st and len(reg) == 1
    assert ESTADO_OPACO not in repr(st) and ESTADO_OPACO not in repr(reg.obter("task-1"))
    reg.limpar("task-1")
    reg.limpar("task-1")  # idempotente
    assert reg.obter("task-1") is None and len(reg) == 0


async def test_continuacao_provisoria_todo_e7b_de_task_nao_terminal_sem_estado_falha() -> None:
    """TODO(E7b): hoje nenhuma Task fica pausada; uma nao-terminal sem PausedState falha com clareza."""
    host = HostFalso([])
    app = app_com(host)
    await _guardar(app, "task-pausa", TaskState.TASK_STATE_INPUT_REQUIRED)
    async with cliente_asgi(app) as c:
        msg = mensagem_usuario("escolha=sala-fusca", "task-pausa", contextId="ctx-t")
        r = await rpc(c, "SendMessage", {"message": msg})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_CONTINUACAO_PROVISORIA
    assert host.chamadas == []


# ------------------------------------------------------------------------------ allowlist / vazamento
ALLOW_TASK = {"id", "contextId", "status", "history", "artifacts"}
ALLOW_STATUS = {"state", "message", "timestamp"}
ALLOW_MENSAGEM = {"messageId", "contextId", "taskId", "role", "parts"}
ALLOW_ARTIFACT = {"artifactId", "name", "parts"}


async def test_respostas_seguem_a_allowlist_de_campos() -> None:
    host = HostFalso([reserva_ok(), texto_resultado(SALA_INEXISTENTE, erro=True), conflito()])
    async with cliente_asgi(app_com(host)) as c:
        rs = [
            await enviar(c, pedido("sala-porao", "09:00", "10:00")),
            await enviar(c, pedido("sala-delorean", "09:00", "10:00")),
            await enviar(c, pedido("sala-garagem", "14:00", "15:00")),
        ]
        gs = [await rpc(c, "GetTask", {"id": tarefa(r)["id"]}) for r in rs]
    for resposta in rs + gs:
        t = tarefa(resposta)
        assert set(t) <= ALLOW_TASK, set(t) - ALLOW_TASK
        assert set(t["status"]) <= ALLOW_STATUS
        for m in [t["status"].get("message", {}), *t.get("history", [])]:
            assert set(m) <= ALLOW_MENSAGEM, set(m) - ALLOW_MENSAGEM
            assert all(set(p) == {"text"} for p in m.get("parts", []))
        for a in t.get("artifacts", []):
            assert set(a) <= ALLOW_ARTIFACT
        assert "metadata" not in json.dumps(resposta)  # nada de metadata/estado interno


# ------------------------------------------------------------------------------ puras
def test_texto_da_tool_e_exato() -> None:
    assert texto_da_tool(texto_resultado(SALA_INEXISTENTE, erro=True)) == SALA_INEXISTENTE


def test_reserva_do_resultado_rejeita_o_que_nao_e_reserva() -> None:
    assert reserva_do_resultado(reserva_ok()) is not None
    assert reserva_do_resultado(texto_resultado("oi")) is None
    assert reserva_do_resultado(texto_resultado('{"reservado": false, "reserva": null}')) is None
    assert reserva_do_resultado(texto_resultado('{"reservado": true}')) is None


def test_artifact_da_reserva_ordem_dos_campos_do_wire() -> None:
    dados = reserva_do_resultado(reserva_ok(reserva="res-9"))
    assert dados is not None
    texto = artifact_da_reserva(dados, "2026-11-01")
    assert list(json.loads(texto)) == [
        "reserva",
        "sala",
        "inicio",
        "fim",
        "responsavel",
        "politica",
    ]


def test_resolver_context_id_prefere_o_da_task_guardada_t41() -> None:
    """T-41 (unidade): na continuacao SEM contextId o SDK gera um id novo em context.context_id;
    o executor tem de usar o da Task guardada, senao: 'Context in event doesn't match TaskManager'."""
    guardada = SimpleNamespace(context_id="ctx-guardado")
    com_task = SimpleNamespace(current_task=guardada, context_id="ctx-recem-gerado-pelo-sdk")
    sem_task = SimpleNamespace(current_task=None, context_id="ctx-da-mensagem")
    vazia = SimpleNamespace(current_task=SimpleNamespace(context_id=""), context_id="ctx-x")
    assert resolver_context_id(com_task) == "ctx-guardado"  # type: ignore[arg-type]
    assert resolver_context_id(sem_task) == "ctx-da-mensagem"  # type: ignore[arg-type]
    assert resolver_context_id(vazia) == "ctx-x"  # type: ignore[arg-type]
