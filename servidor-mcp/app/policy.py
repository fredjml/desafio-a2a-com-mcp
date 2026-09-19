"""Politica de uso: unico lugar com as regras de sala, janela e duracao (a reserva reutiliza).

Ordem das validacoes (a mesma do enunciado e dos checks 09-12 do validador):
  1. sala existe            -> "Sala inexistente: <id>"
  2. formato ISO com fuso   -> "Formato invalido: ..." (texto proprio, o enunciado nao define)
  3. fim > inicio           -> "Intervalo invalido: ..."
  4. janela 08:00-20:00     -> "Fora da janela de uso: ..."  (inclusiva nas duas pontas, em -03:00)
  5. duracao <= 2 horas     -> "Duracao acima do limite: ..."

Nenhuma regra depende da data de hoje: reservar no passado e permitido.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .tempo import FUSO_SP, MSG_FORMATO, FormatoInvalido, interpretar_instante

__all__ = [
    "DURACAO_MAXIMA",
    "MSG_DURACAO",
    "MSG_FORMATO",  # definida em tempo.py (junto do parser); as mensagens de erro sao reexportadas aqui
    "MSG_INTERVALO",
    "MSG_JANELA",
    "MSG_RESPONSAVEL",
    "MSG_SEM_ALTERNATIVAS",
    "ErroDeDominio",
    "Intervalo",
    "msg_sala_inexistente",
    "validar_pedido",
    "validar_responsavel",
]

MSG_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVAS = "Sem alternativas disponiveis no intervalo"
# Texto proprio (o enunciado nao define): nao e uma das cinco mensagens exatas; o validador nao a testa.
MSG_RESPONSAVEL = "Formato invalido: responsavel deve ter de 1 a 200 caracteres"
RESPONSAVEL_MAX = 200

JANELA_ABRE = time(8, 0)
JANELA_FECHA = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)


def msg_sala_inexistente(sala: str) -> str:
    return f"Sala inexistente: {sala}"


class ErroDeDominio(Exception):
    """Erro de EXECUCAO da tool (vira isError:true). `mensagem` e o texto exato devolvido."""

    def __init__(self, mensagem: str) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime  # normalizados para -03:00
    fim: datetime


def validar_responsavel(responsavel: str) -> None:
    """`responsavel` obrigatorio: 1 a 200 caracteres (so espacos conta como vazio)."""
    if not responsavel.strip() or len(responsavel) > RESPONSAVEL_MAX:
        raise ErroDeDominio(MSG_RESPONSAVEL)


def validar_pedido(sala: str, inicio: str, fim: str, salas_existentes: Container[str]) -> Intervalo:
    """Valida sala + intervalo. Levanta ErroDeDominio com a mensagem exata da primeira regra violada."""
    if sala not in salas_existentes:
        raise ErroDeDominio(msg_sala_inexistente(sala))
    try:
        ini = interpretar_instante(inicio)
        fi = interpretar_instante(fim)
    except FormatoInvalido:
        raise ErroDeDominio(MSG_FORMATO) from None
    if fi <= ini:
        raise ErroDeDominio(MSG_INTERVALO)
    # A janela vale para o dia do inicio, no horario de Sao Paulo (instantes ja normalizados).
    dia = ini.date()
    abre = datetime.combine(dia, JANELA_ABRE, tzinfo=FUSO_SP)
    fecha = datetime.combine(dia, JANELA_FECHA, tzinfo=FUSO_SP)
    if ini < abre or fi > fecha:
        raise ErroDeDominio(MSG_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeDominio(MSG_DURACAO)
    return Intervalo(ini, fi)
