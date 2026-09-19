"""E7b: retomada (`escolha=<v>` / `escolha=recusar`), multi-rodada, isolamento entre Tasks e traceparent
na continuacao, com host MCP falso (sem rede).

Cobre R-BR-03..07, R-A2A-05/07, R-HOST-03, AC-29..33, T-11 (ids: mesma chamada, estado ecoado),
T-14, T-15, T-16, T-30, T-33 (unidade), T-41, T-42 e T-44 (unidade).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Message, Part, Role, Task, TaskState, TaskStatus
from mcp_types import CallToolResult, ElicitResult, TextContent

from app.mcp_host import McpHostError, McpProtocolError
from app.tasks import (
    MSG_CLIENTE_RECRIADO,
    MSG_PERGUNTA_INVALIDA,
    MSG_RECUSADA,
    MSG_SEM_ESTADO,
    ExecutorReservas,
)

from .a2a_util import (
    HostFalso,
    app_com,
    artifact_json,
    cliente_asgi,
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
from .test_e7a_pausa import CHAVE, ESTADO_OPACO, TRACE_ID, TRACEPARENT, pergunta

LISTA = "alternativas: sala-fusca, sala-mirante"
SALA_DELOREAN = "Sala inexistente: sala-delorean"
RECUSADA_MCP = CallToolResult(
    content=[TextContent(text='{"reservado": false, "motivo": "recusado"}')],
    structured_content={"reservado": False, "motivo": "recusado", "reserva": None, "sala": None},
)


def executor_de(app: Any) -> ExecutorReservas:
    ex = app.executor
    assert isinstance(ex, ExecutorReservas)
    return ex


async def pausar(
    c: Any, host: HostFalso, sala: str = "sala-garagem", ini: str = "14:00", fim: str = "15:00"
) -> str:
    r = await enviar(c, pedido(sala, ini, fim, "Marty"), cabecalhos={"traceparent": TRACEPARENT})
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED", r
    return str(tarefa(r)["id"])


# ------------------------------------------------------------------------------ retomada feliz (wire 10)
async def test_escolha_valida_conclui_a_task_com_a_reserva_no_formato_do_wire_10() -> None:
    host = HostFalso(
        [
            pergunta(),
            reserva_ok(
                reserva="res-0005",
                sala="sala-mirante",
                inicio=h("14:00"),
                fim=h("15:00"),
                responsavel="Marty",
            ),
        ]
    )
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        assert len(app.pausadas) == 1
        # T-41: a continuacao do validador NAO manda contextId (so taskId)
        r = await enviar(c, "escolha=sala-mirante", tid)
        g = await rpc(c, "GetTask", {"id": tid})
    t = tarefa(r)
    assert set(r["result"]) == {"task"} and t["id"] == tid
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert t["status"]["message"]["parts"] == [
        {"text": "Reserva res-0005 confirmada na sala-mirante."}
    ]
    assert t["status"]["message"]["contextId"] == t["contextId"]  # o da Task guardada, nao um novo
    (art,) = t["artifacts"]
    assert art["name"] == "reserva"
    assert art["parts"] == [
        {
            "text": json.dumps(
                {
                    "reserva": "res-0005",
                    "sala": "sala-mirante",
                    "inicio": h("14:00"),
                    "fim": h("15:00"),
                    "responsavel": "Marty",
                    "politica": "2026-11-01",
                }
            )
        }
    ]
    # history como o wire 10: user, agent(alternativas), user(escolha), agent(reserva) — sem duplicatas
    textos = [(m["role"], m["parts"][0]["text"]) for m in t["history"]]
    assert textos == [
        ("ROLE_USER", pedido("sala-garagem", "14:00", "15:00", "Marty")),
        ("ROLE_AGENT", LISTA),
        ("ROLE_USER", "escolha=sala-mirante"),
        ("ROLE_AGENT", "Reserva res-0005 confirmada na sala-mirante."),
    ]
    assert {m["contextId"] for m in t["history"]} == {t["contextId"]}  # nenhum ctx "novo" do SDK
    assert len({m["messageId"] for m in t["history"]}) == 4
    assert g["result"]["task"] == t  # GetTask reflete o mesmo
    assert len(app.pausadas) == 0  # terminal limpa o PausedState
    assert host.invocacoes_da_fachada == 0


async def test_o_retry_repete_name_arguments_chave_e_estado_sem_modificar_t11() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        await enviar(c, "escolha=sala-mirante", tid)
    inicial, retry = host.chamadas
    assert retry["nome"] == inicial["nome"] == "reservar_sala"
    assert retry["argumentos"] == inicial["argumentos"]  # MESMOS arguments
    assert inicial["input_responses"] is None and inicial["request_state"] is None
    assert list(retry["input_responses"]) == [CHAVE]  # MESMA chave
    resposta = retry["input_responses"][CHAVE]
    assert resposta == ElicitResult(action="accept", content={"sala": "sala-mirante"})
    assert retry["request_state"] == ESTADO_OPACO  # byte a byte
    assert retry["trace_id"] == inicial["trace_id"] == TRACE_ID


async def test_politica_do_artifact_vem_do_resource_lido_nesta_task() -> None:
    host = HostFalso([pergunta(), reserva_ok()], versao="2027-05-05")
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-fusca", tid)
    assert artifact_json(r)["politica"] == "2027-05-05"
    assert host.leituras_de_politica == [TRACE_ID, TRACE_ID]  # 1 na Task nova, 1 na retomada


@pytest.mark.parametrize("valor", ["sala-fusca", "sala-mirante"])
async def test_qualquer_opcao_recebida_e_aceita(valor: str) -> None:
    host = HostFalso([pergunta(), reserva_ok(sala=valor)])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, f"escolha={valor}", tid)
    assert estado(r) == "TASK_STATE_COMPLETED"
    assert host.chamadas[1]["input_responses"][CHAVE].content == {"sala": valor}


async def test_escolha_com_crlf_final_e_tolerada() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-fusca\r\n", tid)
    assert estado(r) == "TASK_STATE_COMPLETED"


async def test_const_uma_alternativa_a_escolha_e_o_valor_unico_t30() -> None:
    host = HostFalso([pergunta({"const": "sala-mirante"}), reserva_ok(sala="sala-mirante")])
    async with cliente_asgi(app_com(host)) as c:
        r0 = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        assert mensagem_de(r0) == "alternativas: sala-mirante"
        tid = tarefa(r0)["id"]
        fora = await enviar(c, "escolha=sala-fusca", tid)
        assert estado(fora) == "TASK_STATE_INPUT_REQUIRED"
        assert mensagem_de(fora) == "alternativas: sala-mirante"
        r = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r) == "TASK_STATE_COMPLETED"
    assert host.chamadas[1]["input_responses"][CHAVE].content == {"sala": "sala-mirante"}


# ------------------------------------------------------------------------------ escolha invalida (T-15)
@pytest.mark.parametrize(
    "valor",
    [
        "",  # vazio
        "sala-xyz",
        "sala-aquario",  # sala real, mas nao oferecida (check 29)
        "SALA-MIRANTE",  # maiusculas
        "Sala-Mirante",
        "sala mirante",  # espaco
        "sala-mirante sala-fusca",
        "sala-mirante,sala-fusca",
        "sala-mirante;",
        "sala-",
    ],
)
async def test_escolha_fora_das_opcoes_mantem_a_pausa_e_repete_a_lista_identica_t15(
    valor: str,
) -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        antes = executor_de(app).pausadas.obter(tid)
        assert antes is not None
        copia = (
            antes.request_state,
            antes.input_request_key,
            list(antes.enum),
            dict(antes.original_arguments),
            antes.rodada,
        )
        respostas = [await enviar(c, f"escolha={valor}", tid) for _ in range(3)]
        g = await rpc(c, "GetTask", {"id": tid})
        # e uma escolha valida DEPOIS ainda funciona (a Task nunca morreu)
        final = await enviar(c, "escolha=sala-mirante", tid)
    for r in respostas:
        assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
        assert mensagem_de(r) == LISTA  # a MESMA lista, byte a byte
        assert mensagem_de(r).encode() == LISTA.encode()
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert len(host.chamadas) == 2  # so a pausa e (depois) a escolha valida: nenhuma chamada ao MCP
    depois = executor_de(app).pausadas.obter(tid)
    assert depois is None  # concluida no fim; antes disso o estado estava intacto (abaixo)
    assert estado(final) == "TASK_STATE_COMPLETED"
    assert copia[0] == ESTADO_OPACO and copia[4] == 1


async def test_escolha_invalida_deixa_o_paused_state_inalterado() -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        antes = executor_de(app).pausadas.obter(tid)
        assert antes is not None
        foto = dict(vars(antes))
        foto["enum"] = list(antes.enum)
        foto["original_arguments"] = dict(antes.original_arguments)
        for texto in ("escolha=", "escolha=xyz", "escolha=SALA-FUSCA", "oi", "reservar x"):
            await enviar(c, texto, tid)
    depois = executor_de(app).pausadas.obter(tid)
    assert depois is not None and dict(vars(depois)) == foto
    assert len(host.chamadas) == 1 and host.leituras_de_politica == [TRACE_ID]


@pytest.mark.parametrize(
    "texto", ["oi", "sala-fusca", "escolha", "escolha sala-fusca", "escolha=a\nb"]
)
async def test_texto_que_nao_e_escolha_numa_task_pausada_responde_claro_sem_chamar_o_mcp(
    texto: str,
) -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await enviar(c, texto, tid)
        g = await rpc(c, "GetTask", {"id": tid})
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
    msg = mensagem_de(r)
    assert msg.startswith("Resposta invalida: ") and "escolha=" in msg and msg.endswith(LISTA)
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert len(host.chamadas) == 1 and executor_de(app).pausadas.obter(tid) is not None


async def test_um_novo_pedido_reservar_numa_task_pausada_nao_reserva_nada() -> None:
    host = HostFalso([pergunta()])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"), tid)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and len(host.chamadas) == 1


# ------------------------------------------------------------------------------ recusar
async def test_recusar_envia_decline_sem_content_e_cancela_a_task() -> None:
    host = HostFalso([pergunta(), RECUSADA_MCP])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=recusar", tid)
        g = await rpc(c, "GetTask", {"id": tid})
        de_novo = await enviar(c, "escolha=sala-mirante", tid)
    retry = host.chamadas[1]
    resposta = retry["input_responses"][CHAVE]
    assert resposta.action == "decline" and resposta.content is None
    assert (
        retry["request_state"] == ESTADO_OPACO
        and retry["argumentos"] == host.chamadas[0]["argumentos"]
    )
    assert estado(r) == "TASK_STATE_CANCELED" and mensagem_de(r) == MSG_RECUSADA
    assert "artifacts" not in tarefa(r) or tarefa(r)["artifacts"] == []
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert len(app.pausadas) == 0
    assert "error" in de_novo and len(host.chamadas) == 2  # terminal e definitivo
    assert host.leituras_de_politica == [TRACE_ID]  # recusar nao precisa da politica


async def test_recusar_com_crlf_ou_espacos_nas_pontas_tambem_recusa() -> None:
    host = HostFalso([pergunta(), RECUSADA_MCP])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "  escolha=recusar \r\n", tid)
    assert estado(r) == "TASK_STATE_CANCELED"


# ------------------------------------------------------------------------------ erros no retry
async def test_iserror_no_retry_falha_a_task_com_a_mensagem_exata_e_limpa_o_estado() -> None:
    host = HostFalso(
        [pergunta(), texto_resultado("Sem alternativas disponiveis no intervalo", erro=True)]
    )
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-mirante", tid)
    t = tarefa(r)
    assert t["status"]["state"] == "TASK_STATE_FAILED"
    assert t["status"]["message"]["parts"] == [
        {"text": "Sem alternativas disponiveis no intervalo"}
    ]
    assert t["history"][-1]["parts"] == [{"text": "Sem alternativas disponiveis no intervalo"}]
    assert len(app.pausadas) == 0


@pytest.mark.parametrize(
    "erro",
    [
        McpProtocolError(-32602, "Invalid or expired requestState"),
        McpHostError("politica_invalida", "O recurso politica://uso nao traz 'versao:'"),
    ],
)
async def test_falha_do_mcp_no_retry_termina_a_task_com_erro_claro_nunca_em_working(
    erro: McpHostError,
) -> None:
    host = HostFalso([pergunta(), erro])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-mirante", tid)
        g = await rpc(c, "GetTask", {"id": tid})
    assert estado(r) == "TASK_STATE_FAILED"
    assert mensagem_de(r) == f"Nao foi possivel concluir a reserva: {erro.mensagem}"
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert len(app.pausadas) == 0
    assert ESTADO_OPACO not in json.dumps(r)


async def test_politica_invalida_na_retomada_falha_a_task_sem_chamar_a_tool() -> None:
    # (falha de TRANSPORTE ao ler a politica mantem a pausa: ver test_correcoes_robustez.py, F-01)
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.versao = McpHostError("politica_invalida", "O recurso nao traz 'versao:'")  # type: ignore[assignment]
        r = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r) == "TASK_STATE_FAILED" and "versao" in mensagem_de(r)
    assert len(host.chamadas) == 1 and len(app.pausadas) == 0


async def test_resposta_do_retry_que_nao_e_reserva_falha_a_task() -> None:
    host = HostFalso([pergunta(), texto_resultado("lixo")])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r) == "TASK_STATE_FAILED" and "inesperada" in mensagem_de(r)


async def test_cliente_mcp_recriado_sem_garantia_de_ids_falha_a_task_sem_enviar_o_retry() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.geracao += (
            1  # o Client foi recriado: os ids recomecam (risco de colidir com o inicial)
        )
        host.ids_ok = False  # e o host nao conseguiu garantir um id novo (`evitar_ids`)
        r = await enviar(c, "escolha=sala-mirante", tid)
        g = await rpc(c, "GetTask", {"id": tid})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_CLIENTE_RECRIADO
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert len(host.chamadas) == 1  # o retry NAO foi enviado
    assert len(app.pausadas) == 0


# ------------------------------------------------------------------------------ multi-rodada (T-42)
async def test_novo_input_required_no_retry_atualiza_o_estado_e_segue_pausada_r_br_07() -> None:
    novo_estado = "v1.OUTRO-ESTADO-OPACO-DA-RODADA-2-0123456789abcdefghij"
    host = HostFalso(
        [
            pergunta(),
            pergunta({"const": "sala-fusca"}, chave="app.mrtr:nova-chave", estado_=novo_estado),
            reserva_ok(sala="sala-fusca"),
        ]
    )
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid)  # o servidor re-pergunta
        assert estado(r2) == "TASK_STATE_INPUT_REQUIRED"
        assert mensagem_de(r2) == "alternativas: sala-fusca"  # a lista NOVA
        ps = executor_de(app).pausadas.obter(tid)
        assert ps is not None
        assert ps.input_request_key == "app.mrtr:nova-chave" and ps.enum == ["sala-fusca"]
        assert ps.request_state == novo_estado and ps.rodada == 2
        assert ps.original_arguments == host.chamadas[0]["argumentos"]  # os originais nao mudam
        # a escolha antiga ja nao vale (nao esta na lista nova); a nova vale
        velha = await enviar(c, "escolha=sala-mirante", tid)
        assert mensagem_de(velha) == "alternativas: sala-fusca"
        r3 = await enviar(c, "escolha=sala-fusca", tid)
    assert estado(r3) == "TASK_STATE_COMPLETED"
    rodada2 = host.chamadas[2]
    assert list(rodada2["input_responses"]) == ["app.mrtr:nova-chave"]  # a chave NOVA
    assert rodada2["request_state"] == novo_estado  # o estado NOVO, byte a byte
    assert rodada2["argumentos"] == host.chamadas[0]["argumentos"]
    assert len(host.chamadas) == 3
    assert novo_estado not in json.dumps([r2, velha, r3]) and ESTADO_OPACO not in json.dumps(r3)
    assert len(app.pausadas) == 0


async def test_nova_pergunta_ilegivel_no_retry_falha_a_task() -> None:
    host = HostFalso([pergunta(), pergunta({"type": "string"})])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_PERGUNTA_INVALIDA
    assert len(app.pausadas) == 0


async def test_recusar_e_de_novo_input_required_segue_pausada() -> None:
    """Se o servidor re-perguntar mesmo apos decline, a ponte nao presume o desfecho."""
    host = HostFalso([pergunta(), pergunta({"const": "sala-fusca"})])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r = await enviar(c, "escolha=recusar", tid)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and mensagem_de(r) == "alternativas: sala-fusca"


# ------------------------------------------------------------------------------ isolamento (T-14 / check 33)
def _host_por_estado() -> HostFalso:
    """Servidor de mentira sem memoria: o estado carrega o par (sala, inicio) da Task de origem."""

    def responder(ch: dict[str, Any]) -> Any:
        args = ch["argumentos"]
        selo = f"v1.SELO-{args['inicio']}-{args['responsavel']}"
        if ch["request_state"] is None:
            return pergunta({"enum": ["sala-fusca", "sala-mirante"]}, estado_=selo)
        assert ch["request_state"] == selo, "requestState trocado entre Tasks!"
        (resp,) = ch["input_responses"].values()
        return reserva_ok(
            reserva=f"res-{args['responsavel']}",
            sala=resp.content["sala"],
            inicio=args["inicio"],
            fim=args["fim"],
            responsavel=args["responsavel"],
        )

    return HostFalso(responder)


@pytest.mark.parametrize("ordem", ["a-depois-b", "b-depois-a"])
async def test_duas_tasks_pausadas_concluem_cada_uma_com_a_sua_reserva_t14(ordem: str) -> None:
    host = _host_por_estado()
    app = app_com(host)
    async with cliente_asgi(app) as c:
        ra = await enviar(c, pedido("sala-fusca", "16:00", "17:00", "Lorraine"))
        rb = await enviar(c, pedido("sala-garagem", "14:00", "15:00", "George"))
        a, b = tarefa(ra)["id"], tarefa(rb)["id"]
        assert a != b and len(app.pausadas) == 2
        # ambas escolhem a MESMA sala em horarios diferentes (como o validador)
        if ordem == "a-depois-b":
            fa = await enviar(c, "escolha=sala-mirante", a)
            fb = await enviar(c, "escolha=sala-mirante", b)
        else:
            fb = await enviar(c, "escolha=sala-mirante", b)
            fa = await enviar(c, "escolha=sala-mirante", a)
    assert estado(fa) == estado(fb) == "TASK_STATE_COMPLETED"
    da, db = artifact_json(fa), artifact_json(fb)
    assert da["reserva"] == "res-Lorraine" and da["inicio"] == h("16:00")
    assert db["reserva"] == "res-George" and db["inicio"] == h("14:00")
    assert da["reserva"] != db["reserva"] and da["inicio"] != db["inicio"]
    assert len(app.pausadas) == 0


async def test_duas_tasks_pausadas_intercaladas_com_respostas_invalidas_no_meio_t14() -> None:
    host = _host_por_estado()
    app = app_com(host)
    async with cliente_asgi(app) as c:
        a = tarefa(await enviar(c, pedido("sala-fusca", "16:00", "17:00", "Lorraine")))["id"]
        b = tarefa(await enviar(c, pedido("sala-garagem", "14:00", "15:00", "George")))["id"]
        await enviar(c, "escolha=xyz", a)
        rb = await enviar(c, "escolha=sala-fusca", b)
        ra = await enviar(c, "escolha=sala-mirante", a)
    assert (
        artifact_json(rb)["responsavel"] == "George" and artifact_json(rb)["sala"] == "sala-fusca"
    )
    assert artifact_json(ra)["responsavel"] == "Lorraine"


async def test_respostas_concorrentes_em_duas_tasks_nao_se_misturam() -> None:
    host = _host_por_estado()
    async with cliente_asgi(app_com(host)) as c:
        ids = [
            tarefa(
                await enviar(
                    c, pedido("sala-garagem", f"{9 + i:02d}:00", f"{10 + i:02d}:00", f"P{i}")
                )
            )["id"]
            for i in range(6)
        ]
        rs = await asyncio.gather(*[enviar(c, "escolha=sala-mirante", t) for t in ids])
    assert [artifact_json(r)["responsavel"] for r in rs] == [f"P{i}" for i in range(6)]
    assert [artifact_json(r)["inicio"] for r in rs] == [h(f"{9 + i:02d}:00") for i in range(6)]


# ------------------------------------------------------------------------------ terminal e definitivo (T-16)
@pytest.mark.parametrize(
    ("retry", "esperado"),
    [
        (reserva_ok(), "TASK_STATE_COMPLETED"),
        (RECUSADA_MCP, "TASK_STATE_CANCELED"),
        (texto_resultado(SALA_DELOREAN, erro=True), "TASK_STATE_FAILED"),
    ],
)
async def test_task_terminal_da_ponte_recusa_novas_mensagens_e_nao_regride_t16(
    retry: CallToolResult, esperado: str
) -> None:
    host = HostFalso([pergunta(), retry])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        escolha = "escolha=recusar" if esperado == "TASK_STATE_CANCELED" else "escolha=sala-mirante"
        r = await enviar(c, escolha, tid)
        assert estado(r) == esperado
        for texto in (
            "escolha=sala-fusca",
            "escolha=recusar",
            pedido("sala-porao", "09:00", "10:00"),
        ):
            erro = await enviar(c, texto, tid)
            assert "result" not in erro and erro["error"]["code"] == -32602
        g = await rpc(c, "GetTask", {"id": tid})
    assert g["result"]["task"]["status"]["state"] == esperado  # nunca voltou a WORKING
    assert len(host.chamadas) == 2  # as recusadas nao chegaram ao MCP
    assert len(app.pausadas) == 0


# ------------------------------------------------------------------------------ casos de borda do estado
async def test_continuacao_de_task_pausada_sem_estado_guardado_falha_com_clareza() -> None:
    host = HostFalso([])
    app = app_com(host)
    task = Task(
        id="task-sem-estado",
        context_id="ctx-t",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="msg-t",
                role=Role.ROLE_AGENT,
                parts=[Part(text="x")],
                task_id="task-sem-estado",
            ),
        ),
    )
    await app.task_store.save(task, ServerCallContext())
    async with cliente_asgi(app) as c:
        msg = mensagem_usuario("escolha=sala-fusca", "task-sem-estado", contextId="ctx-t")
        r = await rpc(c, "SendMessage", {"message": msg})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_SEM_ESTADO
    assert host.chamadas == []


async def test_mensagem_para_task_nao_terminal_sem_estado_de_ponte_falha_com_clareza() -> None:
    """Uma Task WORKING sem PausedState (orfa) nao tem o que continuar: falha clara, sem tocar o MCP."""
    host = HostFalso([])
    app = app_com(host)
    task = Task(
        id="task-orfa", context_id="ctx-t", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)
    )
    await app.task_store.save(task, ServerCallContext())
    async with cliente_asgi(app) as c:
        msg = mensagem_usuario("escolha=sala-fusca", "task-orfa", contextId="ctx-t")
        r = await rpc(c, "SendMessage", {"message": msg})
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_SEM_ESTADO
    assert host.chamadas == []


async def test_duas_escolhas_simultaneas_na_mesma_task_geram_um_unico_retry() -> None:
    """O SDK serializa as execucoes da mesma Task: a 2a ve a Task ja terminal. Nunca 2 retries."""
    host = HostFalso([pergunta(), reserva_ok(), reserva_ok(reserva="res-9999")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.espera = asyncio.Event()
        host.dentro.clear()
        primeira = asyncio.create_task(enviar(c, "escolha=sala-mirante", tid))
        await asyncio.wait_for(host.dentro.wait(), 10)  # o 1o retry esta dentro do MCP
        segunda = asyncio.create_task(enviar(c, "escolha=sala-fusca", tid))
        await asyncio.sleep(0.3)
        host.espera.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(primeira, segunda), 20)
        g = await rpc(c, "GetTask", {"id": tid})
    assert len(host.chamadas) == 2  # a pausa + UM retry: nunca dois
    assert estado(r1) == "TASK_STATE_COMPLETED"
    assert "error" in r2 or estado(r2) == "TASK_STATE_COMPLETED"  # recusada ou so o retrato final
    assert artifact_json(r1)["reserva"] == "res-0003"  # a do 1o retry
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"  # nada a regrediu
    assert "artifacts" in g["result"]["task"] and len(g["result"]["task"]["artifacts"]) == 1


async def test_cancel_task_limpa_o_estado_da_ponte() -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r = await rpc(c, "CancelTask", {"id": tid})
        g = await rpc(c, "GetTask", {"id": tid})
    assert "error" not in r
    assert g["result"]["task"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert len(app.pausadas) == 0


# ------------------------------------------------------------------------------ requestState (T-13 / T-44)
async def test_request_state_nao_vaza_em_nenhuma_resposta_da_retomada_nem_no_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    novo = "v1.SEGUNDO-ESTADO-OPACO-QUE-TAMBEM-NAO-PODE-VAZAR-abcdef0123"
    host = HostFalso([pergunta(), pergunta({"const": "sala-fusca"}, estado_=novo), reserva_ok()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid)
        r3 = await enviar(c, "escolha=sala-fusca", tid)
        g = await rpc(c, "GetTask", {"id": tid})
        lista = await rpc(c, "ListTasks", {})
        card = (await c.get("/.well-known/agent-card.json")).text
    saida = capsys.readouterr()
    tudo = json.dumps([r2, r3, g, lista]) + card + saida.err + saida.out
    for segredo in (ESTADO_OPACO, novo):
        assert segredo not in tudo and segredo[:20] not in tudo and segredo[-20:] not in tudo
    assert CHAVE not in tudo and "input_responses" not in tudo
