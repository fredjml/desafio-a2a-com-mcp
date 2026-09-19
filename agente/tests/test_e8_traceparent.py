"""E8: traceparent. O trace-id da Task e fixado no 1o pedido (DEC-19) e vai em `_meta.traceparent` de
TODOS os requests MCP dela (tools/list se ocorrer, resources/read, tools/call inicial e retry), com
span-id novo por request; na continuacao, um header com OUTRO trace-id nao o troca e a ausencia do
header usa o guardado; formato W3C invalido e ignorado.

Cobre R-HOST-03, AC-19, T-19 e T-33 (unidade e ponta a ponta contra o servidor MCP REAL).
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

import pytest

from app.trace import trace_da_task, trace_id_de, traceparent_para

from .a2a_util import HostFalso, app_com, cliente_asgi, enviar, estado, pedido, reserva_ok, tarefa
from .test_e2e_agente import Ambiente, amb, mensagem  # noqa: F401
from .test_e2e_agente import estado as estado_e2e
from .test_e7_ponte_e2e import Sessao
from .test_e7a_pausa import TRACE_ID, TRACEPARENT, pergunta

TRACE_REAL = secrets.token_hex(16)  # novo por execucao (ponta a ponta)
TRACEPARENT_REAL = f"00-{TRACE_REAL}-{secrets.token_hex(8)}-01"
OUTRO_REAL = secrets.token_hex(16)
OUTRO_TRACEPARENT_REAL = f"00-{OUTRO_REAL}-{secrets.token_hex(8)}-01"


# ------------------------------------------------------------------------------ puras
@pytest.mark.parametrize(
    "valor",
    [
        f"00-{TRACE_ID}-00f067aa0ba902b7-01",
        f"00-{TRACE_ID}-00f067aa0ba902b7-00",
        f"01-{TRACE_ID}-00f067aa0ba902b7-01",
    ],
)
def test_trace_id_de_aceita_traceparent_w3c_valido(valor: str) -> None:
    assert trace_id_de(valor) == TRACE_ID


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "lixo",
        f"00-{TRACE_ID.upper()}-00f067aa0ba902b7-01",  # maiuscula
        f"00-{TRACE_ID}-00f067aa0ba902b7",  # sem flags
        f"00-{TRACE_ID[:-1]}-00f067aa0ba902b7-01",  # trace-id curto
        f"00-{TRACE_ID}-00f067aa0ba902b-01",  # span curto
        f"ff-{TRACE_ID}-00f067aa0ba902b7-01",  # versao proibida
        f"00-{'0' * 32}-00f067aa0ba902b7-01",  # trace-id zero
        f"00-{TRACE_ID}-{'0' * 16}-01",  # span zero
        f" 00-{TRACE_ID}-00f067aa0ba902b7-01",  # espaco
        f"00-{TRACE_ID}-00f067aa0ba902b7-01\n",  # quebra de linha
        f"00-{TRACE_ID}-00f067aa0ba902b7-01; x=1",
        123,
    ],
)
def test_trace_id_de_ignora_o_que_nao_e_w3c(valor: object) -> None:
    assert trace_id_de(valor) is None


def test_traceparent_para_mantem_o_trace_id_e_troca_o_span_a_cada_chamada() -> None:
    partes = [traceparent_para(TRACE_ID).split("-") for _ in range(50)]
    assert {p[1] for p in partes} == {TRACE_ID}
    assert len({p[2] for p in partes}) == 50
    assert all(
        p[0] == "00" and p[3] == "01" and re.fullmatch(r"[0-9a-f]{16}", p[2]) for p in partes
    )
    assert all(trace_id_de("-".join(p)) == TRACE_ID for p in partes)


def test_trace_da_task_o_guardado_sempre_vence() -> None:
    outro = "aaaabbbbccccddddeeeeffff00001111"
    assert trace_da_task(TRACE_ID, None) == (TRACE_ID, False)
    assert trace_da_task(TRACE_ID, "lixo") == (TRACE_ID, False)
    assert trace_da_task(TRACE_ID, TRACEPARENT) == (TRACE_ID, False)  # o mesmo
    assert trace_da_task(TRACE_ID, f"00-{outro}-1122334455667788-01") == (TRACE_ID, True)


# ------------------------------------------------------------------------------ na continuacao (host falso)
OUTRO_TRACE = "aaaabbbbccccddddeeeeffff00001111"
OUTRO_TRACEPARENT = f"00-{OUTRO_TRACE}-1122334455667788-01"


async def pausar(c: Any, host: HostFalso) -> str:
    r = await enviar(
        c,
        pedido("sala-garagem", "14:00", "15:00", "Marty"),
        cabecalhos={"traceparent": TRACEPARENT},
    )
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED", r
    return str(tarefa(r)["id"])


async def test_continuacao_sem_header_usa_o_trace_id_guardado_em_todos_os_requests_mcp() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        await enviar(c, "escolha=sala-mirante", tid)  # SEM traceparent
    assert host.leituras_de_politica == [TRACE_ID, TRACE_ID]
    assert [ch["trace_id"] for ch in host.chamadas] == [TRACE_ID, TRACE_ID]


async def test_header_com_outro_trace_id_na_continuacao_nao_troca_o_da_task_t33() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        await enviar(c, "escolha=sala-mirante", tid, cabecalhos={"traceparent": OUTRO_TRACEPARENT})
    assert host.leituras_de_politica == [TRACE_ID, TRACE_ID]
    assert [ch["trace_id"] for ch in host.chamadas] == [TRACE_ID, TRACE_ID]
    assert OUTRO_TRACE not in json.dumps([host.leituras_de_politica, host.chamadas], default=str)


async def test_header_igual_ou_invalido_na_continuacao_nao_muda_nada() -> None:
    host = HostFalso([pergunta(), pergunta({"const": "sala-fusca"}), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        tid = await pausar(c, host)
        r2 = await enviar(c, "escolha=sala-mirante", tid, cabecalhos={"traceparent": TRACEPARENT})
        r3 = await enviar(c, "escolha=sala-fusca", tid, cabecalhos={"traceparent": "lixo-invalido"})
    assert estado(r2) == "TASK_STATE_INPUT_REQUIRED" and estado(r3) == "TASK_STATE_COMPLETED"
    assert {ch["trace_id"] for ch in host.chamadas} == {TRACE_ID}
    assert set(host.leituras_de_politica) == {TRACE_ID}


async def test_task_sem_header_no_1o_pedido_mantem_o_trace_gerado_mesmo_com_header_depois() -> None:
    host = HostFalso([pergunta(), reserva_ok()])
    async with cliente_asgi(app_com(host)) as c:
        r0 = await enviar(c, pedido("sala-garagem", "14:00", "15:00"))  # sem traceparent
        gerado = host.chamadas[0]["trace_id"]
        assert gerado and gerado != TRACE_ID
        await enviar(
            c, "escolha=sala-mirante", tarefa(r0)["id"], cabecalhos={"traceparent": TRACEPARENT}
        )
    assert [ch["trace_id"] for ch in host.chamadas] == [
        gerado,
        gerado,
    ]  # o da Task (fixado no 1o pedido)
    assert host.leituras_de_politica == [gerado, gerado]


# ------------------------------------------------------------------------------ ponta a ponta (servidor real)
@pytest.fixture
def s(amb: Ambiente) -> Sessao:  # noqa: F811
    return Sessao(amb)


def test_trace_id_do_1o_pedido_em_todos_os_requests_mcp_da_task_inclusive_a_continuacao(
    s: Sessao,
) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT_REAL)
    r = s.enviar("escolha=sala-mirante", task_id=tid)  # continuacao SEM header
    assert estado_e2e(r) == "TASK_STATE_COMPLETED"
    linhas = s.amb.mcp.requests()
    assert [x["method"] for x in linhas] == [
        "tools/list",
        "resources/read",
        "tools/call",
        "resources/read",
        "tools/call",
    ]
    assert all((x["traceparent"] or "").split("-")[1] == TRACE_REAL for x in linhas)
    assert len({x["traceparent"].split("-")[2] for x in linhas}) == 5  # span novo por request
    assert all(
        x["traceparent"].startswith("00-") and x["traceparent"].endswith("-01") for x in linhas
    )


def test_header_com_outro_trace_id_na_continuacao_nao_troca_o_da_task_real_t33(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT_REAL)
    s.enviar("escolha=sala-mirante", task_id=tid, traceparent=OUTRO_TRACEPARENT_REAL)
    linhas = s.amb.mcp.requests()
    assert {x["traceparent"].split("-")[1] for x in linhas} == {TRACE_REAL}
    assert OUTRO_REAL not in "\n".join(s.amb.mcp.stderr)


def test_header_invalido_e_ignorado_sem_quebrar_e_gera_trace_novo_t19(s: Sessao) -> None:
    tid = s.pausar("sala-garagem", "14:00", "15:00", "Marty", "00-lixo-invalido-01")
    r = s.enviar("escolha=sala-mirante", task_id=tid, traceparent="nao-e-w3c")
    assert estado_e2e(r) == "TASK_STATE_COMPLETED"
    tracos = {x["traceparent"].split("-")[1] for x in s.amb.mcp.requests()}
    assert len(tracos) == 1 and re.fullmatch(r"[0-9a-f]{32}", next(iter(tracos)))


def test_duas_tasks_cada_uma_com_o_seu_trace_id(s: Sessao) -> None:
    a = s.pausar("sala-garagem", "14:00", "15:00", "Marty", TRACEPARENT_REAL)
    b = s.pausar("sala-fusca", "16:00", "17:00", "Doc", OUTRO_TRACEPARENT_REAL)
    s.enviar("escolha=sala-mirante", task_id=b)  # continua a 2a primeiro, sem header
    s.enviar("escolha=sala-mirante", task_id=a, traceparent=OUTRO_TRACEPARENT_REAL)
    por_trace: dict[str, list[str]] = {}
    for x in s.amb.mcp.requests():
        por_trace.setdefault(x["traceparent"].split("-")[1], []).append(x["method"])
    assert set(por_trace) == {TRACE_REAL, OUTRO_REAL}
    # cada Task: (tools/list so na 1a) resources/read + tools/call na pausa e outro par na retomada
    assert por_trace[TRACE_REAL].count("tools/call") == 2
    assert por_trace[OUTRO_REAL].count("tools/call") == 2
