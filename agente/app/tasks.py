"""Task A2A: executor (traduz A2A <-> MCP), maquina de estados e estado da ponte (PausedState).

O agente NAO tem regra de sala/politica/alternativas: `execute` so (1) le o pedido, (2) fala MCP,
(3) traduz o resultado em estado A2A. Toda resposta A2A sai do SDK a partir de campos montados aqui
por allowlist (status.message, artifact `reserva`); nenhum objeto interno e serializado.

Estado A2A: SUBMITTED -> WORKING (internos; `GetTask` reflete o corrente) -> terminal | INPUT_REQUIRED.
Terminal (COMPLETED/FAILED/CANCELED) e definitivo: o SDK recusa SendMessage para Task terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.id_generator import IDGenerator, IDGeneratorContext
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from mcp_types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)

from .log import emitir
from .mcp_host import McpHostError
from .parser import FORMATO, PedidoInvalido, parse_escolha, parse_pedido
from .trace import novo_trace_id, trace_da_task, trace_id_de

FERRAMENTA_RESERVA = "reservar_sala"
ESTADOS_TERMINAIS = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)
MSG_ERRO_INTERNO = "Erro interno do agente ao processar o pedido."
MSG_PERGUNTA_INVALIDA = "O servidor MCP pediu uma escolha em formato que o agente nao entende."
MSG_RECUSADA = "Reserva recusada: nenhuma sala foi reservada."
# Estado expirado (vale ~o TTL do requestState no servidor) ou perdido: nao ha como retomar a pausa.
MSG_SEM_ESTADO = (
    "Estado expirado; reenvie o pedido. Esta Task nao tem mais o estado necessario para continuar."
)
MSG_CLIENTE_RECRIADO = (
    "A conexao com o servidor MCP foi refeita desde a pausa e a escolha nao foi enviada, para "
    "nao repetir um id de requisicao. Envie um novo pedido."
)
MSG_INTERROMPIDO = "Processamento interrompido antes de concluir; reenvie o pedido."
MSG_SO_USUARIO = "a mensagem deve ter role ROLE_USER"
ESCOLHA_RECUSAR = "recusar"


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
    Preenchido quando o MCP devolve `InputRequiredResult`; atualizado a cada nova rodada (R-BR-07).
    `campo` = nome da propriedade do formulario recebido (a resposta e `{campo: escolha}`);
    `geracao_cliente` = geracao do Client MCP que fez a chamada (ver `McpHost.geracao`) e `ids_ate` = o
    maior id JSON-RPC que aquele Client ja havia usado (ver `McpHost.evitar_ids`).
    """

    task_id: str
    context_id: str
    tool_name: str
    original_arguments: dict[str, str]
    input_request_key: str
    enum: list[str]
    request_state: str = field(repr=False)
    trace_id: str
    campo: str = "sala"
    geracao_cliente: int = 0
    ids_ate: int = 0
    rodada: int = 1


class PausedRegistry:
    """dict por task_id, separado do TaskStore (nao pode vazar no GetTask).

    Cada estado vale `ttl_s` a partir do ultimo `guardar` (cada rodada renova o requestState no
    servidor). Passado o prazo o estado deixa de existir: `obter` devolve None e `expurgar` (varredura
    preguicosa, chamada a cada SendMessage; sem thread) o remove. Continuar uma Task assim termina em
    FAILED com "estado expirado; reenvie o pedido".
    """

    def __init__(self, ttl_s: float = 660.0, relogio: Callable[[], float] = time.monotonic) -> None:
        self._por_task: dict[str, tuple[PausedState, float]] = {}
        self._ttl_s = ttl_s
        self._relogio = relogio

    def guardar(self, estado: PausedState) -> None:
        self._por_task[estado.task_id] = (estado, self._relogio())

    def _expirado(self, guardado_em: float) -> bool:
        return self._relogio() - guardado_em > self._ttl_s

    def obter(self, task_id: str) -> PausedState | None:
        item = self._por_task.get(task_id)
        if item is None:
            return None
        if self._expirado(item[1]):
            del self._por_task[task_id]
            return None
        return item[0]

    def limpar(self, task_id: str) -> None:
        self._por_task.pop(task_id, None)

    def expurgar(self) -> int:
        """Remove os estados vencidos; devolve quantos."""
        vencidos = [i for i, (_, em) in self._por_task.items() if self._expirado(em)]
        for task_id in vencidos:
            del self._por_task[task_id]
        return len(vencidos)

    def __len__(self) -> int:
        return len(self._por_task)


class ServicoMcp(Protocol):
    """O que o executor precisa do host MCP (facilita fakes nos testes)."""

    @property
    def geracao(self) -> int:
        """Geracao do Client MCP que atenderia uma chamada agora (muda quando o Client e recriado)."""
        ...

    @property
    def ids_emitidos(self) -> int:
        """Requests com id enviados pelo Client vivo (cota superior do maior id ja usado)."""
        ...

    async def evitar_ids(
        self, geracao_origem: int, ids_ate: int, trace_id: str | None = None
    ) -> bool:
        """True se o proximo id do retry nao coincide com o da chamada original (Client recriado)."""
        ...

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


@dataclass(frozen=True)
class Pergunta:
    """A elicitation RECEBIDA do servidor, reduzida ao que a ponte precisa (nada e recalculado)."""

    chave: str
    campo: str
    opcoes: list[str]
    request_state: str = field(repr=False)


def texto_alternativas(opcoes: list[str]) -> str:
    """Texto EXATO da pausa (R-BR-01): `alternativas: a, b, c`, sem prefixo, saudacao nem ponto."""
    return "alternativas: " + ", ".join(opcoes)


def _opcoes_do_campo(propriedade: Any) -> list[str] | None:
    if not isinstance(propriedade, dict):
        return None
    bruto = propriedade.get("enum")
    if bruto is None and "const" in propriedade:
        bruto = [propriedade["const"]]  # 1 alternativa: o SDK do servidor emite `const`
    if not isinstance(bruto, list) or not bruto:
        return None
    if not all(isinstance(v, str) and v for v in bruto):
        return None
    return list(bruto)


def extrair_pergunta(resultado: InputRequiredResult) -> Pergunta | None:
    """Le chave, opcoes (`enum` ou `const` do requestedSchema RECEBIDO) e requestState (opaco).

    None se o formato nao for o esperado (1 pedido de formulario com opcoes, e um requestState).
    """
    pedidos = resultado.input_requests or {}
    estado = resultado.request_state
    if len(pedidos) != 1 or not isinstance(estado, str) or not estado:
        return None
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, ElicitRequest) or not isinstance(
        pedido.params, ElicitRequestFormParams
    ):
        return None
    propriedades = pedido.params.requested_schema.get("properties")
    if not isinstance(propriedades, dict) or not propriedades:
        return None
    campo = "sala" if "sala" in propriedades else next(iter(propriedades))
    opcoes = _opcoes_do_campo(propriedades[campo])
    if opcoes is None:
        return None
    return Pergunta(chave=chave, campo=str(campo), opcoes=opcoes, request_state=estado)


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
        self.pausada = False

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

    async def pausou(self, texto: str) -> None:
        """INPUT_REQUIRED com a mensagem (history fica [user, agent], como no wire 08)."""
        await self._publicar(TaskState.TASK_STATE_INPUT_REQUIRED, texto)
        self.pausada = True


def _task_inicial(mensagem: Message, task_id: str, context_id: str) -> Task:
    """A Task SUBMITTED com a mensagem do usuario no historico (o 1o evento do executor)."""
    return Task(
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        id=task_id,
        context_id=context_id,
        history=[mensagem],
    )


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
        removidos = self.pausadas.expurgar()  # varredura preguicosa de pausas vencidas
        if removidos:
            emitir("ponte", acao="pausas_expiradas", quantidade=removidos)
        nova = atual is None
        if nova:
            # O 1o evento DEVE ser a Task (senao o SDK rejeita: "Agent should enqueue Task before ...").
            # Montada aqui, e nao por `new_task_from_user_message`, que levanta ValueError (=> -32603 com
            # traceback e Task orfa) para texto vazio ou role != USER: esses casos viram FAILED com
            # mensagem clara em `_nova`.
            await event_queue.enqueue_event(_task_inicial(mensagem, task_id, context_id))
        interrompida = False
        try:
            await upd.start_work()
            if nova:
                await self._nova(context, saida)
            else:
                await self._continuacao(context, saida)
        except asyncio.CancelledError:
            # F-15: cancelamento (shutdown/cliente): a Task nao fica em WORKING. Se nem isso der para
            # publicar, o PausedState e preservado (ver `finally`).
            interrompida = True
            if not saida.encerrada and not saida.pausada:
                with contextlib.suppress(Exception):  # melhor esforco: ja estamos cancelados
                    await saida.falhou(MSG_INTERROMPIDO)
            raise
        except Exception:  # noqa: BLE001 - ultima rede (F-04): a Task nunca pode ficar em WORKING
            # Bug de programacao (tipos de infraestrutura ja viram McpHostError no host): loga o
            # traceback, sem o requestState, e responde so "Erro interno".
            emitir("erro", task=task_id, traceback=self._traceback_sem_estado(task_id))
            if not saida.encerrada:
                await saida.falhou(MSG_ERRO_INTERNO)
        finally:
            # terminal (ou falha): o estado da ponte nao sobrevive; cancelado sem publicar o fim, fica
            if not saida.pausada and (saida.encerrada or not interrompida):
                self.pausadas.limpar(task_id)

    def _traceback_sem_estado(self, task_id: str) -> str:
        """Traceback da excecao corrente, sem o `requestState` (opaco) da Task, limitado em tamanho."""
        texto = traceback.format_exc()
        estado = self.pausadas.obter(task_id)
        if estado is not None and estado.request_state:
            texto = texto.replace(estado.request_state, "<requestState omitido>")
        return texto[-6000:]

    async def _nova(self, context: RequestContext, saida: _Saida) -> None:
        texto = context.get_user_input()
        # R-HOST-03/DEC-19: o trace-id da Task e fixado no 1o pedido; header invalido = ignorado.
        trace_id = trace_id_de(traceparent_do_request(context)) or novo_trace_id()
        if context.message is not None and context.message.role != Role.ROLE_USER:
            await saida.falhou(f"Pedido invalido: {MSG_SO_USUARIO}. Formato: {FORMATO}")
            return
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
            await self._pausar(context, saida, pedido.argumentos(), trace_id, resultado)
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

    async def _pausar(
        self,
        context: RequestContext,
        saida: _Saida,
        argumentos: dict[str, str],
        trace_id: str,
        resultado: InputRequiredResult,
    ) -> None:
        """MCP respondeu `input_required`: guarda o PausedState e poe a Task em INPUT_REQUIRED."""
        pergunta = extrair_pergunta(resultado)
        if pergunta is None:
            await saida.falhou(MSG_PERGUNTA_INVALIDA)
            return
        task_id = str(context.task_id)
        self.pausadas.guardar(  # ANTES de publicar: a continuacao pode chegar logo apos a resposta
            PausedState(
                task_id=task_id,
                context_id=str(resolver_context_id(context)),
                tool_name=FERRAMENTA_RESERVA,
                original_arguments=dict(argumentos),
                input_request_key=pergunta.chave,
                enum=list(pergunta.opcoes),
                request_state=pergunta.request_state,
                trace_id=trace_id,
                campo=pergunta.campo,
                geracao_cliente=self._mcp.geracao,
                ids_ate=self._mcp.ids_emitidos,
            )
        )
        emitir(
            "ponte", acao="pausada", task=task_id, trace_id=trace_id, opcoes=len(pergunta.opcoes)
        )
        await saida.pausou(texto_alternativas(pergunta.opcoes))

    async def _continuacao(self, context: RequestContext, saida: _Saida) -> None:
        """Retomada: `escolha=<v>` (v nas opcoes RECEBIDAS) ou `escolha=recusar`, sobre o PausedState."""
        task_id = str(context.task_id)
        estado = self.pausadas.obter(task_id)
        if estado is None:  # Task nao terminal sem estado guardado: nao ha como retomar
            await saida.falhou(MSG_SEM_ESTADO)
            return
        lista = texto_alternativas(estado.enum)
        if context.message is not None and context.message.role != Role.ROLE_USER:
            emitir("ponte", acao="resposta_invalida", task=task_id)
            await saida.pausou(f"Resposta invalida: {MSG_SO_USUARIO}.\n{lista}")
            return
        # R-HOST-03/DEC-19: o trace-id da Task foi fixado no 1o pedido; um traceparent novo NAO o troca.
        trace_id, ignorado = trace_da_task(estado.trace_id, traceparent_do_request(context))
        if ignorado:
            emitir("trace", acao="cabecalho_ignorado", task=task_id, trace_id=trace_id)
        try:
            valor = parse_escolha(context.get_user_input())
        except PedidoInvalido as exc:  # nao e `escolha=...`: resposta clara, sem chamar o MCP
            emitir("ponte", acao="resposta_invalida", task=task_id)
            await saida.pausou(f"{exc}\n{lista}")
            return
        if valor == ESCOLHA_RECUSAR:
            resposta = ElicitResult(action="decline")
        elif valor in estado.enum:
            resposta = ElicitResult(action="accept", content={estado.campo: valor})
        else:  # fora das opcoes (vazio, maiusculas, espacos, sala-xyz): mesma lista, estado intacto
            emitir("ponte", acao="escolha_fora_das_opcoes", task=task_id)
            await saida.pausou(lista)
            return

        aceita = resposta.action == "accept"
        try:
            # A politica vem do resource, lida NESTA Task, antes do retry (que pode criar a reserva).
            politica = await self._mcp.versao_da_politica(trace_id) if aceita else ""
            if self._mcp.geracao != estado.geracao_cliente and not await self._mcp.evitar_ids(
                estado.geracao_cliente, estado.ids_ate, trace_id
            ):
                # Client recriado desde a pausa (ids recomecam em 1) e nao deu para garantir que o retry
                # use um id diferente do da chamada inicial: nao envia (F-02).
                emitir("ponte", acao="cliente_recriado", task=task_id)
                await saida.falhou(MSG_CLIENTE_RECRIADO)
                return
            emitir("ponte", acao="retomada", task=task_id, trace_id=trace_id, aceita=aceita)
            resultado = await self._mcp.chamar_ferramenta(
                estado.tool_name,
                dict(estado.original_arguments),
                trace_id=trace_id,
                input_responses={estado.input_request_key: resposta},
                request_state=estado.request_state,
            )
        except McpHostError as exc:
            emitir("mcp_falha", task=task_id, tipo=exc.tipo)
            if exc.transporte:
                # F-01: falha de TRANSPORTE (conexao/timeout): o requestState ainda vale no servidor e o
                # PausedState fica INTACTO; a Task segue INPUT_REQUIRED para o usuario reenviar. O retry
                # pode ate ter chegado: o servidor revalida e nao duplica a reserva.
                await saida.pausou(f"Servidor MCP indisponivel; reenvie escolha={valor}")
                return
            await saida.falhou(mensagem_de_falha_mcp(exc))  # protocolo (-32602 etc.) ou erro logico
            return

        if isinstance(resultado, InputRequiredResult):  # nova rodada (R-BR-07): segue pausada
            pergunta = extrair_pergunta(resultado)
            if pergunta is None:
                await saida.falhou(MSG_PERGUNTA_INVALIDA)
                return
            self.pausadas.guardar(
                replace(
                    estado,
                    input_request_key=pergunta.chave,
                    campo=pergunta.campo,
                    enum=list(pergunta.opcoes),
                    request_state=pergunta.request_state,
                    geracao_cliente=self._mcp.geracao,
                    ids_ate=self._mcp.ids_emitidos,
                    rodada=estado.rodada + 1,
                )
            )
            emitir("ponte", acao="nova_rodada", task=task_id, rodada=estado.rodada + 1)
            await saida.pausou(texto_alternativas(pergunta.opcoes))
            return
        if resultado.is_error:
            await saida.falhou(texto_da_tool(resultado))  # mensagem EXATA da tool
            return
        if not aceita:
            await saida.cancelou(MSG_RECUSADA)
            return
        reserva = reserva_do_resultado(resultado)
        if reserva is None:
            await saida.falhou("Resposta inesperada do servidor MCP ao reservar a sala.")
            return
        await saida.concluiu(
            f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.",
            artifact_da_reserva(reserva, politica),
        )

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
