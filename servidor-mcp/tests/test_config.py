"""Unit: configuracao e fail-fast do segredo (T-10, T-37). Nenhum valor de segredo e impresso."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.config import (
    DADOS_PADRAO,
    PLACEHOLDER_SEGREDO,
    ConfigError,
    carregar_config,
    validar_segredo,
)


def test_segredo_valido_64_hex_e_aceito() -> None:
    valor = secrets.token_hex(32)
    assert validar_segredo(valor) == valor


def test_segredo_aceita_espacos_nas_pontas_e_hex_maiusculo() -> None:
    valor = secrets.token_hex(32)
    assert validar_segredo(f"  {valor.upper()}\n") == valor.upper()


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "   ",
        PLACEHOLDER_SEGREDO,
        secrets.token_hex(16),  # 32 chars = 16 bytes: o SDK aceitaria (mede caracteres), nos nao
        secrets.token_hex(32)[:-2],  # 31 bytes
        secrets.token_hex(32)[:-1],  # comprimento impar
        "z" * 64,  # nao e hexadecimal
        "ab" * 32,  # 32 bytes, mas so 2 valores distintos: sem aleatoriedade
    ],
    ids=[
        "ausente",
        "vazio",
        "so-espacos",
        "placeholder",
        "16-bytes",
        "31-bytes",
        "impar",
        "nao-hex",
        "trivial",
    ],
)
def test_segredo_invalido_e_recusado_sem_ecoar_o_valor(valor: str | None) -> None:
    with pytest.raises(ConfigError) as exc:
        validar_segredo(valor)
    if valor and valor.strip() and valor != PLACEHOLDER_SEGREDO:
        assert valor.strip() not in str(exc.value)


def test_config_padrao_usa_porta_7301_e_dados_do_fork() -> None:
    cfg = carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32)})
    assert cfg.porta == 7301
    assert cfg.dados_dir == DADOS_PADRAO
    assert (DADOS_PADRAO / "salas.json").is_file()


def test_config_nao_vaza_segredo_no_repr() -> None:
    valor = secrets.token_hex(32)
    assert valor not in repr(carregar_config({"REQUEST_STATE_SECRET": valor}))


@pytest.mark.parametrize("porta", ["0", "65536", "abc", "-1", "7301.5"])
def test_porta_invalida(porta: str) -> None:
    with pytest.raises(ConfigError):
        carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32), "MCP_PORT": porta})


def test_porta_via_ambiente() -> None:
    cfg = carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32), "MCP_PORT": "18301"})
    assert cfg.porta == 18301


def test_dados_dir_invalido(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32), "DADOS_DIR": str(tmp_path)})


def test_ttl_padrao_do_request_state_e_600_s() -> None:
    assert (
        carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32)}).request_state_ttl_s
        == 600.0
    )


@pytest.mark.parametrize(
    ("valor", "esperado"), [("2", 2.0), (" 30 ", 30.0), ("1.5", 1.5), ("1800", 1800.0)]
)
def test_ttl_via_ambiente_para_testes_de_expiracao(valor: str, esperado: float) -> None:
    cfg = carregar_config(
        {"REQUEST_STATE_SECRET": secrets.token_hex(32), "REQUEST_STATE_TTL_S": valor}
    )
    assert cfg.request_state_ttl_s == esperado


@pytest.mark.parametrize("valor", ["0", "0.5", "-1", "1801", "abc", "nan", "inf", "1e9"])
def test_ttl_invalido_falha_no_boot(valor: str) -> None:
    with pytest.raises(ConfigError, match="REQUEST_STATE_TTL_S"):
        carregar_config(
            {"REQUEST_STATE_SECRET": secrets.token_hex(32), "REQUEST_STATE_TTL_S": valor}
        )
