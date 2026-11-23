"""Busca em profundidade com propagação + heurística de Programação Linear.

Fluxo de cada nó:

    propagação (regras locais, ponto fixo)
        ↓
    PL: o nó é viável?           (1 solve)  -> se inviável, backtrack
        ↓
    probing: x[i]=1 / x[i]=0     (2 solves por célula) -> deduções provadas
        ↓  (repete propagação + probing até ponto fixo)
    escolha da célula de decisão (regra de ramificação)
        ↓
    ordem dos valores (preta/branca), recursão
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from tapa_solver_deterministico.model import BLACK, UNKNOWN, WHITE, Board, Puzzle
from tapa_solver_deterministico.patterns import compatible_patterns
from tapa_solver_deterministico.propagation import Contradiction, propagate
from tapa_solver_deterministico.verifier import verify

from .highs_backend import HIGHS_AVAILABLE, HighsRelaxation
from .lp_model import FEASIBLE, TapaRelaxation

Branch = Literal["first", "most_fractional", "fewest_patterns"]
ValueOrder = Literal["black_first", "lp_guided"]
Backend = Literal["auto", "highs", "pulp"]


@dataclass(slots=True)
class LPConfig:
    use_probe: bool = True              # deduções por inviabilidade da PL
    branch: Branch = "most_fractional"  # como escolher a célula de decisão
    value_order: ValueOrder = "lp_guided"
    probe_depth: int | None = 2         # até que profundidade usar PL (None = todas)
    lp_time_limit: float = 5.0          # segundos por resolução de PL
    max_probes_per_node: int | None = None
    # --- otimizações: os padrões abaixo são a configuração MEDIDA mais rápida
    # (ver README, seção "Otimizações"). Nenhuma delas afeta a correção: as
    # deduções continuam vindo só de inviabilidade da PL, e a conectividade
    # segue exigida pela propagação e pelo verificador.
    probe_scope: str = "near_clue"      # "all" | "near_clue" (células em anel de pista)
    integral_exit: bool = True          # PL já 0/1 e válida -> retorna sem buscar
    connectivity_lp: bool = False       # fluxo no modelo de PL (caro e frouxo)
    lp_backend: Backend = "auto"        # "auto" | "highs" | "pulp"
    solver_name: str = "auto"           # usado só no backend PuLP: "auto" | "cbc" | "highs"


@dataclass(slots=True)
class LPSearchStats:
    nodes: int = 0
    decisions: int = 0
    backtracks: int = 0
    contradictions: int = 0
    propagated_assignments: int = 0
    propagation_rounds: int = 0
    max_depth: int = 0
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    node_limit_reached: bool = False
    by_rule: Counter[str] = field(default_factory=Counter)
    lp_solves: int = 0
    lp_infeasible: int = 0
    lp_deductions_black: int = 0
    lp_deductions_white: int = 0
    lp_seconds: float = 0.0
    lp_nodes: int = 0
    cancelled: bool = False
    branch_rule_used: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class LPSolveResult:
    solved: bool
    board: Board | None
    stats: LPSearchStats
    message: str


class _StopSearch(Exception):
    pass


def _assign(board: Board, index: int, value: int, rule: str, stats: LPSearchStats) -> bool:
    current = board.values[index]
    if current == value:
        return False
    if current != UNKNOWN:
        raise Contradiction(f"atribuições incompatíveis na célula {board.puzzle.cell(index)}")
    board.values[index] = value
    stats.propagated_assignments += 1
    stats.by_rule[rule] += 1
    return True


def _fewest_patterns_score(board: Board, index: int) -> int:
    """Quantos padrões compatíveis ainda existem para as pistas que veem a célula."""
    total = 0
    row, col = board.puzzle.cell(index)
    for clue_cell, _ in board.puzzle.clues.items():
        dr, dc = abs(clue_cell[0] - row), abs(clue_cell[1] - col)
        if dr <= 1 and dc <= 1 and (dr, dc) != (0, 0):
            total += len(compatible_patterns(board, clue_cell))
    return total if total else 10**6


def solve_with_lp(
    puzzle: Puzzle,
    config: LPConfig | None = None,
    timeout_seconds: float | None = 120.0,
    node_limit: int | None = 1_000_000,
    progress=None,
    should_stop=None,
) -> LPSolveResult:
    cfg = config or LPConfig()
    stats = LPSearchStats()
    started = time.monotonic()
    precisa_pl = cfg.use_probe or cfg.branch != "first" or cfg.integral_exit
    use_highs = (cfg.lp_backend in {"auto", "highs"} and HIGHS_AVAILABLE
                 and not cfg.connectivity_lp)
    if not precisa_pl:
        relaxation = None
    elif use_highs:
        # Persistente: monta o modelo uma vez e só troca bounds a cada nó.
        relaxation = HighsRelaxation(puzzle)
    else:
        # PuLP: também funciona com conectividade por fluxo (connectivity_lp).
        relaxation = TapaRelaxation(puzzle, connectivity=cfg.connectivity_lp,
                                    solver_name=cfg.solver_name)
    near_clue: set[int] = set()
    for clue_cell in puzzle.clues:
        r0, c0 = clue_cell
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (dr, dc) == (0, 0):
                    continue
                if puzzle.in_bounds(r0 + dr, c0 + dc):
                    near_clue.add(puzzle.index(r0 + dr, c0 + dc))

    def emit() -> None:
        if progress is not None:
            progress({
                "nodes": stats.nodes, "decisions": stats.decisions,
                "backtracks": stats.backtracks, "contradictions": stats.contradictions,
                "lpSolves": stats.lp_solves, "elapsed": time.monotonic() - started,
            })

    def check_limits() -> None:
        if should_stop is not None and should_stop():
            stats.cancelled = True
            raise _StopSearch
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            stats.timed_out = True
            raise _StopSearch
        if node_limit is not None and stats.nodes >= node_limit:
            stats.node_limit_reached = True
            raise _StopSearch

    def lp_deduce(board: Board, depth: int) -> None:
        """Aplica PL + propagação até ponto fixo. Levanta Contradiction se o nó morre."""
        if relaxation is None:
            return
        if cfg.probe_depth is not None and depth > cfg.probe_depth:
            return
        stats.lp_nodes += 1
        deadline = started + timeout_seconds if timeout_seconds else None
        for _ in range(10):  # ponto fixo PL <-> propagação
            t0 = time.monotonic()
            alvo = None
            if cfg.probe_scope == "near_clue":
                alvo = [i for i in range(len(board.values))
                        if board.values[i] == UNKNOWN and i in near_clue]
            ok, forced = relaxation.probe(
                board.values,
                time_limit=cfg.lp_time_limit,
                max_probes=cfg.max_probes_per_node,
                deadline=deadline,
                only=alvo,
            )
            stats.lp_seconds += time.monotonic() - t0
            if not ok:
                raise Contradiction("a relaxação linear do nó é inviável")
            if not forced:
                return
            for index, value in forced.items():
                _assign(board, index, value, "lp_probing", stats)
                if value == BLACK:
                    stats.lp_deductions_black += 1
                else:
                    stats.lp_deductions_white += 1
            report = propagate(board)  # a nova fixação pode destravar regras locais
            stats.propagated_assignments += report.assignments
            stats.propagation_rounds += report.iterations
            stats.by_rule.update(report.by_rule)

    def choose_branch(board: Board) -> tuple[int, int]:
        """Escolhe célula e cor. Resolve a PL NO MÁXIMO uma vez (antes eram duas)."""
        unknown = board.unknown_indices()
        stats.branch_rule_used[cfg.branch] += 1
        precisa_valores = relaxation is not None and (cfg.branch == "most_fractional"
                                                     or cfg.value_order == "lp_guided")
        vals = None
        if precisa_valores:
            relaxation.apply(board.values)
            t0 = time.monotonic()
            vals = relaxation.values(cfg.lp_time_limit)
            stats.lp_seconds += time.monotonic() - t0
        vals = vals or {}

        if cfg.branch == "first" or relaxation is None:
            index = unknown[0]
        elif cfg.branch == "fewest_patterns":
            index = min(unknown, key=lambda i: (_fewest_patterns_score(board, i), i))
        else:  # most_fractional
            index = max(unknown, key=lambda i: (min(vals.get(i, 0.5), 1 - vals.get(i, 0.5)), -i))

        if cfg.value_order == "lp_guided" and vals:
            value_first = BLACK if vals.get(index, 0.5) >= 0.5 else WHITE
        else:
            value_first = BLACK
        return index, value_first

    def search(board: Board, depth: int) -> Board | None:
        check_limits()
        stats.nodes += 1
        stats.max_depth = max(stats.max_depth, depth)
        emit()
        try:
            report = propagate(board)
            stats.propagated_assignments += report.assignments
            stats.propagation_rounds += report.iterations
            stats.by_rule.update(report.by_rule)
            lp_deduce(board, depth)
        except Contradiction:
            stats.contradictions += 1
            return None

        if board.is_complete():
            return board if verify(board).valid else None

        if cfg.integral_exit and depth == 0 and relaxation is not None:
            relaxation.apply(board.values)
            vals = relaxation.values(cfg.lp_time_limit)
            if vals is not None and all(v < 1e-6 or v > 1 - 1e-6 for v in vals.values()):
                tentativa = board.copy()
                for i, v in vals.items():
                    tentativa.values[i] = BLACK if v > 0.5 else WHITE
                if verify(tentativa).valid:
                    stats.by_rule["lp_integral"] += 1
                    return tentativa

        index, first = choose_branch(board)
        stats.decisions += 1
        for color in ((first, WHITE if first == BLACK else BLACK)):
            child = board.copy()
            child.values[index] = color
            answer = search(child, depth + 1)
            if answer is not None:
                return answer
            stats.backtracks += 1
        return None

    try:
        board = search(Board.initial(puzzle), 0)
    except _StopSearch:
        board = None

    stats.elapsed_seconds = time.monotonic() - started
    if relaxation is not None:
        stats.lp_solves = relaxation.stats.solves
        stats.lp_infeasible = relaxation.stats.infeasible
    if board is not None:
        return LPSolveResult(True, board, stats, "solução encontrada e verificada")
    if stats.cancelled:
        return LPSolveResult(False, None, stats, "cancelado pelo usuário")
    if stats.timed_out:
        return LPSolveResult(False, None, stats, "tempo limite atingido")
    if stats.node_limit_reached:
        return LPSolveResult(False, None, stats, "limite de nós atingido")
    return LPSolveResult(False, None, stats, "a instância não possui solução")
