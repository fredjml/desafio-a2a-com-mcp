"""Correcoes dos reviews (seguranca M1/M2/M3/L2): protecao de entrada, ListTasks, retencao e log."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import Task, TaskState, TaskStatus
from starlette.types import Message

from app.a2a import App, TaskStoreCoerente
from app.config import ConfigError, carregar_config
from app.log import campos_do_request, emitir
from app.seguranca import (
    MAX_CORPO_BYTES,
    ProtecaoDeEntrada,
    host_sem_porta,
    origem_de_loopback,
)
from app.tasks import MSG_SEM_ESTADO, PausedRegistry

from .a2a_util import (
    HostFalso,
    app_com,
    cliente_asgi,
    config,
    enviar,
    estado,
    mensagem_de,
    mensagem_usuario,
    pedido,
    reserva_ok,
    rpc,
    tarefa,
)
from .procs import AgenteReal, porta_livre
from .test_e7a_pausa import pergunta

JSON_CT = {"Content-Type": "application/json"}


def corpo_send(texto: str | None = None) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": mensagem_usuario(texto or pedido("sala-porao", "09:00", "10:00"))
            },
        }
    )


# ------------------------------------------------------------------------------ M1: Content-Type
@pytest.mark.parametrize(
    "cabecalhos",
    [
        {"Content-Type": "text/plain;charset=UTF-8"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": "application/jsonx"},
        {"Content-Type": ""},
        {},
    ],
    ids=["text-plain", "form", "jsonx", "vazio", "ausente"],
)
async def test_post_sem_content_type_json_e_recusado_400_e_nao_executa(
    cabecalhos: dict[str, str],
) -> None:
    host = HostFalso()
    async with cliente_asgi(app_com(host)) as c:
        r = await c.post("/a2a", content=corpo_send(), headers=cabecalhos)
    assert r.status_code == 400 and "Content-Type" in r.text
    assert host.chamadas == []  # o pedido nao chegou ao executor


@pytest.mark.parametrize("ct", ["application/json", "Application/JSON; charset=utf-8"])
async def test_content_type_json_e_aceito(ct: str) -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await c.post("/a2a", content=corpo_send(), headers={"Content-Type": ct})
    assert r.status_code == 200 and estado(r.json()) == "TASK_STATE_COMPLETED"


async def test_get_do_card_continua_sem_content_type_e_outros_metodos_seguem_405() -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        assert (await c.get("/.well-known/agent-card.json")).status_code == 200
        assert (await c.get("/a2a")).status_code == 405
        assert (await c.post("/.well-known/agent-card.json")).status_code == 405


# ------------------------------------------------------------------------------ M1: Host
@pytest.mark.parametrize(
    "host",
    [
        "localhost:7300",
        "localhost",
        "LOCALHOST:7300",
        "127.0.0.1:7300",
        "127.0.0.1",
        "[::1]:7300",
        "[::1]",
    ],
)
async def test_hosts_de_loopback_sao_aceitos_com_ou_sem_porta(host: str) -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await c.post("/a2a", content=corpo_send(), headers={**JSON_CT, "Host": host})
        card = await c.get("/.well-known/agent-card.json", headers={"Host": host})
    assert r.status_code == 200 and card.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:7300",
        "localhost.evil.com",
        "localhost.evil.com:7300",
        "evil.com/localhost",
        "localhost@evil.com",
        "127.0.0.2:7300",
        "localhost:abc",
        "localhost:",
        "::1",
        "[::1",
    ],
)
async def test_host_fora_do_loopback_e_421_e_nao_executa(host: str) -> None:
    ha = HostFalso()
    async with cliente_asgi(app_com(ha)) as c:
        r = await c.post("/a2a", content=corpo_send(), headers={**JSON_CT, "Host": host})
        card = await c.get("/.well-known/agent-card.json", headers={"Host": host})
    assert r.status_code == 421 and card.status_code == 421
    assert ha.chamadas == []


async def test_host_configurado_em_a2a_card_host_e_aceito() -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]), card_host="agente.local")) as c:
        ok = await c.post(
            "/a2a", content=corpo_send(), headers={**JSON_CT, "Host": "agente.local:7300"}
        )
        outro = await c.post(
            "/a2a", content=corpo_send(), headers={**JSON_CT, "Host": "outro.local:7300"}
        )
    assert ok.status_code == 200 and outro.status_code == 421


async def test_host_ipv6_configurado_em_a2a_card_host_e_aceito() -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]), card_host="fe80::1")) as c:
        r = await c.post(
            "/a2a", content=corpo_send(), headers={**JSON_CT, "Host": "[fe80::1]:7300"}
        )
    assert r.status_code == 200


def test_host_sem_porta_normaliza_e_recusa_malformados() -> None:
    assert host_sem_porta("LocalHost:80") == "localhost"
    assert host_sem_porta("[::1]:80") == "[::1]"
    assert host_sem_porta("[::1]") == "[::1]"
    for ruim in ("", "  ", "localhost:", "localhost:x", "[::1", "[::1]x", "::1", "a:1:2"):
        assert host_sem_porta(ruim) is None


# ------------------------------------------------------------------------------ M1: Origin
@pytest.mark.parametrize(
    "origem",
    [
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:7300",
        "http://[::1]:8080",
        "https://localhost",
    ],
)
async def test_origin_de_loopback_e_aceito(origem: str) -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await c.post("/a2a", content=corpo_send(), headers={**JSON_CT, "Origin": origem})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "origem",
    [
        "http://evil.example",
        "https://evil.example:7300",
        "null",
        "http://localhost.evil.com",
        "http://localhost@evil.com",
        "http://evil.com:80@localhost",
        "ftp://localhost",
        "http://localhost:99999",
        "lixo",
    ],
)
async def test_origin_fora_do_loopback_e_403_e_nao_executa(origem: str) -> None:
    ha = HostFalso()
    async with cliente_asgi(app_com(ha)) as c:
        r = await c.post(
            "/a2a",
            content=corpo_send(),
            headers={**JSON_CT, "Origin": origem, "Host": "127.0.0.1:7300"},
        )
    assert r.status_code == 403 and ha.chamadas == []


def test_origem_de_loopback_unitario() -> None:
    assert origem_de_loopback("http://localhost:1")
    assert not origem_de_loopback("http://evil.example")
    assert not origem_de_loopback("")


async def test_pedido_como_o_do_validador_continua_funcionando() -> None:
    """urllib do validador: Host `localhost:7300`, sem Origin, Content-Type application/json."""
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await c.post(
            "/a2a",
            content=corpo_send(),
            headers={"Host": "localhost:7300", "Content-Type": "application/json"},
        )
    assert r.status_code == 200 and estado(r.json()) == "TASK_STATE_COMPLETED"


# ------------------------------------------------------------------------------ M2: limite de corpo
async def test_content_length_acima_do_limite_e_413_sem_executar() -> None:
    ha = HostFalso()
    grande = "x" * (MAX_CORPO_BYTES + 1)
    async with cliente_asgi(app_com(ha)) as c:
        r = await c.post("/a2a", content=grande, headers=JSON_CT)
    assert r.status_code == 413 and ha.chamadas == []


async def test_corpo_chunked_sem_content_length_acima_do_limite_e_413() -> None:
    async def pedacos() -> AsyncIterator[bytes]:
        for _ in range(6):  # 6 x 1 MiB
            yield b" " * (1024 * 1024)

    ha = HostFalso()
    async with cliente_asgi(app_com(ha)) as c:
        r = await c.post("/a2a", content=pedacos(), headers=JSON_CT)
    assert r.status_code == 413 and ha.chamadas == []


async def test_corpo_grande_mas_dentro_do_limite_segue_para_o_sdk() -> None:
    texto = "z" * (2 * 1024 * 1024)  # 2 MiB: o parser do agente recusa (> 1000), mas o corpo passou
    async with cliente_asgi(app_com(HostFalso())) as c:
        r = await c.post("/a2a", content=corpo_send(texto), headers=JSON_CT)
    assert r.status_code == 200 and estado(r.json()) == "TASK_STATE_FAILED"


async def test_middleware_para_de_ler_ao_passar_do_limite() -> None:
    lidas = 0

    async def app_qualquer(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError("nao deveria chegar ao app")

    async def receive() -> Message:
        nonlocal lidas
        lidas += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    enviadas: list[Message] = []

    async def send(msg: Message) -> None:
        enviadas.append(msg)

    escopo = {
        "type": "http",
        "method": "POST",
        "path": "/a2a",
        "headers": [(b"content-type", b"application/json"), (b"host", b"localhost")],
    }
    mw = ProtecaoDeEntrada(app_qualquer, caminhos_rpc=["/a2a"], max_corpo=10 * 1024)
    await mw(escopo, receive, send)
    assert lidas == 11  # 11 KiB: para logo no primeiro bloco que estoura, sem ler "para sempre"
    assert enviadas[0]["status"] == 413


# ------------------------------------------------------------------------------ M2: ListTasks
async def test_listtasks_esta_desabilitado_e_send_get_seguem_funcionando() -> None:
    async with cliente_asgi(app_com(HostFalso([reserva_ok()]))) as c:
        r = await enviar(c, pedido("sala-porao", "09:00", "10:00"))
        lista = await rpc(c, "ListTasks", {})
        lista_com_pagina = await rpc(c, "ListTasks", {"pageSize": 100, "historyLength": 10})
        g = await rpc(c, "GetTask", {"id": tarefa(r)["id"]})
    assert estado(r) == "TASK_STATE_COMPLETED" and estado(g) == "TASK_STATE_COMPLETED"
    for resposta in (lista, lista_com_pagina):
        assert resposta["error"]["code"] == -32601 and "result" not in resposta
        assert "task-" not in json.dumps(resposta)  # nada das Tasks existentes


# ------------------------------------------------------------------------------ M3: JSON hostil
@pytest.mark.parametrize(
    "corpo",
    ["[" * 100_000, '{"a":' * 100_000, "{isso nao e json", "", "\xff"],
    ids=["arrays", "objetos", "invalido", "vazio", "nao-utf8"],
)
async def test_json_invalido_ou_aninhado_devolve_32700_sem_traceback(
    corpo: str, capsys: pytest.CaptureFixture[str]
) -> None:
    async with cliente_asgi(app_com(HostFalso())) as c:
        r = await c.post("/a2a", content=corpo.encode("latin-1"), headers=JSON_CT)
        ok = await c.get("/.well-known/agent-card.json")
    assert r.status_code == 400
    erro = r.json()
    assert erro["error"]["code"] == -32700 and erro["id"] is None and erro["jsonrpc"] == "2.0"
    assert "recursion" not in r.text.lower() and "line 1" not in r.text  # sem texto interno
    assert ok.status_code == 200
    err = capsys.readouterr().err
    assert "Traceback" not in err and "RecursionError" not in err
    assert '"status": 400' in err  # o request continua auditado


@pytest.mark.parametrize("corpo", ["[" * 100_000, '{"a":' * 100_000], ids=["arrays", "objetos"])
def test_log_do_agente_nao_quebra_com_json_muito_aninhado(corpo: str) -> None:
    assert campos_do_request(corpo.encode(), {})["method"] is None


def test_processo_real_responde_32700_ao_json_aninhado_e_segue_vivo() -> None:
    ag = AgenteReal(mcp_url=f"http://127.0.0.1:{porta_livre()}/mcp")
    try:
        ag.esperar_porta(ag.porta)
        r = httpx.post(ag.rpc_url, content=b"[" * 100_000, headers=JSON_CT, timeout=15)
        assert r.status_code == 400 and r.json()["error"]["code"] == -32700
        assert httpx.get(f"{ag.base}/.well-known/agent-card.json", timeout=10).status_code == 200
        assert ag.proc.poll() is None
        linhas = ag.linhas_json(evento="request", status=400)
        assert linhas and linhas[0]["http"] == "POST /a2a"
        assert not any("Traceback" in linha or "RecursionError" in linha for linha in ag.stderr)
    finally:
        ag.parar()


def test_processo_real_recusa_host_origin_e_content_type() -> None:
    ag = AgenteReal(mcp_url=f"http://127.0.0.1:{porta_livre()}/mcp")
    try:
        ag.esperar_porta(ag.porta)
        corpo = corpo_send().encode()
        casos = {
            421: {**JSON_CT, "Host": "evil.example"},
            403: {**JSON_CT, "Origin": "http://evil.example"},
            400: {"Content-Type": "text/plain"},
        }
        for esperado, cabecalhos in casos.items():
            r = httpx.post(ag.rpc_url, content=corpo, headers=cabecalhos, timeout=15)
            assert r.status_code == esperado, (esperado, r.text)
    finally:
        ag.parar()


# ------------------------------------------------------------------------------ L2: log ASCII
def test_log_do_agente_escapa_separadores_de_linha_unicode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitir("teste", valor="a\u0085b\u2028c\u2029d\x1b[2Je\nf")
    saida = capsys.readouterr().err
    assert saida.isascii() and len(saida.splitlines()) == 1
    assert json.loads(saida)["valor"] == "a\u0085b\u2028c\u2029d\x1b[2Je\nf"


# ------------------------------------------------------------------------------ M2: retencao
def _task(task_id: str, estado_: TaskState) -> Task:
    return Task(id=task_id, context_id="ctx-x", status=TaskStatus(state=estado_))


async def test_teto_de_tasks_descarta_terminais_mais_antigas_primeiro() -> None:
    descartadas: list[str] = []
    loja = TaskStoreCoerente(max_tasks=3, ao_descartar=descartadas.append)
    ctx = ServerCallContext()
    await loja.save(_task("t1", TaskState.TASK_STATE_COMPLETED), ctx)
    await loja.save(_task("t2", TaskState.TASK_STATE_INPUT_REQUIRED), ctx)
    await loja.save(_task("t3", TaskState.TASK_STATE_FAILED), ctx)
    await loja.save(_task("t4", TaskState.TASK_STATE_WORKING), ctx)
    assert descartadas == ["t1"] and len(loja) == 3
    assert await loja.get("t1", ctx) is None
    for vivo in ("t2", "t3", "t4"):
        assert await loja.get(vivo, ctx) is not None
    await loja.save(_task("t5", TaskState.TASK_STATE_COMPLETED), ctx)
    assert descartadas == ["t1", "t3"]  # a terminal mais antiga; a pausa e a em execucao ficam


async def test_teto_sem_terminais_descarta_a_pausada_mais_antiga_e_nunca_a_em_execucao() -> None:
    descartadas: list[str] = []
    loja = TaskStoreCoerente(max_tasks=2, ao_descartar=descartadas.append)
    ctx = ServerCallContext()
    await loja.save(_task("w", TaskState.TASK_STATE_WORKING), ctx)
    await loja.save(_task("p1", TaskState.TASK_STATE_INPUT_REQUIRED), ctx)
    await loja.save(_task("p2", TaskState.TASK_STATE_INPUT_REQUIRED), ctx)
    assert descartadas == ["p1"]
    assert await loja.get("w", ctx) is not None and await loja.get("p2", ctx) is not None


async def test_task_que_acabou_de_ser_salva_nunca_e_descartada() -> None:
    loja = TaskStoreCoerente(max_tasks=1)
    ctx = ServerCallContext()
    await loja.save(_task("a", TaskState.TASK_STATE_INPUT_REQUIRED), ctx)
    await loja.save(_task("b", TaskState.TASK_STATE_WORKING), ctx)
    await loja.save(_task("a", TaskState.TASK_STATE_COMPLETED), ctx)  # a pausada agora concluiu
    assert await loja.get("a", ctx) is not None


async def test_fluxo_completo_respeita_o_teto_e_limpa_o_estado_da_pausa_descartada() -> None:
    host = HostFalso([pergunta(), pergunta(), pergunta()])
    app = app_com(host, max_tasks=2)
    async with cliente_asgi(app) as c:
        ids = []
        for hora in ("09:00", "10:00", "11:00"):
            r = await enviar(c, pedido("sala-garagem", hora, f"{int(hora[:2]) + 1}:00"))
            assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
            ids.append(tarefa(r)["id"])
        assert len(app.pausadas) == 2  # a pausa da Task mais antiga foi descartada junto
        velha = await rpc(c, "GetTask", {"id": ids[0]})
        recente = await rpc(c, "GetTask", {"id": ids[2]})
    assert velha["error"]["code"] == -32001 and estado(recente) == "TASK_STATE_INPUT_REQUIRED"


class Relogio:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_pausa_expirada_e_purgada_e_a_continuacao_falha_com_mensagem_clara() -> None:
    relogio = Relogio()
    host = HostFalso([pergunta()])
    app = App(config(), host, pausadas=PausedRegistry(ttl_s=660.0, relogio=relogio))
    async with cliente_asgi(app) as c:
        r = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        tid = tarefa(r)["id"]
        assert len(app.pausadas) == 1
        relogio.t += 661.0  # passou o TTL do requestState + margem
        r2 = await enviar(c, "escolha=sala-mirante", tid)
    assert estado(r2) == "TASK_STATE_FAILED"
    assert mensagem_de(r2) == MSG_SEM_ESTADO and "reenvie o pedido" in MSG_SEM_ESTADO
    assert len(host.chamadas) == 1  # o retry NAO foi enviado ao MCP (o estado ja nao vale)
    assert len(app.pausadas) == 0


async def test_varredura_preguicosa_purga_pausas_vencidas_a_cada_sendmessage() -> None:
    relogio = Relogio()
    host = HostFalso([pergunta(), reserva_ok()])
    app = App(config(), host, pausadas=PausedRegistry(ttl_s=100.0, relogio=relogio))
    async with cliente_asgi(app) as c:
        await enviar(c, pedido("sala-garagem", "14:00", "15:00"))
        assert len(app.pausadas) == 1
        relogio.t += 101.0
        await enviar(c, pedido("sala-porao", "09:00", "10:00"))  # outra Task dispara a varredura
        assert len(app.pausadas) == 0


async def test_duas_tasks_pausadas_dentro_do_prazo_continuam_independentes() -> None:
    relogio = Relogio()
    host = HostFalso([pergunta(), pergunta(), reserva_ok(sala="sala-mirante")])
    app = App(config(), host, pausadas=PausedRegistry(ttl_s=660.0, relogio=relogio))
    async with cliente_asgi(app) as c:
        a = tarefa(await enviar(c, pedido("sala-garagem", "14:00", "15:00")))["id"]
        relogio.t += 300.0
        b = tarefa(await enviar(c, pedido("sala-fusca", "16:00", "17:00")))["id"]
        relogio.t += 400.0  # A tem 700 s (vencida); B tem 400 s (valida)
        ra = await enviar(c, "escolha=sala-mirante", a)
        rb = await enviar(c, "escolha=sala-mirante", b)
    assert estado(ra) == "TASK_STATE_FAILED" and mensagem_de(ra) == MSG_SEM_ESTADO
    assert estado(rb) == "TASK_STATE_COMPLETED"


def test_paused_registry_renova_o_prazo_a_cada_guardar() -> None:
    from app.tasks import PausedState

    relogio = Relogio()
    reg = PausedRegistry(ttl_s=10.0, relogio=relogio)
    estado_ = PausedState(
        task_id="t",
        context_id="c",
        tool_name="reservar_sala",
        original_arguments={},
        input_request_key="k",
        enum=["a"],
        request_state="opaco",
        trace_id="x",
    )
    reg.guardar(estado_)
    relogio.t += 8
    reg.guardar(estado_)  # nova rodada: o servidor emitiu um requestState novo
    relogio.t += 8
    assert reg.obter("t") is not None
    relogio.t += 3
    assert reg.obter("t") is None and reg.expurgar() == 0


# ------------------------------------------------------------------------------ config
def test_config_padrao_de_retencao() -> None:
    cfg = carregar_config({})
    assert cfg.max_tasks == 1000 and cfg.pausa_ttl_s == 660.0


def test_config_retencao_via_ambiente() -> None:
    cfg = carregar_config({"A2A_MAX_TASKS": "50", "A2A_PAUSA_TTL_S": "30"})
    assert cfg.max_tasks == 50 and cfg.pausa_ttl_s == 30.0


@pytest.mark.parametrize(
    ("chave", "valor"),
    [
        ("A2A_MAX_TASKS", "0"),
        ("A2A_MAX_TASKS", "abc"),
        ("A2A_MAX_TASKS", "1000001"),
        ("A2A_PAUSA_TTL_S", "0"),
        ("A2A_PAUSA_TTL_S", "nan"),
        ("A2A_PAUSA_TTL_S", "inf"),
        ("A2A_PAUSA_TTL_S", "86401"),
    ],
)
def test_config_retencao_invalida_falha_no_boot(chave: str, valor: str) -> None:
    with pytest.raises(ConfigError, match=chave):
        carregar_config({chave: valor})
