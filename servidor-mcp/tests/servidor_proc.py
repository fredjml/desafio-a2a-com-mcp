"""Ajudantes de teste: sobe o servidor REAL (`python -m app`) em subprocess e fala HTTP com ele.

O segredo do requestState e gerado em memoria a cada execucao (secrets.token_hex) e nunca e
impresso, gravado ou logado pelos testes.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

SERVIDOR_DIR = Path(__file__).resolve().parents[1]
PROTOCOLO = "2026-07-28"
CAPS_ELICITATION: dict[str, Any] = {"elicitation": {"form": {}}}
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
K_VERSAO = "io.modelcontextprotocol/protocolVersion"
K_CAPS = "io.modelcontextprotocol/clientCapabilities"
K_INFO = "io.modelcontextprotocol/clientInfo"

_VARS_DO_SERVIDOR = ("REQUEST_STATE_SECRET", "REQUEST_STATE_TTL_S", "MCP_PORT", "DADOS_DIR")


def ipv6_disponivel() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def montar_meta(
    *,
    traceparent: str | None = None,
    caps: dict[str, Any] | None = None,
    omitir: str | None = None,
    cliente: str | None = "teste-pytest",
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        K_VERSAO: PROTOCOLO,
        K_CAPS: CAPS_ELICITATION if caps is None else caps,
    }
    if cliente:
        meta[K_INFO] = {"name": cliente, "version": "1.0.0"}
    if traceparent:
        meta["traceparent"] = traceparent
    if omitir:
        meta.pop(omitir, None)
    return meta


class Servidor:
    """Processo `python -m app` com stderr/stdout capturados por threads (sem deadlock de pipe)."""

    def __init__(
        self,
        *,
        segredo: str | None,
        porta: int | None = None,
        dados_dir: Path | None = None,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.porta = porta or porta_livre()
        self.stderr: list[str] = []
        self.stdout: list[str] = []
        env = {k: v for k, v in os.environ.items() if k not in _VARS_DO_SERVIDOR}
        env["MCP_PORT"] = str(self.porta)
        env["PYTHONUNBUFFERED"] = "1"
        if segredo is not None:
            env["REQUEST_STATE_SECRET"] = segredo
        if dados_dir is not None:
            env["DADOS_DIR"] = str(dados_dir)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=str(cwd or SERVIDOR_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._threads = [
            threading.Thread(
                target=self._drenar, args=(self.proc.stderr, self.stderr), daemon=True
            ),
            threading.Thread(
                target=self._drenar, args=(self.proc.stdout, self.stdout), daemon=True
            ),
        ]
        for t in self._threads:
            t.start()
        self.cliente = httpx.Client(timeout=15.0)

    @staticmethod
    def _drenar(fluxo: Any, destino: list[str]) -> None:
        for linha in fluxo:
            destino.append(linha.rstrip("\n"))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}/mcp"

    def esperar_pronto(self, prazo: float = 30.0) -> None:
        fim = time.monotonic() + prazo
        while time.monotonic() < fim:
            if self.proc.poll() is not None:
                raise RuntimeError(f"servidor saiu no boot (codigo {self.proc.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", self.porta), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("servidor nao abriu a porta a tempo")

    def esperar_saida(self, prazo: float = 20.0) -> int:
        codigo = self.proc.wait(timeout=prazo)
        for t in self._threads:
            t.join(timeout=2)
        return codigo

    def parar(self) -> None:
        self.cliente.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        for t in self._threads:
            t.join(timeout=2)

    # ---- HTTP/JSON-RPC ----
    def rpc(
        self,
        metodo: str,
        params: dict[str, Any] | None = None,
        *,
        id_: Any = 1,
        meta: dict[str, Any] | None = None,
        nome: str | None = None,
        mcp_method: str | None = None,
        cabecalhos: dict[str, str] | None = None,
        url: str | None = None,
    ) -> httpx.Response:
        corpo_params = dict(params or {})
        corpo_params["_meta"] = montar_meta() if meta is None else meta
        if nome is None:
            if metodo == "tools/call":
                nome = str(corpo_params.get("name"))
            elif metodo == "resources/read":
                nome = str(corpo_params.get("uri"))
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": mcp_method or metodo,
        }
        if nome:
            hdrs["Mcp-Name"] = nome
        hdrs.update(cabecalhos or {})
        corpo = {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": corpo_params}
        return self.cliente.post(url or self.url, content=json.dumps(corpo), headers=hdrs)

    def tool(
        self,
        nome: str,
        argumentos: dict[str, Any] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        """tools/call e devolve `result` (levanta AssertionError se vier `error`)."""
        resp = self.rpc("tools/call", {"name": nome, "arguments": argumentos or {}}, **kw)
        corpo: dict[str, Any] = resp.json()
        assert "error" not in corpo, corpo
        assert resp.status_code == 200, (resp.status_code, corpo)
        resultado: dict[str, Any] = corpo["result"]
        return resultado

    def linhas_de_log(self, **filtro: Any) -> list[dict[str, Any]]:
        """Linhas JSON de stderr (evento=request) que casam com o filtro."""
        # pequena espera: o log e emitido no inicio da resposta, mas a thread leitora e assincrona
        time.sleep(0.15)
        achadas = []
        for linha in list(self.stderr):
            try:
                obj = json.loads(linha)
            except ValueError:
                continue
            if obj.get("evento") == "request" and all(obj.get(k) == v for k, v in filtro.items()):
                achadas.append(obj)
        return achadas


def texto_de(resultado: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))
