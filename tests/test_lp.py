"""Testes da heurística de PL.

Ponto central: um tabuleiro pode ter MAIS DE UMA solução. Logo:
  * o teste de equivalência entre motores só vale em tabuleiro de solução única;
  * deduções da PL por inviabilidade valem em QUALQUER tabuleiro, porque são
    forçadas em todas as soluções — então podem ser comparadas com qualquer
    solução válida.
"""
import pathlib

import pytest

from tapa_solver_deterministico.model import BLACK, Board, Puzzle
from tapa_solver_deterministico.solver import solve as solve_det
from tapa_solver_deterministico.verifier import verify

from tapa_lp import (HIGHS_AVAILABLE, HighsRelaxation, LPConfig, TapaRelaxation,
                     count_solutions, solve_with_lp)
from tapa_lp.io_puzzles import load_puzzle

EXEMPLOS = pathlib.Path(__file__).resolve().parents[1] / "examples"
ARQUIVOS = sorted(EXEMPLOS.glob("*.txt"))

# exemplos comprovadamente de solução única (verificados com count_solutions)
UNICOS = {"4x4_fixture.txt", "6x6_01.txt"}
# tabuleiros pequenos, para testes que rodam em qualquer máquina
RAPIDOS = [p for p in ARQUIVOS if p.name in {"4x4_fixture.txt", "6x6_01.txt"}]
# orçamento generoso: o teste mede correção, não desempenho
ORCAMENTO = dict(timeout_seconds=180, node_limit=500_000)


@pytest.mark.parametrize("path", ARQUIVOS, ids=lambda p: p.name)
def test_ambos_os_motores_produzem_solucao_valida(path):
    """Correção dos dois motores em todos os exemplos (orçamento folgado)."""
    puzzle = load_puzzle(path)
    a = solve_det(puzzle, **ORCAMENTO)
    b = solve_with_lp(puzzle, LPConfig(), **ORCAMENTO)
    assert a.solved, f"determinístico falhou em {path.name}"
    assert b.solved, f"LP falhou em {path.name}"
    assert verify(a.board).valid, path.name
    assert verify(b.board).valid, path.name


@pytest.mark.parametrize("path", [p for p in ARQUIVOS if p.name in UNICOS], ids=lambda p: p.name)
def test_motores_concordam_quando_a_solucao_e_unica(path):
    puzzle = load_puzzle(path)
    a = solve_det(puzzle, **ORCAMENTO)
    b = solve_with_lp(puzzle, LPConfig(), **ORCAMENTO)
    assert a.board.values == b.board.values, f"{path.name}: soluções diferentes em tabuleiro único"


@pytest.mark.parametrize("path", ARQUIVOS, ids=lambda p: p.name)
def test_deducoes_da_pl_sao_corretas_em_todas_as_solucoes(path):
    """Dedução por inviabilidade vale em qualquer solução: confere contra uma."""
    puzzle = load_puzzle(path)
    ref = solve_det(puzzle, **ORCAMENTO)
    assert ref.solved

    valores = [-1] * (puzzle.rows * puzzle.cols)
    for r, c in puzzle.clues:
        valores[puzzle.index(r, c)] = 0     # célula de pista é branca

    rel = TapaRelaxation(puzzle, connectivity=True)  # exercita o modelo completo
    ok, forced = rel.probe(valores)
    assert ok, f"{path.name}: a relaxação declarou inviável um tabuleiro viável"

    erros = [puzzle.cell(i) for i, v in forced.items() if ref.board.values[i] != v]
    assert not erros, f"{path.name}: deduções erradas em {erros}"


@pytest.mark.skipif(not HIGHS_AVAILABLE, reason="highspy não instalado")
@pytest.mark.parametrize("path", ARQUIVOS, ids=lambda p: p.name)
def test_backend_highs_concorda_com_o_pulp(path):
    """Os dois backends montam o mesmo modelo: mesmas deduções por inviabilidade."""
    puzzle = load_puzzle(path)
    valores = [-1] * (puzzle.rows * puzzle.cols)
    for r, c in puzzle.clues:
        valores[puzzle.index(r, c)] = 0

    rel_pulp = TapaRelaxation(puzzle, connectivity=False, solver_name="cbc")
    ok_pulp, forced_pulp = rel_pulp.probe(valores)

    rel_highs = HighsRelaxation(puzzle)
    ok_highs, forced_highs = rel_highs.probe(valores)

    assert ok_pulp == ok_highs, path.name
    assert forced_pulp == forced_highs, path.name


@pytest.mark.skipif(not HIGHS_AVAILABLE, reason="highspy não instalado")
@pytest.mark.parametrize("path", RAPIDOS, ids=lambda p: p.name)
def test_backend_highs_forcado_resolve_os_exemplos(path):
    puzzle = load_puzzle(path)
    r = solve_with_lp(puzzle, LPConfig(lp_backend="highs"), **ORCAMENTO)
    assert r.solved, r.message
    assert verify(r.board).valid


@pytest.mark.parametrize("path", ARQUIVOS, ids=lambda p: p.name)
def test_contagem_de_solucoes_bate_com_o_esperado(path):
    puzzle = load_puzzle(path)
    sols = count_solutions(puzzle, max_solutions=2)
    assert sols, f"{path.name}: nenhuma solução"
    if path.name in UNICOS:
        assert len(sols) == 1, f"{path.name} deveria ser único"
    for sol in sols:
        assert verify(Board(puzzle, sol)).valid


def test_config_otimizada_resolve_os_mesmos_tabuleiros():
    """A configuração padrão (otimizada) não pode perder nenhuma solução."""
    rapida = LPConfig()
    completa = LPConfig(probe_scope="all", integral_exit=False, connectivity_lp=True)
    for path in RAPIDOS:
        puzzle = load_puzzle(path)
        a = solve_with_lp(puzzle, rapida, **ORCAMENTO)
        b = solve_with_lp(puzzle, completa, **ORCAMENTO)
        assert a.solved and b.solved, path.name
        assert a.board.values == b.board.values, path.name


def test_pl_detecta_tabuleiro_impossivel():
    # pista 8 numa célula de canto: só existem 3 vizinhos -> impossível
    puzzle = Puzzle(3, 3, {(0, 0): (8,)})
    r = solve_with_lp(puzzle, LPConfig(), timeout_seconds=10, node_limit=1_000)
    assert r.solved is False


def test_pista_zero_exige_anel_vazio():
    puzzle = Puzzle(3, 3, {(1, 1): (0,)})
    r = solve_with_lp(puzzle, LPConfig(), timeout_seconds=10, node_limit=1_000)
    # sem nenhuma preta não pode existir muro: o verificador rejeita
    assert r.solved is False
