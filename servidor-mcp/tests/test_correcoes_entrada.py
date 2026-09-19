"""Correcoes dos reviews (seguranca M3/L1/L2/L3, codigo F-14): entrada hostil, log e segredo."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

import pytest

from app.config import ConfigError, validar_segredo
from app.log import campos_do_request, emitir
from app.policy import MSG_RESPONSAVEL

from .servidor_proc import Servidor

DIA = "2026-11-03"
CT = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


# ---------------------------------------------------------------- M3: JSON aninhado
@pytest.mark.parametrize("corpo", ["[" * 100_000, '{"a":' * 100_000], ids=["arrays", "objetos"])
def test_log_nao_quebra_com_json_muito_aninhado(corpo: str) -> None:
    campos = campos_do_request(corpo.encode(), {})  # antes: RecursionError
    assert campos["method"] is None and campos["id"] is None


@pytest.mark.parametrize("corpo", [b"[" * 100_000, b'{"a":' * 100_000], ids=["arrays", "objetos"])
def test_json_aninhado_devolve_32700_sem_traceback_e_sem_derrubar(
    fresco: Servidor, corpo: bytes
) -> None:
    resp = fresco.cliente.post(fresco.url, content=corpo, headers=CT)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700
    # o processo segue vivo e atende um request valido
    assert fresco.rpc("tools/list").status_code == 200
    time.sleep(0.3)  # a thread leitora do stderr e assincrona
    stderr = "\n".join(fresco.stderr)
    assert "Traceback" not in stderr and "RecursionError" not in stderr
    # o request hostil tambem gera a linha de auditoria
    assert [linha for linha in fresco.linhas_de_log(status=400, http="POST /mcp")]


# ---------------------------------------------------------------- L2: log em ASCII
def test_log_escapa_separadores_de_linha_unicode(capsys: pytest.CaptureFixture[str]) -> None:
    emitir("teste", valor="a\u0085b\u2028c\u2029d\x1b[2Je\nf")
    saida = capsys.readouterr().err
    assert saida.isascii()
    assert len(saida.splitlines()) == 1  # nada quebra a linha nem com splitlines()
    assert json.loads(saida)["valor"] == "a\u0085b\u2028c\u2029d\x1b[2Je\nf"


# ---------------------------------------------------------------- L1/F-14: responsavel
def _args(responsavel: str) -> dict[str, Any]:
    return {
        "sala": "sala-porao",
        "inicio": f"{DIA}T09:00:00-03:00",
        "fim": f"{DIA}T10:00:00-03:00",
        "responsavel": responsavel,
    }


@pytest.mark.parametrize(
    "responsavel", ["", "   ", "x" * 201, "y" * 5000], ids=["vazio", "espacos", "201", "5000"]
)
def test_responsavel_invalido_e_erro_de_execucao_e_nao_reserva(
    fresco: Servidor, responsavel: str
) -> None:
    res = fresco.tool("reservar_sala", _args(responsavel))
    assert res["isError"] is True
    assert [b["text"] for b in res["content"]] == [MSG_RESPONSAVEL]
    assert MSG_RESPONSAVEL == "Formato invalido: responsavel deve ter de 1 a 200 caracteres"
    consulta = fresco.tool(
        "consultar_disponibilidade",
        {"sala": "sala-porao", "inicio": f"{DIA}T09:00:00-03:00", "fim": f"{DIA}T10:00:00-03:00"},
    )
    assert consulta["structuredContent"]["livre"] is True  # nenhuma reserva foi criada


@pytest.mark.parametrize("tamanho", [1, 200])
def test_responsavel_nos_limites_e_aceito(fresco: Servidor, tamanho: int) -> None:
    res = fresco.tool("reservar_sala", _args("r" * tamanho))
    assert res["isError"] is False
    assert res["structuredContent"]["responsavel"] == "r" * tamanho


def test_responsavel_invalido_nao_pausa_pedido_em_conflito(fresco: Servidor) -> None:
    assert fresco.tool("reservar_sala", _args("Doc"))["isError"] is False
    res = fresco.tool(
        "reservar_sala", _args("")
    )  # mesmo horario: conflito, mas o pedido e invalido
    assert res["isError"] is True and res.get("resultType") != "input_required"
    assert res["content"][0]["text"] == MSG_RESPONSAVEL


# ---------------------------------------------------------------- L3: entropia do segredo
@pytest.mark.parametrize(
    "bruto",
    [
        bytes(range(32)),  # 00 01 02 ... 1f: progressao
        bytes.fromhex("1234567890abcdef" * 4),  # periodo 8
        bytes.fromhex("0102030405060708" * 4),  # periodo 8
        bytes.fromhex("deadbeef" * 8),  # periodo 4
        bytes.fromhex("ab" * 32),  # periodo 1
        bytes.fromhex("00ff" * 16),  # periodo 2
        bytes(range(15)) + bytes([0] * 17),  # 15 distintos (< 16) sem periodo
        bytes([1, 2, 3, 4, 5, 6, 7, 9, 11, 13]) + bytes(22),  # 11 distintos, maioria zero
    ],
    ids=[
        "sequencial",
        "periodo8-a",
        "periodo8-b",
        "deadbeef",
        "um-byte",
        "dois-bytes",
        "15-distintos",
        "metade-zero",
    ],
)
def test_segredo_com_padrao_e_recusado(bruto: bytes) -> None:
    segredo = bruto.hex()
    assert len(segredo) == 64
    with pytest.raises(ConfigError, match="aleatoriedade"):
        validar_segredo(segredo)


def test_periodo_acima_de_8_com_16_distintos_ainda_e_aceito() -> None:
    # 16 bytes distintos repetidos 2x: periodo 16 (> 8) e 16 distintos (>= 16) => dentro da regra.
    bruto = bytes.fromhex("3a91c47e0f52d8b6a3e1749c2b60fd85") * 2
    assert len(set(bruto)) == 16
    segredo = bruto.hex()
    assert validar_segredo(segredo) == segredo


def test_token_hex_real_nunca_e_recusado() -> None:
    for _ in range(10_000):
        valor = secrets.token_hex(32)
        assert validar_segredo(valor) == valor
