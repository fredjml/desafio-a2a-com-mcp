"""Parser estrito do pedido em texto (R-PARSE-01). So TRADUZ: nenhuma regra de sala/politica/janela.

Formato aceito (uma unica linha; `\\r\\n` final tolerado; chaves em qualquer ordem):

    reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>

`responsavel` pode ter espacos: vale o texto ate a proxima chave conhecida ou o fim da linha.
Chave desconhecida, duplicada, ausente, valor vazio ou quebra de linha embutida = erro claro, e o
MCP nem e chamado. Validar existencia da sala, janela, duracao ou intervalo e trabalho do SERVIDOR.
A checagem de ISO 8601 aqui e so de FORMA (para nao repassar lixo); o valor e repassado verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

FORMATO = "reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>"
CAMPOS = ("sala", "inicio", "fim", "responsavel")
MAX_TEXTO = 1000
MAX_CAMPO = 200

_CHAVE = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)=")
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?"
)
_CONTROLE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\x85]")


class PedidoInvalido(Exception):
    """Pedido fora do formato. `str(exc)` e a mensagem clara devolvida na Task FAILED."""


@dataclass(frozen=True)
class Pedido:
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def argumentos(self) -> dict[str, str]:
        """Argumentos de `reservar_sala`, verbatim (nada normalizado)."""
        return {
            "sala": self.sala,
            "inicio": self.inicio,
            "fim": self.fim,
            "responsavel": self.responsavel,
        }


def _erro(motivo: str) -> PedidoInvalido:
    return PedidoInvalido(f"Pedido invalido: {motivo}. Formato: {FORMATO}")


def _erro_de_resposta(motivo: str) -> PedidoInvalido:
    return PedidoInvalido(f"Resposta invalida: {motivo}. Use escolha=<sala> ou escolha=recusar")


def _linha_unica(texto: str, erro: Callable[[str], PedidoInvalido] = _erro) -> str:
    if len(texto) > MAX_TEXTO:
        raise erro(f"texto acima de {MAX_TEXTO} caracteres")
    corpo = texto.strip()  # tolera \r\n e espacos nas pontas
    if not corpo:
        raise erro("texto vazio")
    if _CONTROLE.search(corpo):
        raise erro("deve ser uma unica linha, sem caracteres de controle")
    return corpo


def _iso_de_forma(campo: str, valor: str) -> None:
    achou = _ISO.fullmatch(valor)
    if achou is None:
        raise _erro(f"{campo} nao e um instante ISO 8601 (ex.: 2026-11-03T14:00:00-03:00)")
    ano, mes, dia, hora, minuto, segundo = (int(g or 0) for g in achou.groups()[:6])
    try:
        datetime(ano, mes, dia, hora, minuto, segundo, tzinfo=timezone.utc)
    except ValueError:
        raise _erro(f"{campo} nao e um instante ISO 8601 valido") from None
    zona = achou.group(7)
    if zona and zona != "Z":
        h, m = int(zona[1:3]), int(zona[4:6])
        if h > 23 or m > 59:
            raise _erro(f"{campo} tem fuso horario invalido")


def parse_pedido(texto: str) -> Pedido:
    """`reservar ...` -> Pedido, ou PedidoInvalido com mensagem clara."""
    corpo = _linha_unica(texto)
    if corpo == "reservar" or not corpo.startswith("reservar") or not corpo[8:9].isspace():
        raise _erro("o pedido deve comecar com 'reservar'")
    resto = corpo[len("reservar") :].strip()
    marcas = list(_CHAVE.finditer(resto))
    if not marcas or resto[: marcas[0].start()].strip():
        raise _erro("esperava campos no formato chave=valor logo apos 'reservar'")

    valores: dict[str, str] = {}
    for i, marca in enumerate(marcas):
        chave = marca.group(1)
        fim_do_valor = marcas[i + 1].start() if i + 1 < len(marcas) else len(resto)
        valor = resto[marca.end() : fim_do_valor].strip()
        if chave not in CAMPOS:
            raise _erro(f"campo desconhecido '{chave}'")
        if chave in valores:
            raise _erro(f"campo '{chave}' repetido")
        if not valor:
            raise _erro(f"campo '{chave}' sem valor")
        if len(valor) > MAX_CAMPO:
            raise _erro(f"campo '{chave}' acima de {MAX_CAMPO} caracteres")
        if chave != "responsavel" and any(c.isspace() for c in valor):
            raise _erro(f"campo '{chave}' nao pode conter espacos nem texto solto")
        valores[chave] = valor

    faltando = [c for c in CAMPOS if c not in valores]
    if faltando:
        raise _erro("faltando " + ", ".join(faltando))
    _iso_de_forma("inicio", valores["inicio"])
    _iso_de_forma("fim", valores["fim"])
    return Pedido(
        sala=valores["sala"],
        inicio=valores["inicio"],
        fim=valores["fim"],
        responsavel=valores["responsavel"],
    )


def parse_escolha(texto: str) -> str:
    """`escolha=<v>` -> v (verbatim, pode ser vazio/com espacos: quem valida contra o enum e a ponte).

    Levanta PedidoInvalido se o texto nao comeca com `escolha=` ou tem quebra de linha embutida.
    """
    corpo = _linha_unica(texto, _erro_de_resposta)
    if not corpo.startswith("escolha="):
        raise _erro_de_resposta("esperava escolha=<valor>")
    return corpo[len("escolha=") :].strip()
