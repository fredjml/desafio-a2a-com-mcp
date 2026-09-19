"""F-06: sem `servidor-mcp/.venv` a suite do agente FALHA, salvo pedido explicito para pular."""

from __future__ import annotations

import pytest

from . import procs


def test_sem_venv_do_servidor_falha_por_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs, "python_do_servidor", lambda: None)
    monkeypatch.delenv("AGENTE_TESTS_SEM_SERVIDOR", raising=False)
    with pytest.raises(pytest.fail.Exception, match="servidor-mcp/.venv ausente"):
        procs.exigir_servidor_real()


@pytest.mark.parametrize("valor", ["", "0", "true", "sim"])
def test_so_o_valor_1_permite_pular(monkeypatch: pytest.MonkeyPatch, valor: str) -> None:
    monkeypatch.setattr(procs, "python_do_servidor", lambda: None)
    monkeypatch.setenv("AGENTE_TESTS_SEM_SERVIDOR", valor)
    with pytest.raises(pytest.fail.Exception):
        procs.exigir_servidor_real()


def test_variavel_explicita_faz_pular(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs, "python_do_servidor", lambda: None)
    monkeypatch.setenv("AGENTE_TESTS_SEM_SERVIDOR", "1")
    with pytest.raises(pytest.skip.Exception):
        procs.exigir_servidor_real()


def test_com_o_venv_do_servidor_nao_faz_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs, "python_do_servidor", lambda: procs.SERVIDOR_DIR)
    procs.exigir_servidor_real()
