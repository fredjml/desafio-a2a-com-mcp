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
    descarta a resposta e pergunta de novo (revalidacao, DEC-20).
  * nao se mistura com `InputRequiredResult` manual: o SDK recusa (`InvalidSignature`).
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Literal

from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp_types import CallToolResult
from pydantic import BaseModel, Field, create_model

from .alternatives import alternativas
from .data import Dados
from .policy import MSG_SEM_ALTERNATIVAS, ErroDeDominio, validar_pedido
from .reservations import Agenda
from .tempo import formatar_instante
from .tools import ReservaOut, resultado_erro, resultado_ok

MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
MOTIVO_RECUSADO = "recusado"


class SemConflito(BaseModel):
    """Desfecho do resolvedor: o intervalo esta livre, reserva-se a sala pedida."""


class ErroDeExecucao(BaseModel):
    """Desfecho do resolvedor: erro de dominio (isError) com o texto exato."""

    mensagem: str


class EscolhaFeita(BaseModel):
    """Base do modelo elicitado; o modelo dinamico (enum das alternativas) a especializa."""

    sala: str


Desfecho = SemConflito | ErroDeExecucao | EscolhaFeita


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


def criar_resolvedor(
    dados: Dados, agenda: Agenda
) -> Callable[..., Awaitable[Elicit[Any] | Desfecho]]:
    ids_das_salas = frozenset(s.id for s in dados.salas)

    # `async` sem `await`: roda no event loop (um resolvedor sincrono iria para uma thread e leria a
    # agenda fora do loop, quebrando a atomicidade do check-then-insert do corpo da tool).
    async def escolha_de_sala(
        sala: str, inicio: str, fim: str, ctx: Context[Any, Any]
    ) -> Elicit[Any] | Desfecho:
        try:
            intervalo = validar_pedido(sala, inicio, fim, ids_das_salas)
        except ErroDeDominio as erro:
            return ErroDeExecucao(mensagem=erro.mensagem)
        if not agenda.conflitos(sala, intervalo):
            return SemConflito()
        oferta = alternativas(dados.salas, agenda.reservas, sala, intervalo)
        if not oferta:
            return ErroDeExecucao(mensagem=MSG_SEM_ALTERNATIVAS)
        return Elicit(MENSAGEM_ELICITATION, modelo_de_escolha([s.id for s in oferta]))

    # A chave da elicitation no fio deriva de `modulo:qualname`; fixar o qualname evita `<locals>`.
    escolha_de_sala.__qualname__ = "escolha_de_sala"
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
