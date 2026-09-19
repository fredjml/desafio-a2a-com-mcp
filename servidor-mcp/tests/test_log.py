"""Unit: campos logaveis do request (regex W3C do traceparent; nada de requestState)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.log import campos_do_request, traceparent_valido

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _corpo(**extra: Any) -> bytes:
    params: dict[str, Any] = {"_meta": {"traceparent": TP}}
    params.update(extra)
    return json.dumps(
        {"jsonrpc": "2.0", "id": "abc", "method": "tools/call", "params": params}
    ).encode()


def test_traceparent_valido() -> None:
    assert traceparent_valido(TP) == TP


@pytest.mark.parametrize(
    "valor",
    [
        None,
        123,
        "",
        "lixo",
        TP.upper(),
        TP + "\n",
        TP + "-extra",
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",  # trace-id zerado
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",  # parent-id zerado
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # versao ff proibida
        '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}\n{"forjado":true',
    ],
)
def test_traceparent_invalido_vira_none(valor: object) -> None:
    assert traceparent_valido(valor) is None


def test_campos_basicos() -> None:
    campos = campos_do_request(_corpo(), {"mcp-name": "reservar_sala"})
    assert campos == {
        "method": "tools/call",
        "id": "abc",
        "traceparent": TP,
        "mcp_name": "reservar_sala",
        "client": None,
    }


def test_nunca_loga_request_state_nem_argumentos() -> None:
    corpo = _corpo(
        requestState="v1.SEGREDO", inputResponses={"k": {"action": "accept"}}, arguments={"a": 1}
    )
    campos = campos_do_request(corpo, {})
    assert "SEGREDO" not in json.dumps(campos)
    assert set(campos) == {"method", "id", "traceparent", "mcp_name", "client"}


def test_client_info_e_id_inteiro() -> None:
    corpo = json.dumps(
        {
            "id": 7,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "agente"}}},
        }
    ).encode()
    campos = campos_do_request(corpo, {})
    assert campos["id"] == 7 and campos["client"] == "agente"


@pytest.mark.parametrize("corpo", [b"", b"{nao json", b"[1,2]", b'"texto"', b"\xff\xfe"])
def test_corpo_hostil_nao_quebra(corpo: bytes) -> None:
    campos = campos_do_request(corpo, {})
    assert campos["method"] is None and campos["id"] is None


def test_id_de_tipo_estranho_nao_e_logado() -> None:
    campos = campos_do_request(json.dumps({"id": [1], "method": "x"}).encode(), {})
    assert campos["id"] is None
    campos = campos_do_request(json.dumps({"id": True, "method": "x"}).encode(), {})
    assert campos["id"] is None


def test_texto_longo_e_truncado() -> None:
    campos = campos_do_request(
        json.dumps({"id": "i" * 1000, "method": "m" * 1000}).encode(), {"mcp-name": "n" * 1000}
    )
    assert (
        len(campos["id"]) == 128 and len(campos["method"]) == 128 and len(campos["mcp_name"]) == 128
    )
