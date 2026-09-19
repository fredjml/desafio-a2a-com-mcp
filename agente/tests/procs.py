"""Ajudantes de teste: sobe o servidor MCP REAL e o agente REAL em subprocess, e um proxy gravador.

- O servidor real roda com o python de `servidor-mcp/.venv` (o agente NAO importa codigo do servidor:
  R-ARQ-01; aqui so ha subprocess + HTTP).
- O segredo do requestState e gerado em memoria (secrets.token_hex) a cada execucao e NUNCA e impresso,
  gravado ou logado.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

AGENTE_DIR = Path(__file__).resolve().parents[1]
FORK_DIR = AGENTE_DIR.parent
SERVIDOR_DIR = FORK_DIR / "servidor-mcp"
_PY_SERVIDOR_WIN = SERVIDOR_DIR / ".venv" / "Scripts" / "python.exe"
_PY_SERVIDOR_POSIX = SERVIDOR_DIR / ".venv" / "bin" / "python"
_VARS_DO_SERVIDOR = ("REQUEST_STATE_SECRET", "REQUEST_STATE_TTL_S", "MCP_PORT", "DADOS_DIR")
_VARS_DO_AGENTE = ("A2A_PORT", "MCP_URL", "MCP_TIMEOUT_S", "A2A_CARD_HOST")


def python_do_servidor() -> Path | None:
    for candidato in (_PY_SERVIDOR_WIN, _PY_SERVIDOR_POSIX):
        if candidato.exists():
            return candidato
    return None


def porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Processo:
    """Subprocess com stderr/stdout drenados por threads (sem deadlock de pipe)."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.stderr: list[str] = []
        self.stdout: list[str] = []
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
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

    @staticmethod
    def _drenar(fluxo: Any, destino: list[str]) -> None:
        for linha in fluxo:
            destino.append(linha.rstrip("\n"))

    def esperar_porta(self, porta: int, prazo: float = 40.0) -> None:
        fim = time.monotonic() + prazo
        while time.monotonic() < fim:
            if self.proc.poll() is not None:
                raise RuntimeError(f"processo saiu no boot (codigo {self.proc.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", porta), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("processo nao abriu a porta a tempo")

    def parar(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        for t in self._threads:
            t.join(timeout=2)

    def linhas_json(self, **filtro: Any) -> list[dict[str, Any]]:
        time.sleep(0.2)  # a thread leitora e assincrona
        achadas: list[dict[str, Any]] = []
        for linha in list(self.stderr):
            try:
                obj = json.loads(linha)
            except ValueError:
                continue
            if isinstance(obj, dict) and all(obj.get(k) == v for k, v in filtro.items()):
                achadas.append(obj)
        return achadas


class ServidorMcpReal(Processo):
    """`python -m app` do servidor-mcp (venv do servidor), segredo novo em memoria."""

    def __init__(
        self, *, segredo: str, porta: int | None = None, env_extra: dict[str, str] | None = None
    ):
        py = python_do_servidor()
        if py is None:
            raise RuntimeError("servidor-mcp/.venv nao encontrado")
        self.porta = porta or porta_livre()
        env = {k: v for k, v in os.environ.items() if k not in _VARS_DO_SERVIDOR}
        env["MCP_PORT"] = str(self.porta)
        env["REQUEST_STATE_SECRET"] = segredo
        env["PYTHONUNBUFFERED"] = "1"
        env.update(env_extra or {})
        super().__init__([str(py), "-m", "app"], SERVIDOR_DIR, env)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}/mcp"

    def requests(self, **filtro: Any) -> list[dict[str, Any]]:
        """Linhas `evento=request` do stderr do servidor (1 por POST)."""
        return self.linhas_json(evento="request", **filtro)


class AgenteReal(Processo):
    """`python -m app` do agente (venv atual)."""

    def __init__(
        self, *, mcp_url: str, porta: int | None = None, env_extra: dict[str, str] | None = None
    ):
        self.porta = porta or porta_livre()
        env = {k: v for k, v in os.environ.items() if k not in _VARS_DO_AGENTE}
        env["A2A_PORT"] = str(self.porta)
        env["MCP_URL"] = mcp_url
        env["PYTHONUNBUFFERED"] = "1"
        env.update(env_extra or {})
        super().__init__([sys.executable, "-m", "app"], AGENTE_DIR, env)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    @property
    def rpc_url(self) -> str:
        return f"{self.base}/a2a"


class ProxyGravador:
    """Proxy HTTP local que REGISTRA o que o cliente MCP manda (a "verdade do fio") e repassa ao alvo."""

    def __init__(self, alvo_url: str) -> None:
        self.alvo_url = alvo_url
        self.requests: list[dict[str, Any]] = []
        self.respostas: list[dict[str, Any]] = []
        self.metodos_http: list[str] = []
        proxy = self
        cliente = httpx.Client(timeout=20.0)
        self._cliente = cliente

        class Handler(BaseHTTPRequestHandler):
            def _corpo(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self) -> None:
                corpo = self._corpo()
                cab = {k.lower(): v for k, v in self.headers.items()}
                try:
                    obj = json.loads(corpo)
                except ValueError:
                    obj = None
                proxy.metodos_http.append("POST")
                proxy.requests.append({"headers": cab, "json": obj})
                enviar = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
                }
                resp = cliente.post(proxy.alvo_url, content=corpo, headers=enviar)
                try:
                    proxy.respostas.append({"status": resp.status_code, "json": resp.json()})
                except ValueError:
                    proxy.respostas.append({"status": resp.status_code, "json": None})
                self.send_response(resp.status_code)
                self.send_header(
                    "Content-Type", resp.headers.get("content-type", "application/json")
                )
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)

            def do_GET(self) -> None:
                proxy.metodos_http.append("GET")
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self) -> None:
                proxy.metodos_http.append("DELETE")
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.porta = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}/mcp"

    def parar(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._cliente.close()

    def metodos(self) -> list[str]:
        return [str((r["json"] or {}).get("method")) for r in self.requests]

    def meta(self, i: int) -> dict[str, Any]:
        params = (self.requests[i]["json"] or {}).get("params") or {}
        meta = params.get("_meta")
        return meta if isinstance(meta, dict) else {}
