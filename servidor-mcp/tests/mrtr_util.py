"""Ajudantes dos testes MRTR (E4): pedir conflito, extrair chave/estado, retomar, adulterar."""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

from app.config import DADOS_PADRAO

from .servidor_proc import Servidor

DIA = "2026-11-03"
WIRE = DADOS_PADRAO.parent / "exemplos" / "wire"
_IDS = itertools.count(1000)
MSG_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
MSG_SEM_ALT = "Sem alternativas disponiveis no intervalo"
MSG_ESTADO = "Invalid or expired requestState"
REQUER_FORM: dict[str, Any] = {"elicitation": {"form": {}}}
RECUSADO: dict[str, Any] = {
    "reserva": None,
    "reservado": False,
    "sala": None,
    "inicio": None,
    "fim": None,
    "responsavel": None,
    "politica": None,
    "motivo": "recusado",
}


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


def wire_json(nome: str) -> dict[str, Any]:
    dado: dict[str, Any] = json.loads((WIRE / nome).read_text(encoding="utf-8"))
    return dado


def args(sala: str, ini: str, fim: str, responsavel: str = "Marty") -> dict[str, Any]:
    return {"sala": sala, "inicio": h(ini), "fim": h(fim), "responsavel": responsavel}


def chamar(
    srv: Servidor,
    argumentos: dict[str, Any],
    *,
    respostas: dict[str, Any] | None = None,
    estado: Any = ...,
    **kw: Any,
) -> httpx.Response:
    """tools/call reservar_sala. `estado=...` (Ellipsis) = campo ausente; None = `null` no fio."""
    params: dict[str, Any] = {"name": "reservar_sala", "arguments": argumentos}
    if respostas is not None:
        params["inputResponses"] = respostas
    if estado is not ...:
        params["requestState"] = estado
    return srv.rpc("tools/call", params, **kw)


def pedir(srv: Servidor, argumentos: dict[str, Any], **kw: Any) -> tuple[str, str, list[str]]:
    """Faz o pedido, exige input_required e devolve (chave, requestState, alternativas)."""
    resp = chamar(srv, argumentos, **kw)
    assert resp.status_code == 200, resp.text
    res = resp.json()["result"]
    assert res["resultType"] == "input_required", res
    (chave,) = res["inputRequests"]
    campo = res["inputRequests"][chave]["params"]["requestedSchema"]["properties"]["sala"]
    alternativas = campo["enum"] if "enum" in campo else [campo["const"]]
    return chave, res["requestState"], alternativas


def aceitar(sala: str) -> dict[str, Any]:
    return {"action": "accept", "content": {"sala": sala}}


def retomar(
    srv: Servidor,
    argumentos: dict[str, Any],
    chave: str,
    resposta: dict[str, Any],
    estado: Any,
    **kw: Any,
) -> httpx.Response:
    kw.setdefault("id_", f"retry-{next(_IDS)}")  # id JSON-RPC NOVO a cada retry
    return chamar(srv, argumentos, respostas={chave: resposta}, estado=estado, **kw)


def trocar_um_char(estado: str, posicao: int) -> str:
    """Troca 1 caractere (posicao negativa conta do fim) por outro DIFERENTE do original."""
    i = posicao % len(estado)
    novo = "A" if estado[i] != "A" else "B"
    return estado[:i] + novo + estado[i + 1 :]


def ocupar(srv: Servidor, sala: str, ini: str, fim: str, quem: str = "Ocupante") -> None:
    """Cria uma reserva LIVRE (sem conflito) direto pela tool."""
    resp = chamar(srv, args(sala, ini, fim, quem))
    assert resp.status_code == 200 and resp.json()["result"]["isError"] is False, resp.text


def consultar(srv: Servidor, sala: str, ini: str, fim: str) -> dict[str, Any]:
    res = srv.tool("consultar_disponibilidade", {"sala": sala, "inicio": h(ini), "fim": h(fim)})
    estruturado: dict[str, Any] = res["structuredContent"]
    return estruturado


def reservas_em(srv: Servidor, sala: str, ini: str, fim: str) -> list[str]:
    return [c["id"] for c in consultar(srv, sala, ini, fim)["conflitos"]]


def erro_de(resp: httpx.Response) -> dict[str, Any]:
    corpo: dict[str, Any] = resp.json()
    assert "error" in corpo, corpo
    erro: dict[str, Any] = corpo["error"]
    return erro


def assert_estado_invalido(resp: httpx.Response) -> None:
    """Protocolo: -32602 + HTTP 400 + mensagem unica; nunca 500 nem 200."""
    assert resp.status_code == 400, (resp.status_code, resp.text[:300])
    erro = erro_de(resp)
    assert erro["code"] == -32602 and erro["message"] == MSG_ESTADO, erro
