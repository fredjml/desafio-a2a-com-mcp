"""E7a: a pausa (MCP `input_required` -> Task INPUT_REQUIRED + `alternativas: a, b, c`), com host falso.

Cobre R-BR-01/02, AC-28, T-13 (parte de unidade), T-17 (a pausa), T-30 (unidade: `const`) e o formato
do wire 08/09 (SendMessage e GetTask devolvem `result.task`).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from mcp_types import ElicitRequest, InputRequiredResult

from app.tasks import (
    MSG_PERGUNTA_INVALIDA,
    ExecutorReservas,
    extrair_pergunta,
    texto_alternativas,
)

from .a2a_util import (
    HostFalso,
    app_com,
    cliente_asgi,
    enviar,
    estado,
    mensagem_de,
    pedido,
    reserva_ok,
    rpc,
    tarefa,
)

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ESTADO_OPACO = "v1.ESTADO-OPACO-DO-MCP-QUE-NUNCA-PODE-VAZAR-0123456789abcdef"
CHAVE = "app.mrtr:escolha_de_sala"


def pergunta(
    propriedade: dict[str, Any] | None = None,
    *,
    chave: str = CHAVE,
    estado_: str | None = ESTADO_OPACO,
    modo: str = "form",
    campo: str = "sala",
) -> InputRequiredResult:
    """`InputRequiredResult` como o servidor emite (wire 03), com `enum`/`const` configuravel."""
    propriedade = propriedade or {
        "type": "string",
        "title": "Sala",
        "enum": ["sala-fusca", "sala-mirante"],
    }
    params: dict[str, Any] = {"mode": modo, "message": "escolha"}
    if modo == "form":
        params["requestedSchema"] = {
            "type": "object",
            "properties": {campo: propriedade},
            "required": [campo],
        }
    else:
        params["url"] = "https://exemplo.invalido/x"
        params["elicitationId"] = "e1"
    req = ElicitRequest(method="elicitation/create", params=params)  # type: ignore[arg-type]
    return InputRequiredResult(input_requests={chave: req}, request_state=estado_)


def executor_de(app: Any) -> ExecutorReservas:
    ex = app.executor
    assert isinstance(ex, ExecutorReservas)
    return ex


# ------------------------------------------------------------------------------ o wire 08
async def test_conflito_pausa_a_task_no_formato_do_wire_08() -> None:
    host = HostFalso([pergunta()])
    texto = pedido("sala-garagem", "14:00", "15:00", "Marty")
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, texto, cabecalhos={"traceparent": TRACEPARENT})
    assert set(r) == {"jsonrpc", "id", "result"} and set(r["result"]) == {"task"}
    t = r["result"]["task"]
    assert re.fullmatch(r"task-[0-9a-f]{12}", t["id"])
    assert re.fullmatch(r"ctx-[0-9a-f]{12}", t["contextId"])
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    msg = t["status"]["message"]
    assert msg["role"] == "ROLE_AGENT" and msg["taskId"] == t["id"]
    assert msg["contextId"] == t["contextId"]
    assert msg["parts"] == [{"text": "alternativas: sala-fusca, sala-mirante"}]  # EXATO
    # history [user, agent] com a mesma mensagem do status (wire 08)
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert t["history"][0]["parts"] == [{"text": texto}]
    assert t["history"][1] == msg
    assert not t.get("artifacts")
    # 1 tools/call; sem sessao/callback
    assert len(host.chamadas) == 1 and host.invocacoes_da_fachada == 0


async def test_gettask_da_task_pausada_tem_o_formato_do_wire_09() -> None:
    host = HostFalso([pergunta()])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        tid = tarefa(r)["id"]
        g = await rpc(c, "GetTask", {"id": tid}, id_=2)
    assert set(g) == {"jsonrpc", "id", "result"} and g["id"] == 2
    assert set(g["result"]) == {"task"}  # `result.task`, igual ao wire 09
    assert g["result"]["task"] == r["result"]["task"]  # a mesma Task que o SendMessage devolveu
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


# ------------------------------------------------------------------------------ opcoes: enum e const
@pytest.mark.parametrize(
    ("propriedade", "esperado"),
    [
        ({"enum": ["sala-fusca", "sala-mirante"]}, "alternativas: sala-fusca, sala-mirante"),
        ({"enum": ["sala-mirante", "sala-fusca"]}, "alternativas: sala-mirante, sala-fusca"),
        ({"enum": ["z", "a", "m"]}, "alternativas: z, a, m"),  # ordem do enum RECEBIDO, sem ordenar
        ({"const": "sala-mirante"}, "alternativas: sala-mirante"),  # T-30: 1 alternativa = const
        ({"type": "string", "const": "sala-fusca", "title": "Sala"}, "alternativas: sala-fusca"),
        ({"enum": ["so-uma"]}, "alternativas: so-uma"),
    ],
)
async def test_lista_vem_do_enum_ou_const_recebido_na_ordem_recebida_t30(
    propriedade: dict[str, Any], esperado: str
) -> None:
    host = HostFalso([pergunta(propriedade)])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and mensagem_de(r) == esperado
    guardado = executor_de(app).pausadas.obter(tarefa(r)["id"])
    assert guardado is not None and guardado.enum == esperado.removeprefix("alternativas: ").split(
        ", "
    )


async def test_o_agente_nao_calcula_alternativas_usa_o_que_o_servidor_mandou() -> None:
    """Salas que o agente nunca viu (nem existem nos dados) aparecem tal e qual: sem regra de sala."""
    host = HostFalso([pergunta({"enum": ["sala-que-so-o-servidor-conhece", "outra-inventada"]})])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
    assert mensagem_de(r) == "alternativas: sala-que-so-o-servidor-conhece, outra-inventada"


async def test_a_pausa_e_deterministica_byte_a_byte_t17() -> None:
    host = HostFalso([pergunta(), pergunta()])
    async with cliente_asgi(app_com(host)) as c:
        a = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        b = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
    assert tarefa(a)["id"] != tarefa(b)["id"]
    ma, mb = mensagem_de(a), mensagem_de(b)
    assert ma == mb and ma.startswith("alternativas:")
    assert ma.encode() == mb.encode()


# ------------------------------------------------------------------------------ PausedState
async def test_paused_state_guarda_tudo_fora_do_task_store_e_nao_e_exposto() -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    texto = pedido("sala-garagem", "14:00", "15:00", "Emmett  Brown")
    async with cliente_asgi(app) as c:
        r = await enviar(c, texto, cabecalhos={"traceparent": TRACEPARENT})
    t = tarefa(r)
    ps = executor_de(app).pausadas.obter(t["id"])
    assert ps is not None and app.pausadas is executor_de(app).pausadas
    assert ps.task_id == t["id"] and ps.context_id == t["contextId"]
    assert ps.tool_name == "reservar_sala"
    assert ps.original_arguments == host.chamadas[0]["argumentos"]  # exatamente como enviados
    assert ps.original_arguments["responsavel"] == "Emmett  Brown"
    assert ps.input_request_key == CHAVE and ps.campo == "sala"
    assert ps.enum == ["sala-fusca", "sala-mirante"]
    assert ps.request_state == ESTADO_OPACO  # opaco: guardado tal qual
    assert ps.trace_id == TRACE_ID
    assert ps.geracao_cliente == host.geracao
    assert ESTADO_OPACO not in repr(ps)
    # nao esta no TaskStore do SDK: a Task salva nao carrega o PausedState nem o estado
    salva = await app.task_store.get(t["id"], _contexto())
    assert salva is not None and ESTADO_OPACO not in str(salva)


def _contexto() -> Any:
    from a2a.server.context import ServerCallContext

    return ServerCallContext()


async def test_so_a_tarefa_pausada_ocupa_o_registro() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r1 = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        r2 = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
    assert len(app.pausadas) == 1
    assert app.pausadas.obter(tarefa(r1)["id"]) is not None
    assert app.pausadas.obter(tarefa(r2)["id"]) is None


# ------------------------------------------------------------------------------ requestState nunca vaza (T-13/T-44)
async def test_request_state_nao_aparece_em_nenhuma_resposta_nem_no_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r = await enviar(
            c, pedido("sala-garagem", "14:00", "15:00"), cabecalhos={"traceparent": TRACEPARENT}
        )
        tid = tarefa(r)["id"]
        g = await rpc(c, "GetTask", {"id": tid})
        lista = await rpc(c, "ListTasks", {})
        card = (await c.get("/.well-known/agent-card.json")).text
    saida = capsys.readouterr()
    tudo = json.dumps([r, g, lista]) + card + saida.err + saida.out
    assert ESTADO_OPACO not in tudo and ESTADO_OPACO[:20] not in tudo
    assert CHAVE not in tudo  # nem a chave de inputRequests
    assert '"ponte"' in saida.err  # o log da pausa existe (e nao tem o estado)


# ------------------------------------------------------------------------------ formatos inesperados
@pytest.mark.parametrize(
    "ruim",
    [
        pergunta(estado_=None),
        pergunta(estado_=""),
        pergunta({"enum": []}),
        pergunta({"type": "string"}),  # sem enum nem const
        pergunta({"enum": ["a", 3]}),
        pergunta({"enum": ["a", ""]}),
        pergunta({"const": 7}),
        pergunta(modo="url"),
        InputRequiredResult(input_requests=None, request_state=ESTADO_OPACO),
        InputRequiredResult(input_requests={}, request_state=ESTADO_OPACO),
    ],
)
async def test_pergunta_em_formato_inesperado_falha_a_task_sem_pausar(
    ruim: InputRequiredResult,
) -> None:
    host = HostFalso([ruim])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_PERGUNTA_INVALIDA
    assert len(app.pausadas) == 0
    assert ESTADO_OPACO not in json.dumps(r)


async def test_dois_pedidos_de_entrada_no_mesmo_resultado_nao_sao_suportados() -> None:
    um = pergunta()
    outro = pergunta(chave="outra")
    assert um.input_requests is not None and outro.input_requests is not None
    junto = InputRequiredResult(
        input_requests={**um.input_requests, **outro.input_requests}, request_state=ESTADO_OPACO
    )
    assert extrair_pergunta(junto) is None


def test_extrair_pergunta_usa_a_unica_propriedade_mesmo_com_outro_nome() -> None:
    p = extrair_pergunta(pergunta({"enum": ["x", "y"]}, campo="alternativa"))
    assert p is not None and p.campo == "alternativa" and p.opcoes == ["x", "y"]


def test_texto_alternativas_sem_ponto_final_nem_saudacao() -> None:
    assert texto_alternativas(["a", "b", "c"]) == "alternativas: a, b, c"
    assert texto_alternativas(["a"]) == "alternativas: a"
