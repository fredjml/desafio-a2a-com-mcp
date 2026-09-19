"""Correcoes dos reviews (codigo F-01, F-02, F-04, F-12, F-15) e do QA (contextId, texto vazio, role)."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, TaskState
from mcp_types import CallToolResult

from app.mcp_host import McpHost, McpHostError, McpProtocolError
from app.tasks import (
    MSG_CLIENTE_RECRIADO,
    MSG_ERRO_INTERNO,
    MSG_INTERROMPIDO,
    ExecutorReservas,
)

from .a2a_util import (
    HostFalso,
    app_com,
    cliente_asgi,
    enviar,
    estado,
    mensagem_de,
    mensagem_usuario,
    pedido,
    reserva_ok,
    rpc,
    tarefa,
    texto_resultado,
)
from .procs import ProxyGravador, ServidorMcpReal
from .test_e2e_agente import amb  # noqa: F401  (fixture usada por nome, via `s`)
from .test_e2e_agente import estado as estado_e2e
from .test_e2e_agente import mensagem as mensagem_e2e
from .test_e2e_agente import tarefa as tarefa_e2e
from .test_e7_ponte_e2e import Sessao, s  # noqa: F401
from .test_e7a_pausa import ESTADO_OPACO, pergunta
from .test_e7b_retomada import pausar

APP = Path(__file__).resolve().parents[1] / "app"


def args(sala: str, ini: str, fim: str, resp: str = "Marty") -> dict[str, Any]:
    return {
        "sala": sala,
        "inicio": f"2026-11-03T{ini}:00-03:00",
        "fim": f"2026-11-03T{fim}:00-03:00",
        "responsavel": resp,
    }


# ------------------------------------------------------------------------------ F-01
@pytest.mark.parametrize("tipo", ["conexao", "timeout"])
async def test_falha_de_transporte_na_continuacao_mantem_a_pausa_e_o_estado_intactos(
    tipo: str,
) -> None:
    host = HostFalso(
        [
            pergunta(),
            McpHostError(tipo, "Servidor MCP indisponivel em tools/call"),
            reserva_ok(sala="sala-mirante"),
        ]
    )
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        antes = app.pausadas.obter(tid)
        assert antes is not None
        r2 = await enviar(c, "escolha=sala-mirante", tid)
        depois = app.pausadas.obter(tid)
        g = await rpc(c, "GetTask", {"id": tid})
        r3 = await enviar(c, "escolha=sala-mirante", tid)  # o usuario reenvia
    assert estado(r2) == estado(g) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem_de(r2) == "Servidor MCP indisponivel; reenvie escolha=sala-mirante"
    assert depois is antes and depois.request_state == ESTADO_OPACO  # PausedState INTACTO
    assert ESTADO_OPACO not in json.dumps([r2, g])
    # o reenvio usa o MESMO estado e a MESMA chave, e conclui
    assert estado(r3) == "TASK_STATE_COMPLETED"
    retry_1, retry_2 = host.chamadas[1], host.chamadas[2]
    assert retry_1["request_state"] == retry_2["request_state"] == ESTADO_OPACO
    assert retry_1["input_responses"] == retry_2["input_responses"]
    assert len(app.pausadas) == 0  # terminal: agora sim limpa


async def test_falha_de_transporte_ao_ler_a_politica_na_retomada_tambem_mantem_a_pausa() -> None:
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.versao = McpHostError(
            "timeout", "Servidor MCP nao respondeu a tempo em resources/read"
        )  # type: ignore[assignment]
        r2 = await enviar(c, "escolha=sala-mirante", tid)
        host.versao = "2026-11-01"
        r3 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_INPUT_REQUIRED" and len(host.chamadas) == 2
    assert mensagem_de(r2).startswith("Servidor MCP indisponivel; reenvie escolha=")
    assert estado(r3) == "TASK_STATE_COMPLETED"


async def test_recusar_com_servidor_fora_do_ar_pede_para_reenviar_escolha_recusar() -> None:
    host = HostFalso(
        [pergunta(), McpHostError("conexao", "Servidor MCP indisponivel em tools/call")]
    )
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=recusar", tid)
    assert estado(r2) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem_de(r2) == "Servidor MCP indisponivel; reenvie escolha=recusar"
    assert app.pausadas.obter(tid) is not None


@pytest.mark.parametrize(
    "erro",
    [
        McpProtocolError(-32602, "Invalid or expired requestState"),
        McpHostError("ferramenta_ausente", "O servidor MCP nao oferece a ferramenta reservar_sala"),
        McpHostError("resposta_invalida", "Resposta MCP invalida em tools/call"),
    ],
    ids=["protocolo-32602", "ferramenta-ausente", "resposta-invalida"],
)
async def test_erro_de_protocolo_ou_logico_na_continuacao_continua_falhando_a_task(
    erro: McpHostError,
) -> None:
    host = HostFalso([pergunta(), erro])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_FAILED"
    assert mensagem_de(r2).startswith("Nao foi possivel concluir a reserva: ")
    assert len(app.pausadas) == 0


async def test_iserror_da_tool_na_continuacao_continua_falhando_a_task() -> None:
    host = HostFalso([pergunta(), texto_resultado("Sala inexistente: x", erro=True)])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_FAILED" and mensagem_de(r2) == "Sala inexistente: x"
    assert len(app.pausadas) == 0


async def test_falha_de_transporte_na_primeira_chamada_continua_falhando_a_task() -> None:
    host = HostFalso([McpHostError("conexao", "Servidor MCP indisponivel em tools/call")])
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
    assert estado(r) == "TASK_STATE_FAILED"


# ------------------------------------------------------------------------------ F-02 (executor)
async def test_client_recriado_com_ids_garantidos_envia_o_retry_e_conclui() -> None:
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.geracao = 2  # o Client MCP foi recriado desde a pausa
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_COMPLETED" and len(host.chamadas) == 2
    assert host.evitar_ids_chamadas == [(1, 3)]  # (geracao da pausa, maior id que ela usou)


async def test_client_recriado_sem_como_garantir_ids_nao_envia_o_retry() -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        host.geracao = 2
        host.ids_ok = False
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_FAILED" and mensagem_de(r2) == MSG_CLIENTE_RECRIADO
    assert len(host.chamadas) == 1


async def test_mesmo_client_nao_consulta_evitar_ids() -> None:
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        await enviar(c, "escolha=sala-mirante", tid)
    assert host.evitar_ids_chamadas == []


# ------------------------------------------------------------------------------ F-02 (host real)
async def test_erros_logicos_nao_recriam_o_client(
    servidor_mcp: ServidorMcpReal, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = McpHost(servidor_mcp.url, timeout_s=10)
    try:
        with pytest.raises(McpHostError) as ausente:
            await host.chamar_ferramenta("ferramenta_que_nao_existe", {})
        assert ausente.value.tipo == "ferramenta_ausente"
        monkeypatch.setattr("app.mcp_host.extrair_versao_politica", lambda _texto: None)
        with pytest.raises(McpHostError) as invalida:
            await host.versao_da_politica(None)
        assert invalida.value.tipo == "politica_invalida"
        monkeypatch.undo()
        assert host.clientes_criados == 1  # o Client saudavel nao foi descartado
        assert await host.versao_da_politica(None) == "2026-11-01"
        assert host.clientes_criados == 1
    finally:
        await host.aclose()


async def test_evitar_ids_apos_recriacao_o_retry_nunca_repete_o_id_da_chamada_inicial(
    servidor_mcp: ServidorMcpReal,
) -> None:
    """Client recriado entre a pausa e o retry: os ids recomecam em 1 (o inicial foi 3). O host "queima"
    ids ate passar do 3 e o retry sai com id diferente (F-01/F-02, id novo)."""
    px = ProxyGravador(servidor_mcp.url)
    host = McpHost(px.url, timeout_s=10)
    try:
        await host.versao_da_politica(None)
        r1 = await host.chamar_ferramenta("reservar_sala", args("sala-garagem", "14:00", "15:00"))
        geracao, ids_ate = host.geracao, host.ids_emitidos
        assert ids_ate == 3 and not isinstance(r1, CallToolResult)
        ciclo = host._ciclo  # o teste forca a recriacao
        assert ciclo is not None
        await host._invalidar(ciclo, "teste")
        await host.versao_da_politica(None)  # o Client novo: tools/list (1) + resources/read (2)
        assert host.geracao != geracao
        assert await host.evitar_ids(geracao, ids_ate) is True
        chave = next(iter(r1.input_requests or {}))
        from mcp_types import ElicitResult

        await host.chamar_ferramenta(
            "reservar_sala",
            args("sala-garagem", "14:00", "15:00"),
            input_responses={chave: ElicitResult(action="accept", content={"sala": "sala-fusca"})},
            request_state=str(r1.request_state),
        )
    finally:
        await host.aclose()
        px.parar()
    chamadas = [r["json"]["id"] for r in px.requests if r["json"]["method"] == "tools/call"]
    assert len(chamadas) == 2 and chamadas[0] != chamadas[1], chamadas
    assert chamadas[1] > 3


async def test_evitar_ids_no_mesmo_client_nao_envia_nada(servidor_mcp: ServidorMcpReal) -> None:
    px = ProxyGravador(servidor_mcp.url)
    host = McpHost(px.url, timeout_s=10)
    try:
        await host.versao_da_politica(None)
        enviados = len(px.requests)
        assert await host.evitar_ids(host.geracao, host.ids_emitidos) is True
        assert len(px.requests) == enviados
    finally:
        await host.aclose()
        px.parar()


# ------------------------------------------------------------------------------ F-04
async def test_bug_de_programacao_no_host_vira_erro_interno_com_traceback_no_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class HostComBug(HostFalso):
        async def chamar_ferramenta(self, *a: Any, **k: Any) -> Any:
            raise AttributeError(f"bug interno {ESTADO_OPACO}")

    host = HostComBug()
    async with cliente_asgi(app_com(host)) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
    saida = capsys.readouterr().err
    assert estado(r) == "TASK_STATE_FAILED" and mensagem_de(r) == MSG_ERRO_INTERNO
    assert "AttributeError" not in json.dumps(r) and "bug interno" not in json.dumps(r)
    assert '"evento": "erro"' in saida and "Traceback (most recent call last)" in saida
    assert "AttributeError" in saida


async def test_traceback_logado_nunca_contem_o_request_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = HostFalso([pergunta(), RuntimeError(f"falha com {ESTADO_OPACO} dentro")])  # type: ignore[list-item]

    async def chamar(nome: str, argumentos: dict[str, Any], **kw: Any) -> Any:
        resposta = host.respostas.pop(0)  # type: ignore[union-attr]
        if isinstance(resposta, RuntimeError):
            raise resposta
        return resposta

    host.chamar_ferramenta = chamar  # type: ignore[method-assign]
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    saida = capsys.readouterr().err
    assert estado(r2) == "TASK_STATE_FAILED" and mensagem_de(r2) == MSG_ERRO_INTERNO
    assert "RuntimeError" in saida and "<requestState omitido>" in saida
    assert ESTADO_OPACO not in saida and ESTADO_OPACO not in json.dumps(r2)


async def test_host_deixa_propagar_erro_de_programacao_sem_recriar_o_client(
    servidor_mcp: ServidorMcpReal, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = McpHost(servidor_mcp.url, timeout_s=10)

    def quebra(_texto: str) -> str:
        raise AttributeError("bug")

    try:
        await host.versao_da_politica(None)
        monkeypatch.setattr("app.mcp_host.extrair_versao_politica", quebra)
        with pytest.raises(AttributeError):  # NAO virou McpHostError("conexao", ...)
            await host.versao_da_politica(None)
        monkeypatch.undo()
        assert host.clientes_criados == 1
    finally:
        await host.aclose()


def test_estatico_host_sem_except_exception_largo() -> None:
    arvore = ast.parse((APP / "mcp_host.py").read_text(encoding="utf-8"))
    largos = [
        no.lineno
        for no in ast.walk(arvore)
        if isinstance(no, ast.ExceptHandler)
        and (no.type is None or ast.unparse(no.type) in {"Exception", "BaseException"})
    ]
    assert largos == []


# ------------------------------------------------------------------------------ F-12
async def test_abrir_com_timeout_cancela_a_tarefa_dona_e_traduz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dono: list[asyncio.Task[Any] | None] = []
    cancelado = asyncio.Event()

    async def dono_mudo(self: McpHost, ciclo: Any) -> None:
        dono.append(asyncio.current_task())
        try:
            await asyncio.sleep(3600)  # nunca sinaliza `pronto`
        except asyncio.CancelledError:
            cancelado.set()
            raise

    monkeypatch.setattr(McpHost, "_dono", dono_mudo)
    host = McpHost("http://127.0.0.1:9/mcp", timeout_s=0.2)
    with pytest.raises(McpHostError) as exc:
        await host.versao_da_politica(None)
    assert exc.value.tipo == "timeout"
    assert cancelado.is_set() and dono[0] is not None and dono[0].done()  # nao vazou


def test_estatico_um_so_timeout_efetivo() -> None:
    fonte = (APP / "mcp_host.py").read_text(encoding="utf-8")
    assert "read_timeout_seconds" not in fonte  # so o `asyncio.wait_for` do host


# ------------------------------------------------------------------------------ F-15
class FilaFalsa:
    def __init__(self, falha_apos: int | None = None) -> None:
        self.eventos: list[Any] = []
        self.falha_apos = falha_apos

    async def enqueue_event(self, evento: Any) -> None:
        if self.falha_apos is not None and len(self.eventos) >= self.falha_apos:
            raise RuntimeError("fila fechada")
        self.eventos.append(evento)


async def _contexto_de_continuacao(app: Any, tid: str) -> Any:
    from a2a.server.agent_execution import RequestContext

    atual = await app.task_store.get(tid, ServerCallContext())
    msg = Message(
        message_id="m-cancelavel",
        role=Role.ROLE_USER,
        parts=[Part(text="escolha=sala-mirante")],
        task_id=tid,
        context_id=atual.context_id,
    )
    return RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(message=msg),
        task_id=tid,
        context_id=atual.context_id,
        task=atual,
    )


def _estados_publicados(fila: FilaFalsa) -> list[Any]:
    return [
        e.status.state for e in fila.eventos if hasattr(e, "status") and hasattr(e.status, "state")
    ]


async def test_cancelamento_durante_a_continuacao_marca_a_task_como_failed_antes_de_repropagar() -> (
    None
):
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
    host.espera = asyncio.Event()  # o proximo tools/call fica bloqueado
    host.dentro.clear()
    executor = app.executor
    assert isinstance(executor, ExecutorReservas)
    fila = FilaFalsa()
    contexto = await _contexto_de_continuacao(app, tid)
    trabalho = asyncio.create_task(executor.execute(contexto, fila))  # type: ignore[arg-type]
    await asyncio.wait_for(host.dentro.wait(), 5)
    trabalho.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trabalho
    assert _estados_publicados(fila)[-1] == TaskState.TASK_STATE_FAILED
    ultimo = fila.eventos[-1]
    assert ultimo.status.message.parts[0].text == MSG_INTERROMPIDO
    assert len(app.pausadas) == 0  # a Task terminou (FAILED): o estado da ponte sai junto


async def test_cancelamento_sem_conseguir_publicar_preserva_o_paused_state() -> None:
    host = HostFalso([pergunta()])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
    host.espera = asyncio.Event()
    host.dentro.clear()
    executor = app.executor
    assert isinstance(executor, ExecutorReservas)
    # a fila aceita o Task/WORKING iniciais e falha depois (ex.: ja fechada pelo shutdown)
    fila = FilaFalsa(falha_apos=1)
    contexto = await _contexto_de_continuacao(app, tid)
    trabalho = asyncio.create_task(executor.execute(contexto, fila))  # type: ignore[arg-type]
    await asyncio.wait_for(host.dentro.wait(), 5)
    trabalho.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trabalho
    assert app.pausadas.obter(tid) is not None  # nao foi apagado: nada foi publicado


# ------------------------------------------------------------------------------ QA: contextId
async def test_context_id_divergente_na_continuacao_e_recusado_sem_tocar_no_estado() -> None:
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        estado_antes = app.pausadas.obter(tid)
        errado = await rpc(
            c,
            "SendMessage",
            {"message": mensagem_usuario("escolha=sala-mirante", tid, contextId="ctx-errado")},
        )
        g = await rpc(c, "GetTask", {"id": tid})
        contexto = tarefa(g)["contextId"]
        certo = await rpc(
            c,
            "SendMessage",
            {"message": mensagem_usuario("escolha=sala-mirante", tid, contextId=contexto)},
        )
    assert errado["error"]["code"] == -32602 and "contextId" in errado["error"]["message"]
    assert "result" not in errado
    assert app.pausadas.obter(tid) is None  # (concluiu abaixo)
    assert estado_antes is not None and len(host.chamadas) == 2  # so o inicial e o retry certo
    assert estado(g) == "TASK_STATE_INPUT_REQUIRED"
    assert estado(certo) == "TASK_STATE_COMPLETED"


async def test_context_id_igual_ao_da_task_ou_ausente_continua_aceito() -> None:
    host = HostFalso([pergunta(), pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        a = await pausar(c, host)
        b = await pausar(c, host, "sala-fusca", "16:00", "17:00")
        ctx_b = tarefa(await rpc(c, "GetTask", {"id": b}))["contextId"]
        ok = await rpc(
            c,
            "SendMessage",
            {"message": mensagem_usuario("escolha=sala-mirante", b, contextId=ctx_b)},
        )
        # usar o contextId da Task B na Task A tambem e recusado
        cruzado = await rpc(
            c,
            "SendMessage",
            {"message": mensagem_usuario("escolha=sala-mirante", a, contextId=ctx_b)},
        )
    assert estado(ok) == "TASK_STATE_COMPLETED"
    assert cruzado["error"]["code"] == -32602 and app.pausadas.obter(a) is not None


async def test_novo_pedido_com_context_id_proprio_cria_a_task_nesse_contexto() -> None:
    host = HostFalso([reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        r = await rpc(
            c,
            "SendMessage",
            {
                "message": mensagem_usuario(
                    pedido("sala-porao", "09:00", "10:00"), contextId="ctx-meu"
                )
            },
        )
    assert estado(r) == "TASK_STATE_COMPLETED" and tarefa(r)["contextId"] == "ctx-meu"


# ------------------------------------------------------------------------------ QA: mensagem vazia / role
@pytest.mark.parametrize(
    "parts",
    [[{"text": ""}], [{"text": "   "}], [{"data": {"a": 1}}]],
    ids=["texto-vazio", "so-espacos", "parte-sem-text"],
)
async def test_mensagem_sem_texto_util_vira_failed_com_mensagem_clara_sem_traceback(
    parts: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    host = HostFalso()
    msg = mensagem_usuario("x")
    msg["parts"] = parts
    async with cliente_asgi(app_com(host)) as c:
        r = await rpc(c, "SendMessage", {"message": msg})
        g = await rpc(c, "GetTask", {"id": tarefa(r)["id"]})
    assert "error" not in r and estado(r) == "TASK_STATE_FAILED"
    assert mensagem_de(r).startswith("Pedido invalido: texto vazio") and host.chamadas == []
    assert tarefa(r)["status"]["message"]["parts"] and estado(g) == "TASK_STATE_FAILED"
    saida = capsys.readouterr().err
    assert "Traceback" not in saida and "ValueError" not in saida


async def test_mensagem_sem_parts_e_erro_de_parametros_bem_formado(
    capsys: pytest.CaptureFixture[str],
) -> None:
    msg = mensagem_usuario("x")
    msg["parts"] = []
    async with cliente_asgi(app_com(HostFalso())) as c:
        r = await rpc(c, "SendMessage", {"message": msg})
    assert r["error"]["code"] == -32602 and "result" not in r
    assert "Traceback" not in capsys.readouterr().err


async def test_role_agent_no_primeiro_pedido_vira_failed_com_mensagem_clara(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = HostFalso()
    msg = mensagem_usuario(pedido("sala-porao", "09:00", "10:00"))
    msg["role"] = "ROLE_AGENT"
    async with cliente_asgi(app_com(host)) as c:
        r = await rpc(c, "SendMessage", {"message": msg})
    assert estado(r) == "TASK_STATE_FAILED" and "ROLE_USER" in mensagem_de(r)
    assert host.chamadas == []
    assert "Traceback" not in capsys.readouterr().err


async def test_role_agent_numa_task_pausada_mantem_a_pausa() -> None:
    host = HostFalso([pergunta(), reserva_ok(sala="sala-mirante")])
    app = app_com(host)
    async with cliente_asgi(app) as c:
        tid = await pausar(c, host)
        msg = mensagem_usuario("escolha=sala-mirante", tid)
        msg["role"] = "ROLE_AGENT"
        r = await rpc(c, "SendMessage", {"message": msg})
        depois = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED" and "ROLE_USER" in mensagem_de(r)
    assert len(host.chamadas) == 2 and estado(depois) == "TASK_STATE_COMPLETED"


# ------------------------------------------------------------------------------ e2e (processos reais)
def test_e2e_context_id_errado_e_recusado_e_a_escolha_certa_ainda_conclui(s: Sessao) -> None:  # noqa: F811
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty")
    ctx = tarefa_e2e(s.get(tid))["contextId"]
    msg = {
        "messageId": "msg-ctx-errado",
        "role": "ROLE_USER",
        "parts": [{"text": "escolha=sala-mirante"}],
        "taskId": tid,
        "contextId": "ctx-errado",
    }
    r = s.amb.a2a("SendMessage", {"message": msg})
    assert r["error"]["code"] == -32602 and "result" not in r
    assert estado_e2e(s.get(tid)) == "TASK_STATE_INPUT_REQUIRED"
    assert s.amb.proxy.metodos().count("tools/call") == 1  # o retry NAO foi enviado ao MCP
    assert "Traceback" not in "\n".join(s.amb.agente.stderr)
    ok = s.enviar("escolha=sala-mirante", task_id=tid)
    assert estado_e2e(ok) == "TASK_STATE_COMPLETED" and tarefa_e2e(ok)["contextId"] == ctx


def test_e2e_mcp_fora_do_ar_na_continuacao_mantem_a_pausa_e_reenviar_conclui_com_id_novo(
    s: Sessao,  # noqa: F811
    segredo: str,
) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", None)
    porta = s.amb.mcp.porta
    s.amb.mcp.parar()
    r = s.enviar("escolha=sala-mirante", task_id=tid)  # MCP morto: transporte
    assert estado_e2e(r) == "TASK_STATE_INPUT_REQUIRED"
    assert mensagem_e2e(r) == "Servidor MCP indisponivel; reenvie escolha=sala-mirante"
    assert estado_e2e(s.get(tid)) == "TASK_STATE_INPUT_REQUIRED"
    novo = ServidorMcpReal(segredo=segredo, porta=porta)  # volta, MESMA chave
    try:
        novo.esperar_porta(porta)
        r2 = s.enviar("escolha=sala-mirante", task_id=tid)
        assert estado_e2e(r2) == "TASK_STATE_COMPLETED", r2
        ids = [x["json"]["id"] for x in s.amb.proxy.requests if x["json"]["method"] == "tools/call"]
        assert len(ids) == len(set(ids)) >= 2, ids  # o retry NAO repete o id da chamada inicial
        retry = [x for x in s.amb.proxy.requests if x["json"]["method"] == "tools/call"][-1]
        inicial = next(x for x in s.amb.proxy.requests if x["json"]["method"] == "tools/call")
        assert retry["json"]["id"] != inicial["json"]["id"]
        assert retry["json"]["params"]["requestState"]  # o mesmo estado, ainda valido no servidor
        assert s.estados_reais_do_mcp()[0] == retry["json"]["params"]["requestState"]
    finally:
        novo.parar()
