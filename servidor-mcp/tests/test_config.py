"""Unit: configuracao e fail-fast do segredo (T-10, T-37). Nenhum valor de segredo e impresso."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.config import (
    DADOS_PADRAO,
    PLACEHOLDER_SEGREDO,
    Config,
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


def _cfg(**extra: str) -> Config:
    return carregar_config({"REQUEST_STATE_SECRET": secrets.token_hex(32), **extra})


@pytest.mark.parametrize(
    ("valor", "esperado"), [("300", 300.0), (" 600 ", 600.0), ("300.5", 300.5), ("1800", 1800.0)]
)
def test_ttl_aceita_so_a_faixa_do_enunciado_300_a_1800_s(valor: str, esperado: float) -> None:
    cfg = _cfg(REQUEST_STATE_TTL_S=valor)
    assert cfg.request_state_ttl_s == esperado and cfg.ttl_de_teste is False


@pytest.mark.parametrize(
    "valor", ["299", "299.9", "1801", "0", "-1", "1", "2", "abc", "nan", "inf", "-inf", "1e9", ""]
)
def test_ttl_fora_da_faixa_falha_no_boot(valor: str) -> None:
    if valor == "":  # vazio = nao definido: cai no padrao, nao e erro
        assert _cfg(REQUEST_STATE_TTL_S=valor).request_state_ttl_s == 600.0
        return
    with pytest.raises(ConfigError, match="REQUEST_STATE_TTL_S"):
        _cfg(REQUEST_STATE_TTL_S=valor)


@pytest.mark.parametrize(
    ("valor", "esperado"), [("2", 2.0), (" 30 ", 30.0), ("1", 1.0), ("1.5", 1.5), ("1800", 1800.0)]
)
def test_ttl_de_teste_aceita_1_a_1800_s_e_e_sinalizado(valor: str, esperado: float) -> None:
    cfg = _cfg(REQUEST_STATE_TTL_S_SOMENTE_TESTE=valor)
    assert cfg.request_state_ttl_s == esperado and cfg.ttl_de_teste is True


@pytest.mark.parametrize("valor", ["0", "0.5", "-1", "1801", "abc", "nan", "inf", "1e9"])
def test_ttl_de_teste_invalido_falha_no_boot(valor: str) -> None:
    with pytest.raises(ConfigError, match="REQUEST_STATE_TTL_S_SOMENTE_TESTE"):
        _cfg(REQUEST_STATE_TTL_S_SOMENTE_TESTE=valor)


def test_ttl_de_teste_tem_precedencia_sobre_o_ttl_normal() -> None:
    cfg = _cfg(REQUEST_STATE_TTL_S="900", REQUEST_STATE_TTL_S_SOMENTE_TESTE="3")
    assert cfg.request_state_ttl_s == 3.0 and cfg.ttl_de_teste is True

