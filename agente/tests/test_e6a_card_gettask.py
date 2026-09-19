"""E6a: agent card, roteamento JSON-RPC, A2A-Version ausente, GetTask, ids, config, log, trace."""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.types.a2a_pb2 import Message, Part, Role, Task, TaskState, TaskStatus

from app.a2a import App, criar_app, criar_card
from app.config import ConfigError, carregar_config
from app.log import campos_do_request, emitir, silenciar_ruido
from app.mcp_host import McpHost
from app.trace import novo_trace_id, trace_id_de, traceparent_para

from .a2a_util import (
    HostFalso,
    app_com,
    cliente_asgi,
    config,
    rpc,
)
from .procs import AgenteReal, porta_livre

FORK = Path(__file__).resolve().parents[2]
WIRE_07 = FORK / "exemplos" / "wire" / "07-a2a-agent-card.json"


def wire_card() -> dict[str, Any]:
    dados: dict[str, Any] = json.loads(WIRE_07.read_text(encoding="utf-8"))
    corpo: dict[str, Any] = dados["response"]["body"]
    return corpo


async def guardar_task(app: App, task_id: str, estado_: TaskState, texto: str = "ok") -> Task:
    """Injeta uma Task direto no TaskStore (o GetTask deve refleti-la)."""
    from a2a.server.context import ServerCallContext

    task = Task(
        id=task_id,
        context_id="ctx-teste",
        status=TaskStatus(
            state=estado_,
            message=Message(
                message_id="msg-x",
                role=Role.ROLE_AGENT,
                parts=[Part(text=texto)],
                task_id=task_id,
                context_id="ctx-teste",
            ),
        ),
    )
    await app.task_store.save(task, ServerCallContext())
    return task


# ------------------------------------------------------------------------------- card
async def test_card_e_identico_ao_wire_07() -> None:
    """Card IDENTICO ao wire 07 (com a porta 7300 e host 127.0.0.1 do wire)."""
    async with cliente_asgi(app_com(HostFalso())) as c:
        r = await c.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == wire_card()


async def test_card_nao_declara_seguranca_real_nem_capacidades_extras() -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
    assert "securitySchemes" not in card and "securityRequirements" not in card
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "extendedAgentCard": False,
    }
    (interface,) = card["supportedInterfaces"]
    assert interface["protocolBinding"] == "JSONRPC" and interface["protocolVersion"] == "1.0"
    assert [s["id"] for s in card["skills"]] == ["reservar-sala"]


@pytest.mark.parametrize(
    ("kw", "url"),
    [
        ({"a2a_port": 8123}, "http://127.0.0.1:8123/a2a"),
        ({"a2a_port": 7300, "card_host": "localhost"}, "http://localhost:7300/a2a"),
        ({"a2a_port": 7300, "card_host": "::1"}, "http://[::1]:7300/a2a"),
    ],
)
def test_url_do_card_deriva_de_porta_e_host(kw: dict[str, Any], url: str) -> None:
    card = criar_card(config(**kw))
    assert card.supported_interfaces[0].url == url


# ------------------------------------------------------------------------------- A2A-Version
async def test_request_sem_a2a_version_e_tratado_como_1_0_t40() -> None:
    """T-40: o validador nao manda A2A-Version; sem o context_builder seria -32009."""
    app = app_com(HostFalso())
    await guardar_task(app, "task-1", TaskState.TASK_STATE_WORKING)
    async with cliente_asgi(app) as c:
        resposta = await rpc(c, "GetTask", {"id": "task-1"})
    assert "error" not in resposta, resposta
    assert resposta["result"]["task"]["id"] == "task-1"


async def test_a2a_version_1_0_explicita_tambem_funciona() -> None:
    app = app_com(HostFalso())
    await guardar_task(app, "task-1", TaskState.TASK_STATE_WORKING)
    async with cliente_asgi(app) as c:
        resposta = await rpc(c, "GetTask", {"id": "task-1"}, cabecalhos={"A2A-Version": "1.0"})
    assert "error" not in resposta


async def test_versao_errada_continua_recusada() -> None:
    """O ajuste so preenche header AUSENTE: uma versao errada nao e mascarada."""
    app = app_com(HostFalso())
    await guardar_task(app, "task-1", TaskState.TASK_STATE_WORKING)
    async with cliente_asgi(app) as c:
        resposta = await rpc(c, "GetTask", {"id": "task-1"}, cabecalhos={"A2A-Version": "0.3"})
    assert resposta["error"]["code"] == -32009


# ------------------------------------------------------------------------------- GetTask
async def test_gettask_reflete_o_estado_corrente() -> None:
    app = app_com(HostFalso())
    await guardar_task(app, "task-7", TaskState.TASK_STATE_WORKING)
    async with cliente_asgi(app) as c:
        antes = await rpc(c, "GetTask", {"id": "task-7"})
        await guardar_task(app, "task-7", TaskState.TASK_STATE_COMPLETED, "Reserva res-1 pronta.")
        depois = await rpc(c, "GetTask", {"id": "task-7"})
    assert antes["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert depois["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert depois["result"]["task"]["contextId"] == "ctx-teste"
    assert depois["result"]["task"]["status"]["message"]["parts"] == [
        {"text": "Reserva res-1 pronta."}
    ]


async def test_gettask_inexistente_e_erro_bem_formado() -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        resposta = await rpc(c, "GetTask", {"id": "task-nao-existe"}, id_="abc")
    assert resposta["jsonrpc"] == "2.0" and resposta["id"] == "abc"
    assert resposta["error"]["code"] == -32001
    assert "result" not in resposta


@pytest.mark.parametrize("ident", ["abc-1", "0", 42, 0, "d6b77743258c"])
async def test_ids_string_e_inteiro_sao_ecoados_t26(ident: Any) -> None:
    app = app_com(HostFalso())
    await guardar_task(app, "task-1", TaskState.TASK_STATE_WORKING)
    async with cliente_asgi(app) as c:
        ok = await rpc(c, "GetTask", {"id": "task-1"}, id_=ident)
        erro = await rpc(c, "GetTask", {"id": "nao"}, id_=ident)
    assert ok["id"] == ident and type(ok["id"]) is type(ident)
    assert erro["id"] == ident and type(erro["id"]) is type(ident)


async def test_erros_jsonrpc_sao_bem_formados() -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        inexistente = await rpc(c, "MetodoQueNaoExiste", {})
        lixo = (
            await c.post("/a2a", content="{isso nao e json", headers={"A2A-Version": "1.0"})
        ).json()
        sem_params = await rpc(c, "GetTask", {})
    assert inexistente["error"]["code"] == -32601
    assert lixo["error"]["code"] == -32700 and lixo["id"] is None
    assert sem_params["error"]["code"] in (-32602, -32001)
    for r in (inexistente, lixo, sem_params):
        assert r["jsonrpc"] == "2.0" and "result" not in r


async def test_so_post_em_a2a_e_get_no_card() -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        assert (await c.get("/a2a")).status_code == 405
        assert (await c.post("/.well-known/agent-card.json")).status_code == 405
        assert (await c.get("/qualquer")).status_code == 404


# ------------------------------------------------------------------------------- boot lazy / processo real
def test_criar_app_nao_conecta_ao_mcp() -> None:
    app = criar_app(config(mcp_url="http://127.0.0.1:9/mcp"))
    assert isinstance(app.host, McpHost) and app.host.clientes_criados == 0  # lazy


def test_processo_sobe_e_serve_card_sem_o_mcp_no_ar() -> None:
    """R-HOST-01 (lazy): agente REAL em subprocess, MCP inexistente; card 200 e stderr limpo/JSON."""
    ag = AgenteReal(mcp_url=f"http://127.0.0.1:{porta_livre()}/mcp")
    try:
        ag.esperar_porta(ag.porta)
        r = httpx.get(f"{ag.base}/.well-known/agent-card.json", timeout=10)
        assert r.status_code == 200 and r.json()["name"] == "Central de Salas"
        # `localhost` (IPv6 primeiro no Windows) nao pode custar segundos: bind em 127.0.0.1 e ::1
        boot = ag.linhas_json(evento="boot")
        assert boot and boot[0]["porta"] == ag.porta and "127.0.0.1" in boot[0]["escutando"]
    finally:
        ag.parar()
    for linha in ag.stderr:
        json.loads(linha)  # todo o stderr e JSON-lines (sem ruido de OpenTelemetry)
    assert not any("opentelemetry" in linha.lower() for linha in ag.stderr)


# ------------------------------------------------------------------------------- config
def test_config_padroes() -> None:
    c = carregar_config({})
    assert (c.a2a_port, c.mcp_url, c.mcp_timeout_s) == (7300, "http://localhost:7301/mcp", 10.0)
    assert c.card_url == "http://127.0.0.1:7300/a2a"


def test_config_do_ambiente() -> None:
    c = carregar_config(
        {
            "A2A_PORT": "8000",
            "MCP_URL": "http://127.0.0.1:9000/mcp",
            "MCP_TIMEOUT_S": "2.5",
            "A2A_CARD_HOST": "meu-host",
        }
    )
    assert (c.a2a_port, c.mcp_url, c.mcp_timeout_s) == (8000, "http://127.0.0.1:9000/mcp", 2.5)
    assert c.card_url == "http://meu-host:8000/a2a"


@pytest.mark.parametrize(
    "env",
    [
        {"A2A_PORT": "0"},
        {"A2A_PORT": "70000"},
        {"A2A_PORT": "abc"},
        {"MCP_URL": "ftp://x/mcp"},
        {"MCP_URL": "localhost:7301"},
        {"MCP_TIMEOUT_S": "0"},
        {"MCP_TIMEOUT_S": "nan"},
        {"MCP_TIMEOUT_S": "inf"},
        {"MCP_TIMEOUT_S": "x"},
        {"A2A_CARD_HOST": "a/b"},
    ],
)
def test_config_invalida_falha_rapido(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        carregar_config(env)


# ------------------------------------------------------------------------------- trace
def test_trace_id_de_header_valido_e_invalido() -> None:
    ok = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert trace_id_de(ok) == "4bf92f3577b34da6a3ce929d0e0e4736"
    for ruim in (
        None,
        "",
        "lixo; drop table",
        ok.upper(),
        ok + "-x",
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",
        42,
    ):
        assert trace_id_de(ruim) is None, ruim


def test_traceparent_para_mantem_trace_id_e_troca_span() -> None:
    tid = novo_trace_id()
    tps = [traceparent_para(tid) for _ in range(20)]
    assert all(re.fullmatch(rf"00-{tid}-[0-9a-f]{{16}}-01", t) for t in tps)
    assert len({t.split("-")[2] for t in tps}) == 20
    assert all(trace_id_de(t) == tid for t in tps)
    with pytest.raises(ValueError):
        traceparent_para("nao-e-um-trace-id")


# ------------------------------------------------------------------------------- log
def test_emitir_descarta_campos_de_estado_e_segredo() -> None:
    buf = io.StringIO()
    with redirect_stderr(buf):
        emitir(
            "teste",
            request_state="v1.SEGREDO-OPACO",
            requestState="v1.SEGREDO-OPACO",
            input_responses={"a": 1},
            ok="visivel",
        )
    linha = json.loads(buf.getvalue())
    assert linha["ok"] == "visivel" and linha["proc"] == "agente"
    assert "SEGREDO-OPACO" not in buf.getvalue() and "request_state" not in linha


def test_campos_do_request_nao_logam_texto_da_mensagem() -> None:
    corpo = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "abc",
            "method": "SendMessage",
            "params": {"message": {"parts": [{"text": "responsavel=Fulano SEGURO"}]}},
        }
    ).encode()
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    campos = campos_do_request(corpo, {"traceparent": tp})
    assert campos["method"] == "SendMessage" and campos["id"] == "abc"
    assert campos["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "Fulano" not in json.dumps(campos)
    ruim = campos_do_request(b"{", {"traceparent": "lixo"})
    assert ruim["method"] is None and ruim["trace_id"] is None


async def test_middleware_registra_uma_linha_por_request() -> None:
    app = app_com(HostFalso())
    buf = io.StringIO()
    async with cliente_asgi(app) as c:
        with redirect_stderr(buf):
            await rpc(c, "GetTask", {"id": "nao"}, id_=7)
            await c.get("/.well-known/agent-card.json")
    linhas = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    reqs = [x for x in linhas if x["evento"] == "request"]
    assert [(x["http"], x["method"], x["status"]) for x in reqs] == [
        ("POST /a2a", "GetTask", 200),
        ("GET /.well-known/agent-card.json", None, 200),
    ]
    assert reqs[0]["id"] == 7 and reqs[0]["a2a_version"] is None  # o validador nao manda


def test_silenciar_ruido_baixa_o_nivel_dos_loggers() -> None:
    import logging

    silenciar_ruido()
    assert logging.getLogger("opentelemetry.context").level == logging.CRITICAL
    assert logging.getLogger("a2a").level == logging.ERROR
