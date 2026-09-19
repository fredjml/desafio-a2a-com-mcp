"""E5: host MCP contra o servidor REAL (via proxy gravador) - o que vai NO FIO.

Cobre AC-18/AC-20, R-HOST-01/02, R-ARQ-01/02, T-11, T-12, T-34, T-43.
"""

from __future__ import annotations

import ast
import re
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult, ElicitResult, InputRequiredResult

from app.mcp_host import (
    McpHost,
    McpHostError,
    McpProtocolError,
    extrair_versao_politica,
)
from app.trace import novo_trace_id

from .procs import ProxyGravador, ServidorMcpReal, porta_livre

APP = Path(__file__).resolve().parents[1] / "app"
K_VERSAO = "io.modelcontextprotocol/protocolVersion"
K_CAPS = "io.modelcontextprotocol/clientCapabilities"
K_INFO = "io.modelcontextprotocol/clientInfo"


def h(hora: str) -> str:
    return f"2026-11-03T{hora}:00-03:00"


def args(sala: str, ini: str, fim: str, resp: str = "Marty") -> dict[str, Any]:
    return {"sala": sala, "inicio": h(ini), "fim": h(fim), "responsavel": resp}


@pytest.fixture
def proxy(servidor_mcp: ServidorMcpReal) -> Iterator[ProxyGravador]:
    px = ProxyGravador(servidor_mcp.url)
    try:
        yield px
    finally:
        px.parar()


def chave_de(r: InputRequiredResult) -> str:
    assert r.input_requests is not None
    (chave,) = r.input_requests
    return chave


async def ciclo_conflito(
    host: McpHost, trace_id: str | None
) -> tuple[str, InputRequiredResult, CallToolResult | InputRequiredResult]:
    """politica -> tools/call em conflito (input_required) -> retry accept. Devolve (versao, r1, r2)."""
    versao = await host.versao_da_politica(trace_id)
    a = args("sala-garagem", "14:00", "15:00")
    r1 = await host.chamar_ferramenta("reservar_sala", a, trace_id=trace_id)
    assert isinstance(r1, InputRequiredResult)
    chave = chave_de(r1)
    r2 = await host.chamar_ferramenta(
        "reservar_sala",
        a,
        trace_id=trace_id,
        input_responses={chave: ElicitResult(action="accept", content={"sala": "sala-fusca"})},
        request_state=r1.request_state,
    )
    return versao, r1, r2


# --------------------------------------------------------------------------- o fio
async def test_fio_do_cliente_mcp_contra_o_servidor_real(proxy: ProxyGravador) -> None:
    host = McpHost(proxy.url, timeout_s=10)
    trace_id = novo_trace_id()
    try:
        versao, _, r2 = await ciclo_conflito(host, trace_id)
    finally:
        await host.aclose()

    assert versao == "2026-11-01"
    assert isinstance(r2, CallToolResult) and not r2.is_error
    assert (r2.structured_content or {}).get("sala") == "sala-fusca"

    # sequencia: tools/list ANTES do 1o tools/call (T-43/AC-18); sem initialize/server-discover (T-34)
    assert proxy.metodos() == ["tools/list", "resources/read", "tools/call", "tools/call"]
    assert set(proxy.metodos_http) == {"POST"}  # sem GET/SSE nem DELETE de sessao

    ids = [r["json"]["id"] for r in proxy.requests]
    assert len(set(ids)) == len(ids), f"ids JSON-RPC repetidos: {ids}"  # T-11: retry != inicial
    id_inicial, id_retry = ids[2], ids[3]
    assert id_inicial != id_retry

    spans = []
    for i, r in enumerate(proxy.requests):
        cab, meta, corpo = r["headers"], proxy.meta(i), r["json"]
        assert "mcp-session-id" not in cab
        assert cab["mcp-protocol-version"] == "2026-07-28"
        assert cab["mcp-method"] == corpo["method"]
        if corpo["method"] == "tools/call":
            assert cab["mcp-name"] == corpo["params"]["name"] == "reservar_sala"
        elif corpo["method"] == "resources/read":
            assert cab["mcp-name"] == corpo["params"]["uri"] == "politica://uso"
        assert meta[K_VERSAO] == "2026-07-28"
        assert meta[K_INFO]["name"] == "agente-central-de-salas"
        caps = meta[K_CAPS]
        assert "form" in caps["elicitation"]  # AC-20: declara elicitation.form
        tp = meta["traceparent"]
        assert re.fullmatch(rf"00-{trace_id}-[0-9a-f]{{16}}-01", tp), tp  # MESMO trace-id em todos
        spans.append(tp.split("-")[2])
    assert len(set(spans)) == len(spans)  # span-id novo por request
    assert host.clientes_criados == 1  # um unico Client vivo


async def test_capabilities_no_fio_sao_as_do_sdk_com_callback_de_fachada(
    proxy: ProxyGravador,
) -> None:
    """Evidencia (spike S1, session.py:634-638): com callback o SDK declara {form,url}; aceito pelo servidor."""
    host = McpHost(proxy.url, timeout_s=10)
    try:
        await host.versao_da_politica(None)
    finally:
        await host.aclose()
    assert proxy.meta(1)[K_CAPS] == {"elicitation": {"form": {}, "url": {}}}


async def test_sem_trace_id_nao_envia_traceparent(proxy: ProxyGravador) -> None:
    host = McpHost(proxy.url, timeout_s=10)
    try:
        await host.versao_da_politica(None)
    finally:
        await host.aclose()
    assert all("traceparent" not in proxy.meta(i) for i in range(len(proxy.requests)))


async def test_stderr_do_servidor_mostra_list_antes_do_call(
    servidor_mcp: ServidorMcpReal,
) -> None:
    """T-43 / AC-18 pela evidencia do servidor: tools/list precede o 1o tools/call, ids distintos."""
    host = McpHost(servidor_mcp.url, timeout_s=10)
    trace_id = novo_trace_id()
    try:
        await ciclo_conflito(host, trace_id)
    finally:
        await host.aclose()
    linhas = servidor_mcp.requests()
    metodos = [linha["method"] for linha in linhas]
    assert metodos.index("tools/list") < metodos.index("tools/call")
    assert metodos == ["tools/list", "resources/read", "tools/call", "tools/call"]
    ids = [linha["id"] for linha in linhas]
    assert len(set(ids)) == len(ids)
    assert {linha["client"] for linha in linhas} == {"agente-central-de-salas"}
    assert all((linha["traceparent"] or "").split("-")[1] == trace_id for linha in linhas)


# --------------------------------------------------------------------------- T-12
async def test_callback_de_fachada_nunca_e_invocado(
    proxy: ProxyGravador, servidor_mcp: ServidorMcpReal
) -> None:
    """T-12: input_required vem CRU; accept, decline e nova rodada nunca acionam o callback."""
    host = McpHost(proxy.url, timeout_s=10)
    try:
        _, r1, _ = await ciclo_conflito(host, None)
        a = args("sala-garagem", "14:00", "15:00")
        # decline com o mesmo estado (reutilizavel)
        chave = chave_de(r1)
        r3 = await host.chamar_ferramenta(
            "reservar_sala",
            a,
            input_responses={chave: ElicitResult(action="decline")},
            request_state=r1.request_state,
        )
        assert (
            isinstance(r3, CallToolResult)
            and (r3.structured_content or {}).get("reservado") is False
        )
        # pergunta nova
        r4 = await host.chamar_ferramenta("reservar_sala", a)
        assert isinstance(r4, InputRequiredResult)
    finally:
        await host.aclose()
    assert host.invocacoes_da_fachada == 0


def test_estatico_nunca_usa_o_auto_driver_do_client() -> None:
    """`Client.call_tool` (auto-driver que aciona callbacks) e PROIBIDO: so `session.call_tool`."""
    achados: list[str] = []
    for arq in APP.glob("*.py"):
        arvore = ast.parse(arq.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if (
                isinstance(no, ast.Call)
                and isinstance(no.func, ast.Attribute)
                and no.func.attr in {"call_tool", "read_resource", "get_prompt"}
            ):
                dono = ast.unparse(no.func.value)
                if not dono.endswith("session"):
                    achados.append(f"{arq.name}:{no.lineno} {dono}.{no.func.attr}")
    assert achados == []


def test_estatico_chamada_de_tool_usa_allow_input_required() -> None:
    fonte = (APP / "mcp_host.py").read_text(encoding="utf-8")
    assert "allow_input_required=True" in fonte


def test_estatico_agente_nao_importa_o_servidor() -> None:
    """R-ARQ-01 / T-34: nada de `import app...` do servidor; so HTTP."""
    for arq in APP.glob("*.py"):
        arvore = ast.parse(arq.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                nomes = [a.name for a in no.names]
            elif isinstance(no, ast.ImportFrom):
                nomes = [no.module or ""] if no.level == 0 else []
            else:
                continue
            for nome in nomes:
                assert not nome.startswith(("servidor", "servidor_mcp")), (arq.name, nome)
                assert nome.split(".")[0] != "app", (arq.name, nome)
    todo = "\n".join(a.read_text(encoding="utf-8") for a in APP.glob("*.py"))
    assert "mcp.server" not in todo  # so o lado cliente do SDK


def _constantes_fora_de_docstring(arvore: ast.AST) -> list[str]:
    docs: set[int] = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            corpo = no.body
            if (
                corpo
                and isinstance(corpo[0], ast.Expr)
                and isinstance(corpo[0].value, ast.Constant)
            ):
                docs.add(id(corpo[0].value))
    return [
        str(n.value)
        for n in ast.walk(arvore)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def test_estatico_sem_sessao_no_codigo() -> None:
    """Sem `initialize`/`server/discover`/Mcp-Session-Id; modo do Client FIXO em 2026-07-28."""
    from app import mcp_host

    assert mcp_host.MODO_MCP == "2026-07-28"
    for arq in APP.glob("*.py"):
        arvore = ast.parse(arq.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
                assert no.func.attr != "initialize", arq.name
        for texto in _constantes_fora_de_docstring(arvore):
            baixo = texto.lower()
            assert "server/discover" not in baixo and "mcp-session-id" not in baixo, (
                arq.name,
                texto,
            )
            assert texto not in {"auto", "legacy"}, (arq.name, texto)
    arvore = ast.parse((APP / "mcp_host.py").read_text(encoding="utf-8"))
    modos = [
        kw.value
        for no in ast.walk(arvore)
        if isinstance(no, ast.Call) and ast.unparse(no.func) == "Client"
        for kw in no.keywords
        if kw.arg == "mode"
    ]
    assert [ast.unparse(m) for m in modos] == ["MODO_MCP"]


# --------------------------------------------------------------------------- erros e recriacao
async def test_servidor_fora_do_ar_gera_erro_claro_e_rapido() -> None:
    host = McpHost(f"http://127.0.0.1:{porta_livre()}/mcp", timeout_s=3)
    try:
        with pytest.raises(McpHostError) as exc:
            await host.chamar_ferramenta("reservar_sala", args("sala-porao", "09:00", "10:00"))
    finally:
        await host.aclose()
    assert exc.value.tipo == "conexao"
    assert "Servidor MCP indisponivel" in exc.value.mensagem


async def test_servidor_reiniciado_recria_o_cliente(
    servidor_mcp: ServidorMcpReal, segredo: str
) -> None:
    """R-ARQ-02: apos queda/restart, a proxima chamada recria o cliente e conclui."""
    host = McpHost(servidor_mcp.url, timeout_s=5)
    porta = servidor_mcp.porta
    try:
        assert await host.versao_da_politica(None) == "2026-11-01"
        assert host.clientes_criados == 1
        servidor_mcp.parar()
        with pytest.raises(McpHostError) as exc:  # servidor morto: erro claro, nao trava
            await host.chamar_ferramenta("reservar_sala", args("sala-porao", "09:00", "10:00"))
        assert exc.value.tipo in {"conexao", "timeout"}
        novo = ServidorMcpReal(segredo=segredo, porta=porta)
        try:
            novo.esperar_porta(porta)
            r = await host.chamar_ferramenta("reservar_sala", args("sala-porao", "09:00", "10:00"))
            assert isinstance(r, CallToolResult) and not r.is_error
            assert host.clientes_criados == 2  # cliente recriado
            metodos = [linha["method"] for linha in novo.requests()]
            assert metodos[0] == "tools/list"  # tools/list de novo antes do tools/call
            assert metodos.index("tools/list") < metodos.index("tools/call")
        finally:
            novo.parar()
    finally:
        await host.aclose()


async def test_servidor_que_nao_responde_estoura_o_timeout() -> None:
    mudo = socket.socket()
    mudo.bind(("127.0.0.1", 0))
    mudo.listen(5)
    porta = mudo.getsockname()[1]
    host = McpHost(f"http://127.0.0.1:{porta}/mcp", timeout_s=1.0)
    try:
        with pytest.raises(McpHostError) as exc:
            await host.versao_da_politica(None)
    finally:
        await host.aclose()
        mudo.close()
    assert exc.value.tipo == "timeout"


async def test_erro_de_protocolo_do_servidor_nao_recria_o_cliente(
    servidor_mcp: ServidorMcpReal,
) -> None:
    """-32602 devolvido pelo servidor e resposta valida: e `McpProtocolError` e o cliente segue vivo."""
    host = McpHost(servidor_mcp.url, timeout_s=10)
    try:
        r = await host.chamar_ferramenta("reservar_sala", args("sala-garagem", "14:00", "15:00"))
        assert isinstance(r, InputRequiredResult)
        chave = chave_de(r)
        with pytest.raises(McpProtocolError) as exc:
            await host.chamar_ferramenta(
                "reservar_sala",
                args("sala-garagem", "14:00", "15:00"),
                input_responses={
                    chave: ElicitResult(action="accept", content={"sala": "sala-fusca"})
                },
                request_state=str(r.request_state)[:-4] + "AAAA",  # adulterado
            )
        assert exc.value.codigo == -32602
        assert host.clientes_criados == 1
    finally:
        await host.aclose()


async def test_ferramenta_ausente_na_listagem_e_erro_claro(servidor_mcp: ServidorMcpReal) -> None:
    host = McpHost(servidor_mcp.url, timeout_s=10)
    try:
        with pytest.raises(McpHostError) as exc:
            await host.chamar_ferramenta("voar_delorean", {})
    finally:
        await host.aclose()
    assert exc.value.tipo == "ferramenta_ausente"


async def test_chamadas_concorrentes_de_varias_tarefas_usam_o_mesmo_cliente(
    proxy: ProxyGravador,
) -> None:
    """O Client vive na tarefa dona; tarefas de request (como as do uvicorn) so usam a sessao."""
    import asyncio

    host = McpHost(proxy.url, timeout_s=10)
    try:
        versoes = await asyncio.gather(
            *[asyncio.create_task(host.versao_da_politica(novo_trace_id())) for _ in range(6)]
        )
    finally:
        await host.aclose()
    assert set(versoes) == {"2026-11-01"}
    assert host.clientes_criados == 1
    assert proxy.metodos().count("tools/list") == 1
    ids = [r["json"]["id"] for r in proxy.requests]
    assert len(set(ids)) == len(ids)


async def test_boot_e_lazy_nao_conecta_ao_criar_o_host() -> None:
    host = McpHost("http://127.0.0.1:9/mcp", timeout_s=1)  # ninguem escuta; nao pode falhar aqui
    assert host.clientes_criados == 0
    await host.aclose()


# --------------------------------------------------------------------------- politica (T-36)
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("versao: 2026-11-01\n\n- Reservas...\n", "2026-11-01"),
        ("versao: 2026-11-01\r\n\r\n- Reservas...\r\n", "2026-11-01"),  # CRLF (Windows)
        ("versao:2026-11-01", "2026-11-01"),
        ("\ufeffversao: 2026-11-01  \r\nresto", "2026-11-01"),
        ("versao: 2027-01-15\n", "2027-01-15"),
        ("", None),
        ("Versao: 2026-11-01\n", None),  # a chave e exata
        ("- item\nversao: 2026-11-01\n", None),  # so a 1a linha vale
        ("versao: \n", None),
        ("versao: a b\n", None),
        ("versao: <script>\n", None),
    ],
)
def test_extrair_versao_da_politica(texto: str, esperado: str | None) -> None:
    v = extrair_versao_politica(texto)
    assert v == esperado
    assert v is None or "\r" not in v
