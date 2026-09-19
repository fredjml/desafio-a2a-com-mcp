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
  tarefas): as tarefas de request so usam `client.session`. Apos erro de TRANSPORTE/timeout o
  cliente e recriado (R-ARQ-02); erros de protocolo devolvidos pelo servidor e erros logicos
  (ferramenta ausente, resposta ou politica invalida) NAO o recriam (F-02).
- Um Client novo recomeca os ids JSON-RPC em 1: `evitar_ids` garante que o retry de uma pausa feita
  por um Client anterior nao reutilize o id da chamada inicial (id novo, regra do enunciado).
- Um unico timeout (`asyncio.wait_for`, `MCP_TIMEOUT_S`); excecoes traduzidas so por tipo (F-04).
- O `requestState` e opaco: passa daqui para o servidor sem ser aberto, logado ou exposto.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
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
from pydantic import ValidationError

from .log import emitir
from .trace import traceparent_para

MODO_MCP = "2026-07-28"
NOME_CLIENTE = "agente-central-de-salas"
VERSAO_CLIENTE = "1.0.0"
URI_POLITICA = "politica://uso"
_VERSAO_POLITICA = re.compile(r"versao:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]{0,63})[ \t]*")
# Falhas de TRANSPORTE que o host traduz (F-04: nada de `except Exception`; um bug de programacao
# deve propagar ate o executor, que loga o traceback). `asyncio.TimeoutError` so e alias de
# `TimeoutError` a partir do Python 3.11.
_ERROS_DE_TRANSPORTE = (MCPError, httpx.HTTPError, OSError, asyncio.TimeoutError)
TIPOS_DE_TRANSPORTE = frozenset({"conexao", "timeout"})
# Ao trocar de Client, ids recem-emitidos ate esta distancia do id da chamada original sao "queimados".
JANELA_DE_IDS = 8


class McpHostError(Exception):
    """Falha de infraestrutura MCP (timeout, conexao, resposta invalida). Mensagem clara e segura."""

    def __init__(self, tipo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.tipo = tipo
        self.mensagem = mensagem

    @property
    def transporte(self) -> bool:
        """Falha de transporte (conexao/timeout): o servidor pode voltar e o pedido ser reenviado."""
        return self.tipo in TIPOS_DE_TRANSPORTE


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
    # Requests com id enviados por este Client. O SDK numera 1, 2, 3...; logo isto e o maior id ja usado
    # (cota superior, com chamadas concorrentes).
    enviados: int = 0

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

    @property
    def ids_emitidos(self) -> int:
        """Requests com id enviados pelo Client vivo (0 se nao ha): cota superior do maior id usado."""
        return self._ciclo.enviados if self._ciclo is not None else 0

    @property
    def geracao(self) -> int:
        """Geracao do Client que atenderia uma chamada AGORA: a atual, ou a proxima se nao ha Client vivo.

        Um Client novo recomeca os ids JSON-RPC em 1; a ponte compara a geracao da pausa com a do
        retry e nao envia o retry por outro Client (o id poderia igualar o da chamada inicial).
        """
        vivo = self._ciclo is not None and self._ciclo.client is not None
        return self.clientes_criados if vivo else self.clientes_criados + 1

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
                cache=None,  # cada leitura vai ao servidor (politica por Task; ttlMs e 0 mesmo)
            ) as client:
                ciclo.client = client
                ciclo.pronto.set()
                await ciclo.parar.wait()
        except asyncio.CancelledError:
            ciclo.erro = ciclo.erro or RuntimeError("cancelado")
            raise
        except _ERROS_DE_TRANSPORTE as exc:  # guardado e traduzido por quem abriu o ciclo
            ciclo.erro = exc
        finally:
            ciclo.client = None
            ciclo.pronto.set()

    async def _abrir(self) -> _Ciclo:
        ciclo = _Ciclo()
        dono = asyncio.create_task(self._dono(ciclo), name="mcp-host-dono")
        ciclo.dono = dono
        try:
            await asyncio.wait_for(ciclo.pronto.wait(), self._timeout_s)
        except asyncio.TimeoutError:
            dono.cancel()  # F-12: a tarefa dona nao pode ficar viva sem dono
            await asyncio.gather(dono, return_exceptions=True)
            raise McpHostError(
                "timeout", "Servidor MCP nao respondeu a tempo ao abrir o cliente"
            ) from None
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
                ciclo.enviados += 1
                listagem = await asyncio.wait_for(
                    ciclo.vivo().list_tools(meta=self._meta(trace_id)),  # type: ignore[arg-type]
                    self._timeout_s,
                )
                ciclo.ferramentas = frozenset(t.name for t in listagem.tools)
            except (McpHostError, ValidationError, *_ERROS_DE_TRANSPORTE) as exc:
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
        if isinstance(exc, ValidationError):  # resposta do servidor fora do formato do protocolo
            return McpHostError("resposta_invalida", f"Resposta MCP invalida em {etapa}")
        return McpHostError(
            "conexao", f"Servidor MCP indisponivel em {etapa} ({type(exc).__name__})"
        )

    async def _executar(
        self, etapa: str, trace_id: str | None, operacao: Any, *, repetir_uma_vez: bool
    ) -> Any:
        """Roda `operacao(client)` com timeout; em falha de TRANSPORTE descarta o cliente (R-ARQ-02).

        Erro logico (ferramenta ausente, resposta invalida, politica invalida) e erro de protocolo
        devolvido pelo servidor NAO recriam o Client (F-02): o Client segue saudavel.

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
                # Este e o UNICO timeout efetivo do host (F-12); o SDK nao tem outro configurado.
                return await asyncio.wait_for(operacao(ciclo), self._timeout_s)
            except (McpHostError, ValidationError, *_ERROS_DE_TRANSPORTE) as exc:
                erro = self._traduzir(exc, etapa)
                if not erro.transporte:
                    raise erro from None
                await self._invalidar(ciclo, f"{erro.tipo}:{etapa}")
                ultimo = erro
        raise ultimo

    # ------------------------------------------------------------------ API
    async def versao_da_politica(self, trace_id: str | None = None) -> str:
        """Le `politica://uso` e extrai `versao:` da 1a linha. Chamado a CADA Task."""

        async def ler(ciclo: _Ciclo) -> str:
            ciclo.enviados += 1
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
            ciclo.enviados += 1
            resultado = await ciclo.vivo().session.call_tool(
                nome,
                argumentos,
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

    async def evitar_ids(
        self, geracao_origem: int, ids_ate: int, trace_id: str | None = None
    ) -> bool:
        """O retry NAO pode reutilizar o id da chamada original (id novo, regra do enunciado).

        Mesmo Client: o contador e monotonico, nada a fazer. Client recriado: os ids recomecam em 1 e
        o proximo pode cair perto de `ids_ate` (o maior id do Client anterior na chamada original); se
        cair, "queima" ids com `tools/list` ate passar dele. False se nao deu para garantir.
        """
        if geracao_origem == self.geracao:
            return True

        async def queimar(ciclo: _Ciclo) -> None:
            ciclo.enviados += 1
            await ciclo.vivo().list_tools(meta=self._meta(trace_id))  # type: ignore[arg-type]

        for _ in range(JANELA_DE_IDS + 1):
            ciclo = await self._garantir(trace_id)
            if not ids_ate - JANELA_DE_IDS <= ciclo.enviados + 1 <= ids_ate:
                return True
            await self._executar("tools/list", trace_id, queimar, repetir_uma_vez=False)
        return False

    @staticmethod
    def _meta(trace_id: str | None) -> dict[str, str] | None:
        """`_meta` extra do request: `traceparent` com o trace-id da Task e SPAN NOVO a cada request."""
        return {"traceparent": traceparent_para(trace_id)} if trace_id else None
