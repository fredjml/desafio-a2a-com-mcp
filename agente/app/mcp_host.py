"""Host MCP do agente (E5): cliente `mcp.Client` UNICO e vivo, sem sessao, sem responder elicitation.

Decisoes (DEC-04/DEC-25, spikes S1/S2):
- `Client(url, mode="2026-07-28")`: modo fixo, nenhum `initialize`/`server/discover`/Mcp-Session-Id.
- `await client.list_tools()` EXPLICITO antes da 1a `tools/call` (o SDK so listaria depois).
- Chamadas por `client.session.call_tool(..., allow_input_required=True)`: o `InputRequiredResult`
  volta CRU para a ponte. `Client.call_tool` (auto-driver, que aciona callbacks) NUNCA e usado.
- O SDK so declara `elicitation` em `clientCapabilities` se houver `elicitation_callback`
  (mcp/client/session.py:634-638) e o declara como {form, url}. O callback abaixo e uma FACHADA
  so para anunciar a capability: nunca e invocado no caminho `allow_input_required=True`
  (um teste prova) e, se um dia for, responde erro em vez de escolher pelo usuario.
- O `Client` e aberto/fechado por UMA tarefa dona (o `async with` do anyio nao pode atravessar
  tarefas): as tarefas de request so usam `client.session`. Apos erro de transporte/timeout o
  cliente e recriado (R-ARQ-02); erros de protocolo devolvidos pelo servidor NAO o recriam.
- O `requestState` e opaco: passa daqui para o servidor sem ser aberto, logado ou exposto.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, MCPError
from mcp_types import (
    CONNECTION_CLOSED,
    REQUEST_TIMEOUT,
    CallToolResult,
    ElicitRequestParams,
    ElicitResult,
    ErrorData,
    Implementation,
    InputRequiredResult,
    TextResourceContents,
)

from .log import emitir
from .trace import traceparent_para

MODO_MCP = "2026-07-28"
NOME_CLIENTE = "agente-central-de-salas"
VERSAO_CLIENTE = "1.0.0"
URI_POLITICA = "politica://uso"
_VERSAO_POLITICA = re.compile(r"versao:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]{0,63})[ \t]*")


class McpHostError(Exception):
    """Falha de infraestrutura MCP (timeout, conexao, resposta invalida). Mensagem clara e segura."""

    def __init__(self, tipo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.tipo = tipo
        self.mensagem = mensagem


class McpProtocolError(McpHostError):
    """Erro JSON-RPC devolvido pelo servidor MCP (ex.: -32602, -32021). Nao recria o cliente."""

    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(
            "protocolo", f"Servidor MCP recusou o pedido (codigo {codigo}): {mensagem}"
        )
        self.codigo = codigo


def extrair_versao_politica(texto: str) -> str | None:
    """`versao: X` da 1a linha do resource (tolera \\r\\n, BOM e espacos)."""
    linhas = texto.lstrip("\ufeff").splitlines()
    if not linhas:
        return None
    achou = _VERSAO_POLITICA.fullmatch(linhas[0].strip())
    return achou.group(1) if achou else None


@dataclass
class _Ciclo:
    """Uma vida do Client: aberta e fechada pela mesma tarefa (`dono`)."""

    client: Client | None = None
    erro: BaseException | None = None
    pronto: asyncio.Event = field(default_factory=asyncio.Event)
    parar: asyncio.Event = field(default_factory=asyncio.Event)
    dono: asyncio.Task[None] | None = None
    ferramentas: frozenset[str] = frozenset()

    def vivo(self) -> Client:
        if self.client is None:
            raise McpHostError("conexao", "Cliente MCP fechado")
        return self.client


class McpHost:
    def __init__(self, url: str, *, timeout_s: float = 10.0) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._ciclo: _Ciclo | None = None
        self._trava = asyncio.Lock()
        # Quantas vezes a fachada de elicitation foi invocada: DEVE ser sempre 0 (T-12).
        self.invocacoes_da_fachada = 0
        self.clientes_criados = 0

    # ------------------------------------------------------------------ fachada
    async def _fachada_elicitation(
        self, context: object, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        """NUNCA deve ser chamada: existe so para o SDK anunciar `elicitation` em clientCapabilities.

        A elicitation do MRTR e resolvida pela ponte A2A (o usuario escolhe), nunca por callback.
        """
        self.invocacoes_da_fachada += 1
        emitir("erro", codigo="fachada_invocada", mensagem="callback de elicitation invocado")
        return ErrorData(code=-32600, message="elicitation nao e respondida por callback")

    # ------------------------------------------------------------------ ciclo de vida
    async def _dono(self, ciclo: _Ciclo) -> None:
        try:
            async with Client(
                self._url,
                mode=MODO_MCP,
                client_info=Implementation(name=NOME_CLIENTE, version=VERSAO_CLIENTE),
                elicitation_callback=self._fachada_elicitation,
                read_timeout_seconds=self._timeout_s,
                cache=None,  # cada leitura vai ao servidor (politica por Task; ttlMs e 0 mesmo)
            ) as client:
                ciclo.client = client
                ciclo.pronto.set()
                await ciclo.parar.wait()
        except asyncio.CancelledError:
            ciclo.erro = ciclo.erro or RuntimeError("cancelado")
            raise
        except Exception as exc:  # noqa: BLE001 - guardado e traduzido por quem abriu o ciclo
            ciclo.erro = exc
        finally:
            ciclo.client = None
            ciclo.pronto.set()

    async def _abrir(self) -> _Ciclo:
        ciclo = _Ciclo()
        ciclo.dono = asyncio.create_task(self._dono(ciclo), name="mcp-host-dono")
        await asyncio.wait_for(ciclo.pronto.wait(), self._timeout_s)
        if ciclo.client is None:
            raise McpHostError("conexao", "Nao foi possivel abrir o cliente MCP")
        self.clientes_criados += 1
        emitir("mcp_cliente", acao="criado", modo=MODO_MCP, criados=self.clientes_criados)
        return ciclo

    @staticmethod
    async def _fechar(ciclo: _Ciclo) -> None:
        ciclo.parar.set()
        dono = ciclo.dono
        if dono is None:
            return
        with contextlib.suppress(Exception):  # fechamento e melhor esforco
            try:
                await asyncio.wait_for(asyncio.shield(dono), 5.0)
            except (asyncio.TimeoutError, TimeoutError):
                dono.cancel()

    async def _invalidar(self, ciclo: _Ciclo, motivo: str) -> None:
        async with self._trava:
            if self._ciclo is ciclo:
                self._ciclo = None
        emitir("mcp_cliente", acao="descartado", motivo=motivo)
        await self._fechar(ciclo)

    async def aclose(self) -> None:
        ciclo, self._ciclo = self._ciclo, None
        if ciclo is not None:
            await self._fechar(ciclo)

    async def _garantir(self, trace_id: str | None) -> _Ciclo:
        """Cliente vivo com `tools/list` ja feito (lazy: o boot do agente nao exige o MCP)."""
        async with self._trava:
            if self._ciclo is not None and self._ciclo.client is not None:
                return self._ciclo
            ciclo = await self._abrir()
            try:
                listagem = await asyncio.wait_for(
                    ciclo.vivo().list_tools(meta=self._meta(trace_id)),  # type: ignore[arg-type]
                    self._timeout_s,
                )
                ciclo.ferramentas = frozenset(t.name for t in listagem.tools)
            except Exception as exc:  # noqa: BLE001 - traduzido em McpHostError
                await self._fechar(ciclo)
                raise self._traduzir(exc, "tools/list") from None
            self._ciclo = ciclo
            emitir("mcp_ferramentas", ferramentas=sorted(ciclo.ferramentas))
            return ciclo

    # ------------------------------------------------------------------ erros
    @staticmethod
    def _traduzir(exc: BaseException, etapa: str) -> McpHostError:
        if isinstance(exc, McpHostError):
            return exc
        if isinstance(exc, MCPError):
            # O SDK sintetiza -32000 (conexao fechada/recusada) e -32001 (timeout de leitura) para falhas
            # de TRANSPORTE; o servidor nunca os emite. Sao falha de infraestrutura: recria o cliente.
            if int(exc.code) == CONNECTION_CLOSED:
                return McpHostError("conexao", f"Servidor MCP indisponivel em {etapa}")
            if int(exc.code) == REQUEST_TIMEOUT:
                return McpHostError("timeout", f"Servidor MCP nao respondeu a tempo em {etapa}")
            return McpProtocolError(int(exc.code), str(exc.error.message)[:300])
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return McpHostError("timeout", f"Servidor MCP nao respondeu a tempo em {etapa}")
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise exc
        return McpHostError(
            "conexao", f"Servidor MCP indisponivel em {etapa} ({type(exc).__name__})"
        )

    async def _executar(
        self, etapa: str, trace_id: str | None, operacao: Any, *, repetir_uma_vez: bool
    ) -> Any:
        """Roda `operacao(client)` com timeout; em falha de transporte descarta o cliente (R-ARQ-02).

        `repetir_uma_vez`: so para operacoes de leitura (tools/list ja ocorreu em `_garantir`,
        resources/read): uma conexao obsoleta (servidor reiniciado) e refeita 1x. `tools/call`
        NUNCA e repetida aqui: poderia ter chegado ao servidor.
        """
        tentativas = 2 if repetir_uma_vez else 1
        ultimo = McpHostError("conexao", f"Servidor MCP indisponivel em {etapa}")
        for _ in range(tentativas):
            try:
                ciclo = await self._garantir(trace_id)
            except McpHostError as exc:
                ultimo = exc
                if isinstance(exc, McpProtocolError):
                    raise
                continue
            try:
                return await asyncio.wait_for(operacao(ciclo), self._timeout_s)
            except Exception as exc:  # noqa: BLE001 - traduzido em McpHostError
                erro = self._traduzir(exc, etapa)
                if isinstance(erro, McpProtocolError):
                    raise erro from None
                await self._invalidar(ciclo, f"{erro.tipo}:{etapa}")
                ultimo = erro
        raise ultimo

    # ------------------------------------------------------------------ API
    async def versao_da_politica(self, trace_id: str | None = None) -> str:
        """Le `politica://uso` e extrai `versao:` da 1a linha. Chamado a CADA Task."""

        async def ler(ciclo: _Ciclo) -> str:
            resposta = await ciclo.vivo().session.read_resource(
                URI_POLITICA,
                meta=self._meta(trace_id),  # type: ignore[arg-type]
            )
            for conteudo in resposta.contents:
                if isinstance(conteudo, TextResourceContents):
                    versao = extrair_versao_politica(conteudo.text)
                    if versao:
                        return versao
            raise McpHostError(
                "politica_invalida", "O recurso politica://uso nao traz 'versao:' na primeira linha"
            )

        resultado: str = await self._executar("resources/read", trace_id, ler, repetir_uma_vez=True)
        return resultado

    async def chamar_ferramenta(
        self,
        nome: str,
        argumentos: dict[str, Any],
        *,
        trace_id: str | None = None,
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """`tools/call` crua: devolve `CallToolResult | InputRequiredResult` sem interpretar."""

        async def chamar(ciclo: _Ciclo) -> CallToolResult | InputRequiredResult:
            if nome not in ciclo.ferramentas:
                raise McpHostError(
                    "ferramenta_ausente", f"O servidor MCP nao oferece a ferramenta {nome}"
                )
            resultado = await ciclo.vivo().session.call_tool(
                nome,
                argumentos,
                read_timeout_seconds=self._timeout_s,
                meta=self._meta(trace_id),  # type: ignore[arg-type]
                input_responses=input_responses,  # type: ignore[arg-type]
                request_state=request_state,
                allow_input_required=True,
            )
            if not isinstance(resultado, (CallToolResult, InputRequiredResult)):
                raise McpHostError("resposta_invalida", "Resposta MCP inesperada em tools/call")
            return resultado

        resposta: CallToolResult | InputRequiredResult = await self._executar(
            "tools/call", trace_id, chamar, repetir_uma_vez=False
        )
        return resposta

    @staticmethod
    def _meta(trace_id: str | None) -> dict[str, str] | None:
        """`_meta` extra do request: `traceparent` com o trace-id da Task e SPAN NOVO a cada request."""
        return {"traceparent": traceparent_para(trace_id)} if trace_id else None
