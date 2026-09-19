"""Task A2A: executor (traduz A2A <-> MCP), maquina de estados e estado da ponte (PausedState).

O agente NAO tem regra de sala/politica/alternativas: `execute` so (1) le o pedido, (2) fala MCP,
(3) traduz o resultado em estado A2A. Toda resposta A2A sai do SDK a partir de campos montados aqui
por allowlist (status.message, artifact `reserva`); nenhum objeto interno e serializado.

Estado A2A: SUBMITTED -> WORKING (internos; `GetTask` reflete o corrente) -> terminal | INPUT_REQUIRED.
Terminal (COMPLETED/FAILED/CANCELED) e definitivo: o SDK recusa SendMessage para Task terminal.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.id_generator import IDGenerator, IDGeneratorContext
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, Part, TaskState
from a2a.utils.errors import InvalidParamsError
from mcp_types import CallToolResult, ElicitResult, InputRequiredResult, TextContent

from .log import emitir
from .mcp_host import McpHostError
from .parser import PedidoInvalido, parse_pedido
from .trace import novo_trace_id, trace_id_de

FERRAMENTA_RESERVA = "reservar_sala"
ESTADOS_TERMINAIS = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)
# TODO(E7a): substituir por INPUT_REQUIRED + "alternativas: a, b, c" (ponte real).
MSG_PAUSA_PROVISORIA = (
    "Conflito de reserva: a sala pedida esta ocupada e a escolha de alternativa "
    "ainda nao esta implementada nesta versao do agente."
)
# TODO(E7b): substituir pela retomada (escolha=<v>) com o PausedState.
MSG_CONTINUACAO_PROVISORIA = "A continuacao de Tasks pausadas ainda nao esta implementada."
MSG_ERRO_INTERNO = "Erro interno do agente ao processar o pedido."


class GeradorComPrefixo(IDGenerator):
    """Ids no formato do wire (`task-…`, `ctx-…`, `msg-…`, `art-…`): so estetica, o formato e livre."""

    def __init__(self, prefixo: str) -> None:
        self._prefixo = prefixo

    def generate(self, context: IDGeneratorContext) -> str:
        return f"{self._prefixo}{secrets.token_hex(6)}"


# ------------------------------------------------------------------------- estado da ponte
@dataclass
class PausedState:
    """Estado de uma Task pausada (03 §5). Vive SO em memoria, num dict FORA do TaskStore do SDK.

    Nunca e serializado para o cliente A2A. `request_state` e opaco (guardar e ecoar; nunca abrir).
    TODO(E7a): preenchido quando o MCP devolver `InputRequiredResult`.
    """

    task_id: str
    context_id: str
    tool_name: str
    original_arguments: dict[str, str]
    input_request_key: str
    enum: list[str]
    request_state: str = field(repr=False)
    trace_id: str


class PausedRegistry:
    """dict por task_id, separado do TaskStore (nao pode vazar no GetTask/ListTasks)."""

    def __init__(self) -> None:
        self._por_task: dict[str, PausedState] = {}

    def guardar(self, estado: PausedState) -> None:
        self._por_task[estado.task_id] = estado

    def obter(self, task_id: str) -> PausedState | None:
        return self._por_task.get(task_id)

    def limpar(self, task_id: str) -> None:
        self._por_task.pop(task_id, None)

    def __len__(self) -> int:
        return len(self._por_task)


class ServicoMcp(Protocol):
    """O que o executor precisa do host MCP (facilita fakes nos testes)."""

    async def versao_da_politica(self, trace_id: str | None = None) -> str: ...

    async def chamar_ferramenta(
        self,
        nome: str,
        argumentos: dict[str, Any],
        *,
        trace_id: str | None = None,
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult: ...

    async def aclose(self) -> None: ...


# ------------------------------------------------------------------------- traducao (puras)
def texto_da_tool(resultado: CallToolResult) -> str:
    """Texto EXATO devolvido pela tool (blocos de texto; varios blocos separados por linha)."""
    return "\n".join(b.text for b in resultado.content if isinstance(b, TextContent))


def reserva_do_resultado(resultado: CallToolResult) -> dict[str, Any] | None:
    """`structuredContent` da reserva (ou o JSON do bloco de texto). None se nao for uma reserva."""
    dados: Any = resultado.structured_content
    if dados is None:
        try:
            dados = json.loads(texto_da_tool(resultado))
        except ValueError:
            return None
    if not isinstance(dados, dict) or dados.get("reservado") is not True:
        return None
    if not all(isinstance(dados.get(k), str) and dados[k] for k in ("reserva", "sala")):
        return None
    return dados


def artifact_da_reserva(dados: dict[str, Any], politica: str) -> str:
    """Texto do artifact `reserva`: JSON {reserva,sala,inicio,fim,responsavel,politica} (wire 10)."""
    return json.dumps(
        {
            "reserva": dados["reserva"],
            "sala": dados["sala"],
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        },
        ensure_ascii=False,
    )


def mensagem_de_falha_mcp(exc: McpHostError) -> str:
    return f"Nao foi possivel concluir a reserva: {exc.mensagem}"


def resolver_context_id(context: RequestContext) -> str | None:
    """AJUSTE OBRIGATORIO (spike S2): na continuacao o validador NAO manda contextId, e o SDK gera um
    novo em `context.context_id`; usar esse id faz o TaskManager rejeitar ("Context in event doesn't
    match"). O da Task guardada (`current_task.context_id`) e o correto.
    """
    atual = context.current_task
    if atual is not None and atual.context_id:
        return str(atual.context_id)
    return context.context_id


def traceparent_do_request(context: RequestContext) -> str | None:
    cabecalhos = context.call_context.state.get("headers")
    if not isinstance(cabecalhos, dict):
        return None
    valor = cabecalhos.get("traceparent")
    return valor if isinstance(valor, str) else None


# ------------------------------------------------------------------------- executor
class _Saida:
    """Garante que TODA execucao termine a Task (terminal ou pausa): nunca fica em WORKING."""

    def __init__(self, upd: TaskUpdater) -> None:
        self.upd = upd
        self.encerrada = False

    def _mensagem(self, texto: str) -> Message:
        return self.upd.new_agent_message([Part(text=texto)])

    async def _publicar(self, estado: TaskState, texto: str) -> None:
        msg = self._mensagem(texto)
        # O SDK move o status.message ANTERIOR para o `history` quando chega o proximo status. Para
        # o `history` ter tambem a mensagem do agente (wire 08/10 e R-HOST-04), publica-se antes um
        # WORKING carregando a mesma mensagem; o terminal/pausa a empurra para o historico.
        await self.upd.update_status(TaskState.TASK_STATE_WORKING, message=msg)
        await self.upd.update_status(estado, message=msg)
        self.encerrada = True

    async def falhou(self, texto: str) -> None:
        await self._publicar(TaskState.TASK_STATE_FAILED, texto)

    async def concluiu(self, texto: str, artifact_texto: str) -> None:
        await self.upd.add_artifact([Part(text=artifact_texto)], name="reserva")
        await self._publicar(TaskState.TASK_STATE_COMPLETED, texto)

    async def cancelou(self, texto: str) -> None:
        await self._publicar(TaskState.TASK_STATE_CANCELED, texto)


class ExecutorReservas(AgentExecutor):
    def __init__(self, mcp: ServicoMcp, pausadas: PausedRegistry | None = None) -> None:
        self._mcp = mcp
        self.pausadas = pausadas if pausadas is not None else PausedRegistry()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        mensagem = context.message
        task_id = context.task_id
        context_id = resolver_context_id(context)
        if mensagem is None or not task_id or not context_id:
            raise InvalidParamsError(message="Mensagem, task ou contexto ausente")
        atual = context.current_task
        if atual is not None and atual.status.state in ESTADOS_TERMINAIS:
            # O SDK ja recusa antes; defesa em profundidade: terminal e definitivo.
            raise InvalidParamsError(message=f"Task {task_id} esta em estado terminal")

        upd = TaskUpdater(
            event_queue,
            task_id,
            context_id,
            artifact_id_generator=GeradorComPrefixo("art-"),
            message_id_generator=GeradorComPrefixo("msg-"),
        )
        saida = _Saida(upd)
        nova = atual is None
        if nova:
            # O 1o evento DEVE ser a Task (senao o SDK rejeita: "Agent should enqueue Task before ...").
            await event_queue.enqueue_event(new_task_from_user_message(mensagem))
        await upd.start_work()
        try:
            if nova:
                await self._nova(context, saida)
            else:
                await self._continuacao(context, saida)
        except Exception as exc:  # noqa: BLE001 - a Task nunca pode ficar em WORKING
            emitir("erro", task=task_id, tipo=type(exc).__name__)
            if not saida.encerrada:
                await saida.falhou(MSG_ERRO_INTERNO)

    async def _nova(self, context: RequestContext, saida: _Saida) -> None:
        texto = context.get_user_input()
        # R-HOST-03/DEC-19: o trace-id da Task e fixado no 1o pedido; header invalido = ignorado.
        trace_id = trace_id_de(traceparent_do_request(context)) or novo_trace_id()
        try:
            pedido = parse_pedido(texto)
        except PedidoInvalido as exc:
            await saida.falhou(str(exc))  # nao chama o MCP
            return
        try:
            politica = await self._mcp.versao_da_politica(trace_id)
            resultado = await self._mcp.chamar_ferramenta(
                FERRAMENTA_RESERVA, pedido.argumentos(), trace_id=trace_id
            )
        except McpHostError as exc:
            emitir("mcp_falha", task=context.task_id, tipo=exc.tipo)
            await saida.falhou(mensagem_de_falha_mcp(exc))
            return

        if isinstance(resultado, InputRequiredResult):
            # TODO(E7a): pausa (INPUT_REQUIRED + "alternativas: ..."), PausedState em self.pausadas.
            await saida.falhou(MSG_PAUSA_PROVISORIA)
            return
        if resultado.is_error:
            await saida.falhou(texto_da_tool(resultado))  # mensagem EXATA da tool
            return
        reserva = reserva_do_resultado(resultado)
        if reserva is None:
            await saida.falhou("Resposta inesperada do servidor MCP ao reservar a sala.")
            return
        await saida.concluiu(
            f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.",
            artifact_da_reserva(reserva, politica),
        )

    async def _continuacao(self, context: RequestContext, saida: _Saida) -> None:
        # TODO(E7b): escolha=<v> / escolha=recusar sobre o PausedState desta Task.
        await saida.falhou(MSG_CONTINUACAO_PROVISORIA)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = resolver_context_id(context)
        if not task_id or not context_id:
            raise InvalidParamsError(message="Task ou contexto ausente")
        self.pausadas.limpar(task_id)
        upd = TaskUpdater(
            event_queue, task_id, context_id, message_id_generator=GeradorComPrefixo("msg-")
        )
        await _Saida(upd).cancelou("Task cancelada.")
