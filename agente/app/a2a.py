"""Servidor A2A v1.0 (a2a-sdk 1.1.4): agent card, `POST /a2a` (SendMessage/GetTask), app Starlette.

Ajustes obrigatorios do spike S2:
1. `context_builder` injeta `A2A-Version: 1.0` quando ausente (o validador nao envia; sem isso
   o SDK assume 0.3 e responde -32009 em todo SendMessage/GetTask).
2. A continuacao usa `current_task.context_id` (ver `tasks.resolver_context_id`).
3. O 1o evento do executor e a Task; o ruido de OpenTelemetry no stderr e silenciado (`log`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from a2a.server.agent_execution import AgentExecutor, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import ASGIApp

from .config import Config
from .log import RegistroDeRequests
from .mcp_host import McpHost
from .tasks import ExecutorReservas, GeradorComPrefixo, PausedRegistry, ServicoMcp

VERSAO_A2A = "1.0"
CAMINHO_RPC = "/a2a"
EXEMPLO_PEDIDO = (
    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
)


def criar_card(config: Config) -> AgentCard:
    """Card identico ao wire 07 nos campos exigidos. Sem securitySchemes (sem auth no desafio)."""
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=config.card_url, protocol_binding="JSONRPC", protocol_version=VERSAO_A2A
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False, push_notifications=False, extended_agent_card=False
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description=(
                    "Reserva uma sala em um intervalo. Se houver conflito, "
                    "pergunta qual alternativa usar."
                ),
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[EXEMPLO_PEDIDO],
            )
        ],
    )


class ContextoComVersaoPadrao(DefaultServerCallContextBuilder):
    """Ponto de extensao PUBLICO do SDK: trata request sem `A2A-Version` como 1.0 (R-A2A-06/DEC-26).

    Nao reescreve o protocolo nem aceita versao errada: so preenche o header AUSENTE. Um cliente que
    manda outra versao (ex.: 0.3) continua recebendo `VersionNotSupported` do SDK.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        cabecalhos = contexto.state.setdefault("headers", {})
        if not (cabecalhos.get("a2a-version") or cabecalhos.get("A2A-Version")):
            cabecalhos["a2a-version"] = VERSAO_A2A
        return contexto


class DespachanteComGetTaskNoWire(JsonRpcDispatcher):
    """`GetTask` devolve `result.task`, identico ao wire 09 (o SDK 1.1.4 devolve a Task direto em `result`).

    Decisao E7: fidelidade ao wire (o validador aceita as duas formas). So o formato do `GetTask` muda;
    `SendMessage` ja devolve `result.task` no SDK. Sobrescreve o metodo interno `_handle_get_task`
    (versao do SDK travada em 1.1.4; um teste confere o formato).
    """

    async def _handle_get_task(
        self, request_obj: Any, context: ServerCallContext
    ) -> dict[str, Any]:
        tarefa: dict[str, Any] = await super()._handle_get_task(request_obj, context)
        return {"task": tarefa}


class App:
    """Agrupa o que o processo e os testes precisam (host MCP, executor, ASGI)."""

    def __init__(
        self,
        config: Config,
        host: ServicoMcp,
        executor: AgentExecutor | None = None,
        pausadas: PausedRegistry | None = None,
        task_store: TaskStore | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.pausadas = pausadas if pausadas is not None else PausedRegistry()
        self.executor = executor or ExecutorReservas(host, self.pausadas)
        self.task_store: TaskStore = task_store if task_store is not None else InMemoryTaskStore()
        self.card = criar_card(config)
        self.handler = DefaultRequestHandler(
            agent_executor=self.executor,
            task_store=self.task_store,
            agent_card=self.card,
            request_context_builder=SimpleRequestContextBuilder(
                should_populate_referred_tasks=False,
                task_store=self.task_store,
                task_id_generator=GeradorComPrefixo("task-"),
                context_id_generator=GeradorComPrefixo("ctx-"),
            ),
        )

    @asynccontextmanager
    async def _ciclo_de_vida(self, _app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await self.handler.aclose()
            await self.host.aclose()

    def asgi(self) -> ASGIApp:
        despachante = DespachanteComGetTaskNoWire(
            self.handler, context_builder=ContextoComVersaoPadrao()
        )
        rotas: list[Any] = create_agent_card_routes(self.card) + [
            Route(CAMINHO_RPC, endpoint=despachante.handle_requests, methods=["POST"])
        ]
        return RegistroDeRequests(Starlette(routes=rotas, lifespan=self._ciclo_de_vida))


def criar_app(config: Config, host: ServicoMcp | None = None) -> App:
    """O `host` MCP e LAZY: criar o app nao conecta ao MCP (o boot nao falha se o MCP nao subiu)."""
    return App(config, host or McpHost(config.mcp_url, timeout_s=config.mcp_timeout_s))
