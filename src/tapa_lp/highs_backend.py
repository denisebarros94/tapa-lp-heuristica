"""Backend de PL persistente usando highspy (opcional).

O PuLP reescreve o modelo e reinicia o solver a cada resolução. Neste projeto
isso custa ~6-10 ms por PL — o que domina o tempo dos métodos com heurística.
O highspy permite manter o modelo e o basis entre resoluções: montar o modelo
uma vez e trocar apenas os bounds custa ~0,1-0,2 ms por resolução.

O modelo é o mesmo de `lp_model.TapaRelaxation` (padrões, elo padrão↔célula,
pista branca e proibição de 2×2), sem as variáveis de fluxo de conectividade.
Como no motor PuLP, toda dedução continua vindo exclusivamente de
`Infeasible`; qualquer outro status é "não sei".
"""
from __future__ import annotations

import time

try:  # o backend é opcional: sem highspy, o projeto continua no PuLP
    import highspy
    import numpy as np

    HIGHS_AVAILABLE = True
except ImportError:  # pragma: no cover - depende do ambiente
    highspy = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    HIGHS_AVAILABLE = False

from tapa_solver_deterministico.model import BLACK, UNKNOWN, WHITE, Puzzle
from tapa_solver_deterministico.patterns import patterns_for_clue, ring_indices

from .lp_model import FEASIBLE, INFEASIBLE, UNKNOWN_STATUS, LPStats


class HighsRelaxation:
    """Relaxação linear persistente, com a mesma interface de `TapaRelaxation`."""

    def __init__(self, puzzle: Puzzle, presolve: str = "off") -> None:
        if not HIGHS_AVAILABLE:
            raise ImportError("highspy não está instalado; use lp_backend='pulp'")

        self.puzzle = puzzle
        self.n = puzzle.rows * puzzle.cols
        self.stats = LPStats()
        self.trivially_infeasible = False
        self._time_limit: float | None = None
        self._x_idx = np.arange(self.n, dtype=np.int32)
        self._lb = np.zeros(self.n, dtype=float)
        self._ub = np.ones(self.n, dtype=float)

        self.h = highspy.Highs()
        self.h.setOptionValue("output_flag", False)
        self.h.setOptionValue("presolve", presolve)
        self.h.setOptionValue("solver", "simplex")
        # Não fixamos "threads": o scheduler do HiGHS é global e rejeita
        # modelos com um número de threads diferente do primeiro inicializado.

        costs: list[float] = []
        lbs: list[float] = []
        ubs: list[float] = []
        for _ in range(self.n):
            costs.append(1.0)       # objetivo: minimizar o nº de pretas
            lbs.append(0.0)
            ubs.append(1.0)

        # --- pistas -> padrões (mesma tabela do motor determinístico) -------
        self.clue_info: dict[
            tuple[int, int], tuple[tuple[int | None, ...], tuple[tuple[int, ...], ...]]
        ] = {}
        self.z_cols: dict[tuple[int, int], list[int]] = {}
        for cell, clue in puzzle.clues.items():
            slots = ring_indices(puzzle, cell)
            # célula fora do tabuleiro conta como branca
            pats = tuple(
                bits for bits in patterns_for_clue(clue)
                if all(bits[k] == 0 for k, idx in enumerate(slots) if idx is None)
            )
            if not pats:
                self.trivially_infeasible = True
            base = len(costs)
            self.z_cols[cell] = list(range(base, base + len(pats)))
            for _ in pats:
                costs.append(0.0)
                lbs.append(0.0)
                ubs.append(1.0)
            self.clue_info[cell] = (slots, pats)

        ncol = len(costs)
        self.h.addCols(
            ncol,
            np.asarray(costs, dtype=float),
            np.asarray(lbs, dtype=float),
            np.asarray(ubs, dtype=float),
            0,
            np.zeros(ncol + 1, dtype=np.int32),
            np.array([], dtype=np.int32),
            np.array([], dtype=float),
        )

        rows: list[tuple[float, float, list[tuple[int, float]]]] = []
        for cell, (slots, pats) in self.clue_info.items():
            zidx = self.z_cols[cell]
            rows.append((1.0, 1.0, [(z, 1.0) for z in zidx]))
            for k, idx in enumerate(slots):
                if idx is None:
                    continue
                entries = [(idx, 1.0)] + [
                    (zidx[p], -1.0) for p in range(len(pats)) if pats[p][k] == 1
                ]
                rows.append((0.0, 0.0, entries))
            rows.append((0.0, 0.0, [(puzzle.index(*cell), 1.0)]))

        inf = highspy.kHighsInf
        for row in range(puzzle.rows - 1):
            for col in range(puzzle.cols - 1):
                idxs = (
                    puzzle.index(row, col),
                    puzzle.index(row + 1, col),
                    puzzle.index(row, col + 1),
                    puzzle.index(row + 1, col + 1),
                )
                rows.append((-inf, 3.0, [(i, 1.0) for i in idxs]))

        starts = [0]
        indices: list[int] = []
        values: list[float] = []
        lows: list[float] = []
        highs: list[float] = []
        for lo, hi, entries in rows:
            lows.append(lo)
            highs.append(hi)
            for col, coef in entries:
                indices.append(col)
                values.append(coef)
            starts.append(len(indices))
        self.h.addRows(
            len(rows),
            np.asarray(lows, dtype=float),
            np.asarray(highs, dtype=float),
            len(indices),
            np.asarray(starts, dtype=np.int32),
            np.asarray(indices, dtype=np.int32),
            np.asarray(values, dtype=float),
        )

    # ------------------------------------------------------------------ uso
    def apply(self, values: list[int]) -> None:
        """Fixa os limites de x conforme o tabuleiro parcial do nó atual."""
        self._lb.fill(0.0)
        self._ub.fill(1.0)
        for i, v in enumerate(values):
            if v == BLACK:
                self._lb[i] = 1.0
            elif v == WHITE:
                self._ub[i] = 0.0
        self.h.changeColsBounds(self.n, self._x_idx, self._lb, self._ub)

    def solve(self, time_limit: float | None = None) -> str:
        if self.trivially_infeasible:
            self.stats.solves += 1
            self.stats.infeasible += 1
            return INFEASIBLE

        limit = highspy.kHighsInf if time_limit is None else float(time_limit)
        if limit != self._time_limit:
            self.h.setOptionValue("time_limit", limit)
            self._time_limit = limit

        started = time.monotonic()
        run_status = self.h.run()
        elapsed = time.monotonic() - started
        self.stats.solves += 1
        self.stats.seconds += elapsed

        if run_status != highspy.HighsStatus.kOk:
            self.stats.unknown += 1
            return UNKNOWN_STATUS
        status = self.h.getModelStatus()
        if status == highspy.HighsModelStatus.kOptimal:
            self.stats.optimal += 1
            return FEASIBLE
        if status == highspy.HighsModelStatus.kInfeasible:
            self.stats.infeasible += 1
            return INFEASIBLE
        self.stats.unknown += 1
        return UNKNOWN_STATUS

    def values(self, time_limit: float | None = None) -> dict[int, float] | None:
        if self.solve(time_limit) != FEASIBLE:
            return None
        sol = self.h.getSolution().col_value
        return {i: float(sol[i]) for i in range(self.n)}

    def probe(
        self,
        values: list[int],
        time_limit: float | None = None,
        max_probes: int | None = None,
        deadline: float | None = None,
        only: list[int] | None = None,
    ) -> tuple[bool, dict[int, int]]:
        """Testa x[i]=1 e x[i]=0 por inviabilidade.

        Só `Infeasible` conta como prova; tempo esgotado ou status indefinido
        são "não sei" e nunca viram dedução.
        """
        self.apply(values)
        if self.solve(time_limit) == INFEASIBLE:
            return False, {}

        livres = only if only is not None else [
            i for i, v in enumerate(values) if v == UNKNOWN
        ]
        if max_probes is not None:
            sol = self.h.getSolution().col_value
            vals = {i: float(sol[i]) for i in livres}
            livres = sorted(livres, key=lambda i: -min(vals[i], 1 - vals[i]))[:max_probes]

        forced: dict[int, int] = {}
        for i in livres:
            if deadline is not None and time.monotonic() > deadline:
                break
            self.h.changeColBounds(i, 1.0, 1.0)
            if self.solve(time_limit) == INFEASIBLE:
                forced[i] = WHITE
                self.h.changeColBounds(i, 0.0, 1.0)
                continue
            self.h.changeColBounds(i, 0.0, 0.0)
            if self.solve(time_limit) == INFEASIBLE:
                forced[i] = BLACK
            self.h.changeColBounds(i, 0.0, 1.0)
        return True, forced
