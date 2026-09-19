"""Ciclo MRTR de `reservar_sala` (DEC-24: Elicit/Resolve). Conflito => input_required.

Este modulo NAO usa `from __future__ import annotations` de proposito: o SDK resolve as anotacoes da
tool com `get_type_hints`, e `Annotated[..., Resolve(resolvedor)]` referencia um objeto criado em
`registrar_reserva` (nao existe no namespace do modulo). Anotacao avaliada na definicao funciona.

Como funciona (mcp 2.2.0, protocolo >= 2026-07-28):
  * o RESOLVEDOR `escolha_de_sala` roda antes do corpo da tool e devolve UM de:
      - `Elicit(...)`         conflito COM alternativa  => o SDK responde `input_required`
                              (1 inputRequest, chave atribuida pelo SDK, requestState selado);
      - `SemConflito()`       intervalo livre           => o corpo reserva direto;
      - `ErroDeExecucao(...)` pedido invalido ou conflito SEM alternativa => o corpo devolve
                              CallToolResult(is_error=True) com o texto EXATO. NAO se levanta excecao
                              no resolvedor: o SDK prefixaria "Error executing tool ...".
  * o SDK guarda em `requestState` so o DESFECHO ja respondido, selado (AES-256-GCM, chave de
    REQUEST_STATE_SECRET) e vinculado a metodo, nome da tool e digest dos argumentos. O servidor NAO
    guarda nada entre `input_required` e o retry; nao ha nonce (o estado e reutilizavel).
  * o resolvedor roda de novo a cada rodada: se as alternativas mudaram, a pergunta muda e o SDK
    descarta a resposta e pergunta de novo (revalidacao, DEC-20). Excecoes decididas aqui: decline/cancel
    sempre concluem sem reservar; retry sem requestState (ausente/null) e -32602.
  * nao se mistura com `InputRequiredResult` manual: o SDK recusa (`InvalidSignature`).
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Literal

from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, MISSING_REQUIRED_CLIENT_CAPABILITY, CallToolResult
from pydantic import BaseModel, Field, create_model

from .alternatives import alternativas
from .data import Dados
from .policy import MSG_SEM_ALTERNATIVAS, ErroDeDominio, validar_pedido, validar_responsavel
from .reservations import Agenda
from .tempo import formatar_instante
from .tools import ReservaOut, resultado_erro, resultado_ok

MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
MOTIVO_RECUSADO = "recusado"
# Chave de inputRequests atribuida pelo SDK: `modulo:qualname` do resolvedor (qualname fixado abaixo).
CHAVE_DA_ELICITATION = "app.mrtr:escolha_de_sala"
MSG_ESTADO_INVALIDO = (
    "Invalid or expired requestState"  # a mesma que o SDK usa para selo/ttl/vinculo
)


class SemConflito(BaseModel):
    """Desfecho do resolvedor: o intervalo esta livre, reserva-se a sala pedida."""


class Recusado(BaseModel):
    """Desfecho do resolvedor: o cliente recusou (decline/cancel): conclui sem reservar."""


class ErroDeExecucao(BaseModel):
    """Desfecho do resolvedor: erro de dominio (isError) com o texto exato."""

    mensagem: str


class EscolhaFeita(BaseModel):
    """Base do modelo elicitado; o modelo dinamico (enum das alternativas) a especializa."""

    sala: str


Desfecho = SemConflito | Recusado | ErroDeExecucao | EscolhaFeita


def modelo_de_escolha(salas: Sequence[str]) -> type[EscolhaFeita]:
    """Modelo do formulario: `sala` restrita as alternativas (1 => const, varias => enum)."""
    return create_model(
        "EscolhaDeSala",
        __base__=EscolhaFeita,
        sala=(
            Literal[tuple(salas)],
            Field(title="Sala", description="Sala alternativa escolhida"),
        ),
    )


def estado_ausente_no_retry(ctx: Context[Any, Any]) -> bool:
    """Retry (traz `inputResponses`) sem `requestState` (ausente ou null): nao ha o que retomar."""
    return bool(ctx.input_responses) and not ctx.request_state


def declarou_elicitation_form(ctx: Context[Any, Any]) -> bool:
    """So `elicitation.form` serve. `{}` e `{"elicitation":{}}` NAO contam (o SDK aceitaria o 2o)."""
    capacidades = ctx.client_capabilities
    elicitation = capacidades.elicitation if capacidades is not None else None
    return elicitation is not None and elicitation.form is not None


def cliente_recusou(ctx: Context[Any, Any]) -> bool:
    """Alguma resposta do retry e decline/cancel (o requestState ja foi verificado pelo SDK)."""
    respostas = ctx.input_responses or {}
    return any(getattr(r, "action", None) in ("decline", "cancel") for r in respostas.values())


def criar_resolvedor(
    dados: Dados, agenda: Agenda
) -> Callable[..., Awaitable[Elicit[Any] | Desfecho]]:
    ids_das_salas = frozenset(s.id for s in dados.salas)

    # `async` sem `await`: roda no event loop (um resolvedor sincrono iria para uma thread e leria a
    # agenda fora do loop, quebrando a atomicidade do check-then-insert do corpo da tool).
    async def escolha_de_sala(
        sala: str, inicio: str, fim: str, responsavel: str, ctx: Context[Any, Any]
    ) -> Elicit[Any] | Desfecho:
        if estado_ausente_no_retry(ctx):
            # O SDK trata `requestState` ausente/null como "sem progresso" e re-perguntaria; um retry
            # sem estado e entrada invalida (a spec pede o estado ecoado): mesmo erro do selo invalido.
            raise MCPError(
                code=INVALID_PARAMS,
                message=MSG_ESTADO_INVALIDO,
                data={"reason": "invalid_request_state"},
            )
        try:
            intervalo = validar_pedido(sala, inicio, fim, ids_das_salas)
            validar_responsavel(responsavel)  # antes de perguntar: nao pausar pedido invalido
        except ErroDeDominio as erro:
            return ErroDeExecucao(mensagem=erro.mensagem)
        if cliente_recusou(ctx):
            # decline/cancel vale sempre (o SDK ja verificou o requestState): a pergunta pode ter mudado
            # desde a recusa (outra Task pegou uma alternativa) e re-perguntar a quem recusou nao serve.
            return Recusado()
        if not agenda.conflitos(sala, intervalo):
            # Revalidacao (DEC-20): o conflito que gerou a pergunta sumiu (ex.: restart). Um accept
            # reserva a sala PEDIDA, nao a alternativa escolhida sobre uma pergunta obsoleta.
            return SemConflito()
        oferta = alternativas(dados.salas, agenda.reservas, sala, intervalo)
        if not oferta:
            return ErroDeExecucao(mensagem=MSG_SEM_ALTERNATIVAS)  # sem elicitation: nada a exigir
        if not declarou_elicitation_form(ctx):
            raise MCPError(  # -32021 (HTTP 400 pelo transporte); nenhuma reserva foi criada
                code=MISSING_REQUIRED_CLIENT_CAPABILITY,
                message=(
                    "Client did not declare the form elicitation capability required by "
                    f"resolver '{CHAVE_DA_ELICITATION}'"
                ),
                data={"requiredCapabilities": {"elicitation": {"form": {}}}},
            )
        return Elicit(MENSAGEM_ELICITATION, modelo_de_escolha([s.id for s in oferta]))

    # A chave da elicitation no fio deriva de `modulo:qualname`; fixar o qualname evita `<locals>`.
    escolha_de_sala.__qualname__ = "escolha_de_sala"
    if f"{escolha_de_sala.__module__}:{escolha_de_sala.__qualname__}" != CHAVE_DA_ELICITATION:
        raise RuntimeError("chave da elicitation divergente do esperado")  # pragma: no cover
    return escolha_de_sala


def recusada() -> CallToolResult:
    """decline/cancel: conclui SEM reservar e SEM isError (wire 11)."""
    return resultado_ok(ReservaOut(reservado=False, motivo=MOTIVO_RECUSADO))


def registrar_reserva(mcp: MCPServer, dados: Dados, agenda: Agenda) -> None:
    ids_das_salas = frozenset(s.id for s in dados.salas)
    resolvedor = criar_resolvedor(dados, agenda)

    @mcp.tool(
        name="reservar_sala",
        description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
    )
    async def reservar_sala(
        sala: str,
        inicio: str,
        fim: str,
        responsavel: str,
        escolha: Annotated[ElicitationResult[BaseModel], Resolve(resolvedor)],
    ) -> Annotated[CallToolResult, ReservaOut]:
        if not isinstance(escolha, AcceptedElicitation):  # decline / cancel
            return recusada()
        desfecho = escolha.data
        if isinstance(desfecho, ErroDeExecucao):
            return resultado_erro(desfecho.mensagem)
        if isinstance(desfecho, Recusado):
            return recusada()
        if isinstance(desfecho, SemConflito):
            alvo = sala
        elif isinstance(desfecho, EscolhaFeita):
            alvo = desfecho.sala
        else:  # desfecho desconhecido: nunca reservar
            return resultado_erro(MSG_SEM_ALTERNATIVAS)
        try:
            intervalo = validar_pedido(alvo, inicio, fim, ids_das_salas)
        except ErroDeDominio as erro:
            return resultado_erro(erro.mensagem)
        # Ultima barreira (sem `await` entre esta checagem e o `criar`): nunca criar reserva sobreposta.
        if agenda.conflitos(alvo, intervalo):
            return resultado_erro(MSG_SEM_ALTERNATIVAS)
        reserva = agenda.criar(alvo, intervalo, responsavel)
        return resultado_ok(
            ReservaOut(
                reserva=reserva.id,
                reservado=True,
                sala=reserva.sala,
                inicio=formatar_instante(reserva.inicio),
                fim=formatar_instante(reserva.fim),
                responsavel=reserva.responsavel,
                politica=dados.politica_versao,
                motivo=None,
            )
        )
