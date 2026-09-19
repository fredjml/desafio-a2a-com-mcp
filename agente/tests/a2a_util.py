"""Ajudantes de teste do lado A2A: config, cliente ASGI em processo, host MCP falso, JSON-RPC."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from typing import Any

import httpx
from mcp_types import CallToolResult, ElicitResult, InputRequiredResult, TextContent

from app.a2a import App, criar_app
from app.config import Config
from app.mcp_host import McpHostError

WIRE = "http://127.0.0.1:7300"
DIA = "2026-11-03"


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


def config(**kw: Any) -> Config:
    base: dict[str, Any] = {
        "a2a_port": 7300,
        "mcp_url": "http://127.0.0.1:9/mcp",
        "mcp_timeout_s": 3.0,
        "card_host": "127.0.0.1",
    }
    base.update(kw)
    return Config(**base)


def texto_resultado(texto: str, *, erro: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=texto)], is_error=erro)


def reserva_ok(
    reserva: str = "res-0003",
    sala: str = "sala-porao",
    inicio: str = "2026-11-03T09:00:00-03:00",
    fim: str = "2026-11-03T10:00:00-03:00",
    responsavel: str = "Doc",
) -> CallToolResult:
    dados = {
        "reserva": reserva,
        "reservado": True,
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
        "politica": "2026-11-01",
        "motivo": None,
    }
    return CallToolResult(
        content=[TextContent(text=json.dumps(dados, indent=2))], structured_content=dados
    )


Resposta = CallToolResult | InputRequiredResult | McpHostError


class HostFalso:
    """Substitui o McpHost: registra chamadas e devolve respostas programadas (sem rede)."""

    def __init__(
        self,
        respostas: list[Resposta] | Callable[[dict[str, Any]], Resposta] | None = None,
        versao: str = "2026-11-01",
    ) -> None:
        self.respostas = respostas if respostas is not None else [reserva_ok()]
        self.versao = versao
        self.chamadas: list[dict[str, Any]] = []
        self.leituras_de_politica: list[str | None] = []
        self.invocacoes_da_fachada = 0
        self.geracao = 1  # geracao do Client MCP (ver McpHost.geracao); testes podem alterar
        self.espera: asyncio.Event | None = (
            None  # se definido, bloqueia tools/call ate ser liberado
        )
        self.dentro = asyncio.Event()

    async def versao_da_politica(self, trace_id: str | None = None) -> str:
        self.leituras_de_politica.append(trace_id)
        if isinstance(self.versao, McpHostError):
            raise self.versao
        return self.versao

    async def chamar_ferramenta(
        self,
        nome: str,
        argumentos: dict[str, Any],
        *,
        trace_id: str | None = None,
        input_responses: dict[str, ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        chamada = {
            "nome": nome,
            "argumentos": argumentos,
            "trace_id": trace_id,
            "input_responses": input_responses,
            "request_state": request_state,
        }
        self.chamadas.append(chamada)
        self.dentro.set()
        if self.espera is not None:
            await self.espera.wait()
        r = self.respostas(chamada) if callable(self.respostas) else self.respostas.pop(0)
        if isinstance(r, McpHostError):
            raise r
        return r

    async def aclose(self) -> None:
        return None


def app_com(host: Any, **cfg: Any) -> App:
    return criar_app(config(**cfg), host)


def cliente_asgi(app: App) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.asgi()), base_url=WIRE, timeout=20.0
    )


def mensagem_usuario(texto: str, task_id: str | None = None, **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "messageId": f"msg-{secrets.token_hex(6)}",
        "role": "ROLE_USER",
        "parts": [{"text": texto}],
    }
    if task_id:
        msg["taskId"] = task_id
    msg.update(extra)
    return msg


async def rpc(
    c: httpx.AsyncClient,
    metodo: str,
    params: dict[str, Any],
    *,
    id_: Any = None,
    cabecalhos: dict[str, str] | None = None,
) -> dict[str, Any]:
    corpo = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(6) if id_ is None else id_,
        "method": metodo,
        "params": params,
    }
    hdr = {"Content-Type": "application/json"}  # como o validador: SEM A2A-Version
    hdr.update(cabecalhos or {})
    r = await c.post("/a2a", content=json.dumps(corpo), headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    resposta: dict[str, Any] = r.json()
    return resposta


async def enviar(
    c: httpx.AsyncClient,
    texto: str,
    task_id: str | None = None,
    *,
    cabecalhos: dict[str, str] | None = None,
    id_: Any = None,
) -> dict[str, Any]:
    return await rpc(
        c,
        "SendMessage",
        {"message": mensagem_usuario(texto, task_id)},
        cabecalhos=cabecalhos,
        id_=id_,
    )


def tarefa(resposta: dict[str, Any]) -> dict[str, Any]:
    resultado = resposta.get("result") or {}
    t: dict[str, Any] = resultado.get("task") or resultado
    return t


def estado(resposta: dict[str, Any]) -> str:
    return str((tarefa(resposta).get("status") or {}).get("state", ""))


def mensagem_de(resposta: dict[str, Any]) -> str:
    status = tarefa(resposta).get("status") or {}
    partes = (status.get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in partes)


def artifact_json(resposta: dict[str, Any]) -> dict[str, Any]:
    arts = tarefa(resposta).get("artifacts") or []
    assert arts, "sem artifact"
    dados: dict[str, Any] = json.loads(" ".join(p.get("text", "") for p in arts[0]["parts"]))
    return dados


def pedido(sala: str, ini: str, fim: str, resp: str = "Doc") -> str:
    return f"reservar sala={sala} inicio={h(ini)} fim={h(fim)} responsavel={resp}"
