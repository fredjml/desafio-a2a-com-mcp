"""R-PARSE-01 / T-22: parser estrito. So traduz; nenhuma regra de dominio."""

from __future__ import annotations

import itertools
import random
import string

import pytest

from app.parser import (
    CAMPOS,
    FORMATO,
    MAX_CAMPO,
    MAX_TEXTO,
    Pedido,
    PedidoInvalido,
    parse_escolha,
    parse_pedido,
)

INI = "2026-11-03T14:00:00-03:00"
FIM = "2026-11-03T15:00:00-03:00"
CANONICO = f"reservar sala=sala-garagem inicio={INI} fim={FIM} responsavel=Marty"


def esperado(**kw: str) -> Pedido:
    base = {"sala": "sala-garagem", "inicio": INI, "fim": FIM, "responsavel": "Marty"}
    base.update(kw)
    return Pedido(**base)


def campos(**kw: str) -> dict[str, str]:
    base = {"sala": "sala-garagem", "inicio": INI, "fim": FIM, "responsavel": "Marty"}
    base.update(kw)
    return base


def montar(ordem: tuple[str, ...], **kw: str) -> str:
    c = campos(**kw)
    return "reservar " + " ".join(f"{k}={c[k]}" for k in ordem)


def invalido(texto: str) -> str:
    with pytest.raises(PedidoInvalido) as exc:
        parse_pedido(texto)
    mensagem = str(exc.value)
    assert mensagem.startswith("Pedido invalido: ")
    assert FORMATO in mensagem  # a mensagem ensina o formato
    return mensagem


# ---------------------------------------------------------------------------- aceitos
def test_pedido_canonico() -> None:
    assert parse_pedido(CANONICO) == esperado()
    assert parse_pedido(CANONICO).argumentos() == campos()


@pytest.mark.parametrize("ordem", list(itertools.permutations(CAMPOS)))
def test_chaves_em_qualquer_ordem(ordem: tuple[str, ...]) -> None:
    assert parse_pedido(montar(ordem)) == esperado()


@pytest.mark.parametrize("sufixo", ["", "\n", "\r\n", "  \r\n", "\r\n\r\n"])
def test_quebra_de_linha_final_e_tolerada(sufixo: str) -> None:
    assert parse_pedido(CANONICO + sufixo) == esperado()
    assert parse_pedido("  " + CANONICO + sufixo) == esperado()


def test_responsavel_com_espacos_vale_o_resto_da_linha() -> None:
    p = parse_pedido(f"reservar sala=s inicio={INI} fim={FIM} responsavel=Marty  McFly Junior")
    assert p.responsavel == "Marty  McFly Junior"  # espacos internos preservados


def test_responsavel_no_meio_vai_ate_a_proxima_chave() -> None:
    p = parse_pedido(f"reservar responsavel=Emmett Brown sala=s inicio={INI} fim={FIM}")
    assert (p.responsavel, p.sala) == ("Emmett Brown", "s")


@pytest.mark.parametrize(
    "nome", ['Doc"; DROP TABLE x', "O'Brien", "a=b", "x\\y", "<b>Doc</b>", "Zé"]
)
def test_aspas_e_simbolos_no_responsavel_passam_como_dado(nome: str) -> None:
    assert (
        parse_pedido(f"reservar sala=s inicio={INI} fim={FIM} responsavel={nome}").responsavel
        == nome
    )


def test_valores_sao_repassados_verbatim() -> None:
    for ini, fim in [
        ("2026-11-03T09:00:00Z", "2026-11-03T10:00:00Z"),
        ("2026-11-03T09:00", "2026-11-03T10:00"),
        ("2026-11-03T09:00:00.123-03:00", "2026-11-03T10:00:00.5+00:00"),
    ]:
        p = parse_pedido(f"reservar sala=s inicio={ini} fim={fim} responsavel=D")
        assert (p.inicio, p.fim) == (ini, fim)


@pytest.mark.parametrize(
    "kw",
    [
        {"sala": "sala-delorean"},  # sala inexistente: quem sabe e o servidor
        {"sala": "SALA-COM-MAIUSCULA"},
        {
            "inicio": "2026-11-03T07:00:00-03:00",
            "fim": "2026-11-03T08:00:00-03:00",
        },  # fora da janela
        {"inicio": "2026-11-03T09:00:00-03:00", "fim": "2026-11-03T13:00:00-03:00"},  # > 2 h
        {"inicio": "2026-11-03T10:00:00-03:00", "fim": "2026-11-03T09:00:00-03:00"},  # invertido
        {"inicio": "2020-01-01T09:00:00-03:00", "fim": "2020-01-01T10:00:00-03:00"},  # passado
        {"inicio": INI, "fim": INI},  # duracao zero
    ],
)
def test_nenhuma_regra_de_dominio_no_agente(kw: dict[str, str]) -> None:
    """T-22: o agente traduz, nao julga (sala/janela/duracao/ordem sao do servidor MCP)."""
    assert parse_pedido(montar(CAMPOS, **kw)).argumentos() == campos(**kw)


# ---------------------------------------------------------------------------- rejeitados
@pytest.mark.parametrize("faltando", CAMPOS)
def test_campo_faltando(faltando: str) -> None:
    ordem = tuple(c for c in CAMPOS if c != faltando)
    assert f"faltando {faltando}" in invalido(montar(ordem))


def test_varios_campos_faltando_sao_todos_listados() -> None:
    m = invalido("reservar sala=s")
    assert "faltando inicio, fim, responsavel" in m


@pytest.mark.parametrize("dup", CAMPOS)
def test_campo_duplicado(dup: str) -> None:
    texto = CANONICO + f" {dup}=outro"
    if dup == "responsavel":
        texto = CANONICO.replace("responsavel=Marty", "responsavel=Marty responsavel=Doc")
    assert f"campo '{dup}' repetido" in invalido(texto)


def test_duplicado_disfarcado_de_injecao_de_campo() -> None:
    """`responsavel=Doc sala=sala-delorean` nao pode trocar a sala em silencio."""
    assert "repetido" in invalido(CANONICO + " sala=sala-delorean")


@pytest.mark.parametrize(
    "extra",
    [
        "extra=1",
        "responsavel=Marty sala=sala-x",
        "cor=azul",
        "sala2=x",
        "Sala=x",
    ],
)
def test_campo_extra_ou_desconhecido(extra: str) -> None:
    texto = f"reservar sala=s inicio={INI} fim={FIM} {extra} responsavel=Marty"
    m = invalido(texto)
    assert "desconhecido" in m or "repetido" in m


def test_campo_desconhecido_depois_do_responsavel_tambem_e_recusado() -> None:
    assert "desconhecido" in invalido(CANONICO + " extra=1")


@pytest.mark.parametrize("campo", CAMPOS)
def test_valor_vazio(campo: str) -> None:
    c = campos()
    c[campo] = ""
    texto = "reservar " + " ".join(f"{k}={v}" if v else f"{k}=" for k, v in c.items())
    assert f"campo '{campo}' sem valor" in invalido(texto)


@pytest.mark.parametrize(
    "ruim",
    [
        "amanha",
        "2026-11-03",
        "2026-13-03T14:00:00-03:00",
        "2026-11-31T14:00:00-03:00",
        "2026-02-30T14:00:00",
        "2026-11-03T25:00:00-03:00",
        "2026-11-03T14:60:00-03:00",
        "2026-11-03T14:00:61",
        "2026-11-03T14:00:00-25:00",
        "2026-11-03T14:00:00-03:99",
        "2026-11-03T14:00:00-0300",
        "20261103T140000",
        "26-11-03T14:00:00",
        "2026-11-03T14:00:00Zextra",
        "2026-11-03T14:00:00;DROP",
        "'2026-11-03T14:00:00'",
    ],
)
def test_datas_sao_repassadas_verbatim_o_servidor_e_o_dono_da_validacao(ruim: str) -> None:
    """O agente so traduz: sem fuso, lixo ou data impossivel passam intactos ao servidor."""
    for campo in ("inicio", "fim"):
        assert parse_pedido(montar(CAMPOS, **{campo: ruim})).argumentos()[campo] == ruim


def test_iso_sem_fuso_nao_e_recusado_pelo_agente() -> None:
    sem_fuso = "2026-11-03T14:00:00"
    assert parse_pedido(montar(CAMPOS, inicio=sem_fuso, fim=sem_fuso)).inicio == sem_fuso


@pytest.mark.parametrize("campo", ["sala", "inicio", "fim"])
def test_texto_solto_dentro_de_valor_simples(campo: str) -> None:
    assert "espacos" in invalido(montar(CAMPOS, **{campo: campos()[campo] + " lixo"}))


def test_data_e_hora_separadas_por_espaco_sao_texto_solto_no_formato() -> None:
    assert "espacos" in invalido(montar(CAMPOS, inicio="2026-11-03 14:00:00"))


@pytest.mark.parametrize(
    "texto",
    [
        "",
        "   ",
        "\r\n",
        "reservar",
        "reservar ",
        "Reservar sala=s inicio=x fim=y responsavel=z",
        "reserva sala=s",
        "cancelar sala=s",
        "reservar-sala sala=s",
        "reservarsala=s",
        "  quero reservar a sala-garagem das 14 as 15",
        "sala=s inicio=x fim=y responsavel=z",
        "escolha=sala-fusca",
        "reservar lixo antes sala=s inicio=x fim=y responsavel=z",
    ],
)
def test_nao_e_um_pedido(texto: str) -> None:
    invalido(texto)


@pytest.mark.parametrize(
    "injecao",
    [
        "\nsala=sala-delorean",
        "\r\nsala=sala-delorean",
        "\rsala=x",
        "\x00",
        "\x1b[31m",
        "\t",
        "\u2028sala=x",
        "\u2029",
        "\x85",
        "\x7f",
    ],
)
def test_injecao_de_quebra_de_linha_e_controle_no_responsavel(injecao: str) -> None:
    """Embutida no meio do valor (nas pontas, so espaco/fim de linha e tolerado pelo strip)."""
    texto = f"reservar sala=s inicio={INI} fim={FIM} responsavel=Do{injecao}c"
    assert "unica linha" in invalido(texto)


def test_quebra_no_meio_do_pedido() -> None:
    assert "unica linha" in invalido(f"reservar sala=s\ninicio={INI} fim={FIM} responsavel=D")
    assert "unica linha" in invalido(f"reservar sala=s inicio={INI}\r\nfim={FIM} responsavel=D")


def test_limites_de_tamanho() -> None:
    assert "acima de" in invalido("reservar " + "x" * (MAX_TEXTO + 1))
    longo = "d" * (MAX_CAMPO + 1)
    assert "acima de" in invalido(f"reservar sala=s inicio={INI} fim={FIM} responsavel={longo}")
    ok = "d" * MAX_CAMPO
    assert (
        parse_pedido(f"reservar sala=s inicio={INI} fim={FIM} responsavel={ok}").responsavel == ok
    )


def test_fuzz_nunca_levanta_excecao_diferente_de_pedido_invalido() -> None:
    rng = random.Random(20260918)
    alfabeto = string.printable + "áé\u2028=" * 3
    pecas = ["reservar", "sala=", "inicio=", "fim=", "responsavel=", INI, "x", " ", "\n", "="]
    for _ in range(3000):
        if rng.random() < 0.5:
            texto = "".join(rng.choice(alfabeto) for _ in range(rng.randint(0, 80)))
        else:
            texto = "".join(rng.choice(pecas) for _ in range(rng.randint(0, 14)))
        try:
            p = parse_pedido(texto)
        except PedidoInvalido:
            continue
        assert set(p.argumentos()) == set(CAMPOS)


# ---------------------------------------------------------------------------- escolha=
@pytest.mark.parametrize(
    ("texto", "valor"),
    [
        ("escolha=sala-fusca", "sala-fusca"),
        ("escolha=sala-fusca\r\n", "sala-fusca"),
        ("escolha=recusar", "recusar"),
        ("escolha=", ""),
        ("escolha=Sala Fusca", "Sala Fusca"),
        ("escolha=SALA-FUSCA", "SALA-FUSCA"),
        ("  escolha=sala-xyz  ", "sala-xyz"),
    ],
)
def test_parse_escolha(texto: str, valor: str) -> None:
    assert parse_escolha(texto) == valor


@pytest.mark.parametrize(
    "texto", ["", "sala-fusca", "escolha", "escolha sala-fusca", "escolha=a\nb", "reservar x"]
)
def test_parse_escolha_invalida(texto: str) -> None:
    with pytest.raises(PedidoInvalido) as exc:
        parse_escolha(texto)
    assert str(exc.value).startswith("Resposta invalida: ")
