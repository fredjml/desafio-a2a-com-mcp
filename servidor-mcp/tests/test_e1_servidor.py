"""Integracao E1 (processo REAL via subprocess+httpx): envelope _meta, ids, Accept, log, bind, listar_salas."""

from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import DADOS_PADRAO

from .servidor_proc import (
    K_CAPS,
    K_VERSAO,
    TRACEPARENT,
    Servidor,
    ipv6_disponivel,
    montar_meta,
    texto_de,
)

SALAS_JSON = json.loads((DADOS_PADRAO / "salas.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- tools/list e capabilities
def test_tools_list_traz_listar_salas_no_formato_do_wire_01(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/list")
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["id"] == 1
    res = corpo["result"]
    assert res["resultType"] == "complete"
    assert res["ttlMs"] == 0 and res["cacheScope"] == "private"
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "central-de-salas",
        "version": "1.0.0",
    }
    tool = {t["name"]: t for t in res["tools"]}["listar_salas"]
    assert tool["description"] == "Lista todas as salas com capacidade e recursos."
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {},
        "title": "listar_salasArguments",
    }
    saida = tool["outputSchema"]
    assert saida["type"] == "object" and saida["title"] == "ListaDeSalas"
    assert saida["required"] == ["salas"]
    assert set(saida["$defs"]["SalaOut"]["required"]) == {"id", "nome", "capacidade", "recursos"}


def test_capabilities_declaram_tools_e_resources(servidor: Servidor) -> None:
    res = servidor.rpc("server/discover").json()["result"]
    assert "tools" in res["capabilities"] and "resources" in res["capabilities"]
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "central-de-salas"
    assert res["supportedVersions"] == ["2026-07-28"]


# ---------------------------------------------------------------- listar_salas
def test_listar_salas_structured_igual_ao_json_do_bloco_de_texto(servidor: Servidor) -> None:
    res = servidor.tool("listar_salas")
    assert res["resultType"] == "complete" and res["isError"] is False
    estruturado = res["structuredContent"]
    assert json.loads(texto_de(res)) == estruturado  # check 03 do validador
    assert len(res["content"]) == 1 and res["content"][0]["type"] == "text"
    assert estruturado == {"salas": SALAS_JSON}  # mesmos dados do arquivo, mesma ordem
    assert texto_de(res) == json.dumps(estruturado, indent=2)


# ---------------------------------------------------------------- envelope _meta obrigatorio
@pytest.mark.parametrize(
    ("omitir", "chave_citada"),
    [(K_VERSAO, "protocolVersion"), (K_CAPS, "clientCapabilities")],
    ids=["sem-protocolVersion", "sem-clientCapabilities"],
)
@pytest.mark.parametrize("metodo", ["tools/list", "tools/call"])
def test_meta_incompleto_gera_32602_http_400(
    servidor: Servidor, omitir: str, chave_citada: str, metodo: str
) -> None:
    params = {"name": "listar_salas", "arguments": {}} if metodo == "tools/call" else {}
    resp = servidor.rpc(metodo, params, meta=montar_meta(omitir=omitir))
    assert resp.status_code == 400
    erro = resp.json()["error"]
    assert erro["code"] == -32602
    assert chave_citada in erro["message"]


def test_meta_vazio_e_ausente_geram_32602_http_400(servidor: Servidor) -> None:
    assert servidor.rpc("tools/list", meta={}).status_code == 400
    cru = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
    }
    resp = servidor.cliente.post(servidor.url, json=cru, headers=hdrs)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32602
    sem_params = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    resp = servidor.cliente.post(servidor.url, json=sem_params, headers=hdrs)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32602


def test_sem_inferencia_de_request_anterior(servidor: Servidor) -> None:
    """Um request completo nao 'ensina' o servidor: o seguinte, sem _meta, continua rejeitado."""
    assert servidor.rpc("tools/list").status_code == 200
    resp = servidor.rpc("tools/list", meta=montar_meta(omitir=K_CAPS))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32602


# ---------------------------------------------------------------- ids JSON-RPC (T-26) e Accept (T-27, T-39)
@pytest.mark.parametrize("ident", [7, 0, 1234567890, "abc", "42", "9f8e7d6c5b4a", "x" * 300])
def test_ids_string_e_inteiro_sao_ecoados(servidor: Servidor, ident: Any) -> None:
    resp = servidor.rpc("tools/list", id_=ident)
    assert resp.status_code == 200
    eco = resp.json()["id"]
    assert eco == ident and type(eco) is type(ident)


def test_accept_com_sse_devolve_json(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/list", cabecalhos={"Accept": "application/json, text/event-stream"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    resp = servidor.rpc("tools/list", cabecalhos={"Accept": "text/event-stream, application/json"})
    assert resp.headers["content-type"].startswith("application/json")


def test_accept_json_sozinho_e_200(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/list", cabecalhos={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


def test_nao_ha_sessao_no_transporte(servidor: Servidor) -> None:
    resp = servidor.rpc("tools/list")
    assert "mcp-session-id" not in {k.lower() for k in resp.headers}


# ---------------------------------------------------------------- entrada hostil (T-21)
def test_json_invalido_devolve_erro_jsonrpc_bem_formado(servidor: Servidor) -> None:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    resp = servidor.cliente.post(servidor.url, content="{nao json", headers=hdrs)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700
    assert "Traceback" not in resp.text


def test_metodo_desconhecido_devolve_32601(servidor: Servidor) -> None:
    resp = servidor.rpc("metodo/inexistente", mcp_method="metodo/inexistente")
    assert resp.status_code in (400, 404)
    assert resp.json()["error"]["code"] == -32601


def test_meta_gigante_nao_derruba_o_servidor(servidor: Servidor) -> None:
    meta = montar_meta()
    meta["lixo"] = "x" * 200_000
    resp = servidor.rpc("tools/list", meta=meta)
    assert resp.status_code < 500
    assert servidor.rpc("tools/list").status_code == 200  # segue de pe


def test_metodo_http_get_nao_e_500(servidor: Servidor) -> None:
    resp = servidor.cliente.get(servidor.url, headers={"Accept": "application/json"})
    assert resp.status_code < 500


# ---------------------------------------------------------------- log em stderr (AC-08, T-19 parte servidor)
def test_stderr_registra_metodo_id_traceparent_nome_e_cliente(servidor: Servidor) -> None:
    ident = "log-8f3a1c"
    servidor.tool("listar_salas", meta=montar_meta(traceparent=TRACEPARENT), id_=ident)
    linhas = servidor.linhas_de_log(id=ident)
    assert len(linhas) == 1  # exatamente uma linha por request (sem tools/list fantasma)
    linha = linhas[0]
    assert linha["method"] == "tools/call"
    assert linha["traceparent"] == TRACEPARENT
    assert linha["mcp_name"] == "listar_salas"
    assert linha["client"] == "teste-pytest"
    assert linha["status"] == 200 and linha["proc"] == "servidor-mcp"


def test_stderr_registra_id_inteiro_e_request_rejeitado(servidor: Servidor) -> None:
    servidor.rpc("tools/list", id_=987001, meta=montar_meta(omitir=K_VERSAO))
    linha = servidor.linhas_de_log(id=987001)[0]
    assert linha["method"] == "tools/list" and linha["status"] == 400


def test_traceparent_invalido_nao_e_logado_e_nao_forja_linhas(servidor: Servidor) -> None:
    forjado = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}\n{"evento":"forjado"'
    servidor.tool("listar_salas", meta=montar_meta(traceparent=forjado), id_="log-forja")
    linha = servidor.linhas_de_log(id="log-forja")[0]
    assert linha["traceparent"] is None
    assert not any('"forjado"' in ln and ln.startswith('{"evento"') for ln in servidor.stderr)


def test_tools_list_antes_de_tools_call_na_ordem_do_log(servidor: Servidor) -> None:
    servidor.rpc("tools/list", id_="ordem-1")
    servidor.tool("listar_salas", id_="ordem-2")
    metodos = [
        ln["method"] for ln in servidor.linhas_de_log() if ln["id"] in ("ordem-1", "ordem-2")
    ]
    assert metodos == ["tools/list", "tools/call"]


def test_stderr_e_stdout_nao_vazam_segredo_nem_request_state(
    servidor: Servidor, segredo: str
) -> None:
    servidor.rpc(
        "tools/call",
        {"name": "listar_salas", "arguments": {}, "requestState": "v1.ESTADO-OPACO-QUALQUER"},
        id_="log-rs",
    )
    servidor.linhas_de_log()  # sincroniza a thread leitora
    todo = "\n".join(servidor.stderr + servidor.stdout)
    assert segredo not in todo
    assert "ESTADO-OPACO-QUALQUER" not in todo
    assert servidor.stdout == []  # nada em stdout: log so em stderr


def test_linha_de_boot_registra_comando_e_enderecos(servidor: Servidor) -> None:
    boot = json.loads(next(ln for ln in servidor.stderr if '"evento": "boot"' in ln))
    assert boot["comando"] == "python -m app"
    assert boot["porta"] == servidor.porta and boot["endpoint"] == "/mcp"
    assert "0.0.0.0" not in boot["escutando"] and "127.0.0.1" in boot["escutando"]


# ---------------------------------------------------------------- bind (T-20, T-39)
def test_responde_em_127_0_0_1(servidor: Servidor) -> None:
    assert (
        servidor.rpc("tools/list", url=f"http://127.0.0.1:{servidor.porta}/mcp").status_code == 200
    )


@pytest.mark.skipif(not ipv6_disponivel(), reason="este SO nao tem IPv6 em ::1")
def test_responde_em_ipv6_loopback(servidor: Servidor) -> None:
    assert servidor.rpc("tools/list", url=f"http://[::1]:{servidor.porta}/mcp").status_code == 200


def test_localhost_sem_lentidao_de_fallback(servidor: Servidor) -> None:
    """Sem o socket ::1, `localhost` custa ~2 s/request no urllib (achado S3-l)."""
    inicio = time.perf_counter()
    for _ in range(5):
        assert (
            servidor.rpc("tools/list", url=f"http://localhost:{servidor.porta}/mcp").status_code
            == 200
        )
    assert (time.perf_counter() - inicio) / 5 < 0.5


def test_urllib_como_o_validador_em_localhost_e_rapido(servidor: Servidor) -> None:
    import urllib.request

    req = urllib.request.Request(
        f"http://localhost:{servidor.porta}/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "u1",
                "method": "tools/list",
                "params": {"_meta": montar_meta()},
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        method="POST",
    )
    inicio = time.perf_counter()
    with urllib.request.urlopen(req, timeout=15) as r:
        assert r.status == 200
    assert time.perf_counter() - inicio < 1.0


def test_nao_escuta_em_todas_as_interfaces(servidor: Servidor) -> None:
    """Se a maquina tem um IP nao-loopback, a conexao nele deve ser recusada (nunca 0.0.0.0)."""
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        pytest.skip("nao foi possivel descobrir o IP local")
    if ip.startswith("127."):
        pytest.skip("a maquina so tem loopback")
    with pytest.raises(OSError):
        socket.create_connection((ip, servidor.porta), timeout=2).close()


def test_codigo_nao_contem_bind_em_todas_as_interfaces() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    for arq in app_dir.glob("*.py"):
        texto = arq.read_text(encoding="utf-8")
        assert "0.0.0.0" not in texto, arq.name


# ---------------------------------------------------------------- dados via Path(__file__), nao cwd
def test_dados_nao_dependem_do_cwd(segredo: str, tmp_path: Path) -> None:
    # cwd diferente: `python -m app` precisa achar o pacote; PYTHONPATH aponta so para o codigo.
    srv = Servidor(
        segredo=segredo,
        cwd=tmp_path,
        env_extra={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        srv.esperar_pronto()
        assert len(srv.tool("listar_salas")["structuredContent"]["salas"]) == 5
    finally:
        srv.parar()


def test_dados_dir_pode_ser_sobrescrito(segredo: str, tmp_path: Path) -> None:
    pasta = tmp_path / "dados-alternativos"
    shutil.copytree(DADOS_PADRAO, pasta)
    (pasta / "salas.json").write_text(
        json.dumps([{"id": "sala-unica", "nome": "Unica", "capacidade": 2, "recursos": []}]),
        encoding="utf-8",
    )
    srv = Servidor(segredo=segredo, dados_dir=pasta)
    try:
        srv.esperar_pronto()
        salas = srv.tool("listar_salas")["structuredContent"]["salas"]
        assert [s["id"] for s in salas] == ["sala-unica"]
    finally:
        srv.parar()


# ---------------------------------------------------------------- fail-fast do segredo (T-10, T-37)
def _falha_no_boot(
    segredo: str | None, extra: dict[str, str] | None = None
) -> tuple[int, str, Servidor]:
    srv = Servidor(segredo=segredo, env_extra=extra)
    try:
        codigo = srv.esperar_saida()
    finally:
        srv.parar()
    return codigo, "\n".join(srv.stderr), srv


@pytest.mark.parametrize(
    "caso",
    ["ausente", "vazio", "placeholder", "16-bytes", "31-bytes", "nao-hex", "trivial"],
)
def test_boot_falha_com_segredo_invalido_e_nao_ecoa_o_valor(caso: str) -> None:
    import secrets

    valores: dict[str, str | None] = {
        "ausente": None,
        "vazio": "",
        "placeholder": "<cole-aqui-64-hex-gerados-localmente>",
        "16-bytes": secrets.token_hex(16),
        "31-bytes": secrets.token_hex(31),
        "nao-hex": "g" * 64,
        "trivial": "00" * 32,
    }
    valor = valores[caso]
    codigo, stderr, srv = _falha_no_boot(valor)
    assert codigo != 0
    assert "REQUEST_STATE_SECRET" in stderr
    if valor:
        assert valor not in stderr
    with pytest.raises(OSError):
        socket.create_connection(
            ("127.0.0.1", srv.porta), timeout=1
        ).close()  # nunca chegou a escutar


def test_boot_falha_com_porta_invalida(segredo: str) -> None:
    codigo, stderr, _ = _falha_no_boot(segredo, {"MCP_PORT": "abc"})
    assert codigo != 0 and "MCP_PORT" in stderr


def test_boot_falha_com_porta_ocupada(segredo: str, servidor: Servidor) -> None:
    srv = Servidor(segredo=segredo, porta=servidor.porta)
    try:
        assert srv.esperar_saida() != 0
    finally:
        srv.parar()
    assert servidor.rpc("tools/list").status_code == 200  # o servidor original nao foi afetado
