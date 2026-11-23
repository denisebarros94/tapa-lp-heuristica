"""Relaxação linear (PL contínua) do modelo binário do Tapa.

O modelo reaproveita EXATAMENTE a tabela de padrões do motor determinístico
(`patterns_for_clue` + `ring_indices`), o que garante que a relaxação e a
propagação falem a mesma língua: se a PL diz que uma célula não pode ser
preta, a propagação jamais diria o contrário.

Variáveis
---------
x[i]   ∈ [0,1]   1 = célula preta
z[c,p] ∈ [0,1]   escolha do padrão p no anel da pista c
f[a]   ≥ 0       fluxo na aresta dirigida a (conectividade)
y[i]   ∈ [0,1]   raiz do fluxo (exatamente uma)
src[i] ≥ 0       suprimento de fluxo na raiz

Restrições
----------
1. padrões:       Σ_p z[c,p] = 1
2. elo:           x[nb] = Σ_{p ∋ nb} z[c,p]        para cada vizinho do anel
3. pista branca:  x[c] = 0
4. sem 2x2:       Σ x num bloco 2x2 ≤ 3
5. uma raiz:      Σ_i y[i] = 1,  y[i] ≤ x[i]
6. fluxo:         f[a] ≤ n·x[u] e f[a] ≤ n·x[v]
7. conservação:   Σ_in f + src[i] − Σ_out f = x[i],  src[i] ≤ n·y[i]

Toda solução inteira válida do Tapa satisfaz 1–7, logo a relaxação contém o
conjunto viável inteiro. É essa inclusão que autoriza as deduções por
inviabilidade: se a relaxação (conjunto maior) não tem ponto com x[i]=1,
o conjunto inteiro (menor) também não tem.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache

import pulp

from tapa_solver_deterministico.model import BLACK, UNKNOWN, WHITE, Puzzle
from tapa_solver_deterministico.patterns import patterns_for_clue, ring_indices

FEASIBLE = "feasible"
INFEASIBLE = "infeasible"
UNKNOWN_STATUS = "unknown"

DIRS_ORTHO = ((1, 0), (0, 1))


@lru_cache(maxsize=1)
def _tem_highs() -> bool:
    """Consulta cara (lista executáveis); o resultado não muda em runtime."""
    try:
        return "HiGHS" in pulp.listSolvers(onlyAvailable=True)
    except Exception:
        return False


@dataclass(slots=True)
class LPStats:
    solves: int = 0
    infeasible: int = 0
    optimal: int = 0
    unknown: int = 0
    seconds: float = 0.0

    def merge(self, other: "LPStats") -> None:
        self.solves += other.solves
        self.infeasible += other.infeasible
        self.optimal += other.optimal
        self.unknown += other.unknown
        self.seconds += other.seconds


class TapaRelaxation:
    """Uma instância de PL reaproveitada em vários nós da busca.

    O modelo é montado UMA vez por tabuleiro; cada nó apenas troca os limites
    (bounds) das variáveis x conforme as células já decididas. Assim o custo
    de montagem é pago uma vez e o solver só refaz a otimização.
    """

    def __init__(self, puzzle: Puzzle, connectivity: bool = True, solver_name: str = "auto") -> None:
        self.puzzle = puzzle
        self.solver_name = solver_name
        self.n = puzzle.rows * puzzle.cols
        self.connectivity = connectivity
        self.stats = LPStats()
        self.trivially_infeasible = False

        prob = pulp.LpProblem("tapa_relaxacao", pulp.LpMinimize)
        self.prob = prob

        cells = list(range(self.n))
        self.x = {i: pulp.LpVariable(f"x_{i}", lowBound=0, upBound=1) for i in cells}

        # --- pistas -> padrões (mesma tabela do motor determinístico) ---------
        self.clue_info: dict[tuple[int, int], tuple[tuple[int | None, ...], tuple[tuple[int, ...], ...]]] = {}
        for cell, clue in puzzle.clues.items():
            slots = ring_indices(puzzle, cell)
            pats = []
            for bits in patterns_for_clue(clue):
                # célula fora do tabuleiro conta como branca
                if all(bits[k] == 0 for k, idx in enumerate(slots) if idx is None):
                    pats.append(bits)
            if not pats:
                self.trivially_infeasible = True
            self.clue_info[cell] = (slots, tuple(pats))

        for cell, (slots, pats) in self.clue_info.items():
            z = [pulp.LpVariable(f"z_{cell[0]}_{cell[1]}_{p}", lowBound=0, upBound=1)
                 for p in range(len(pats))]
            prob += pulp.lpSum(z) == 1, f"padrao_um_{cell[0]}_{cell[1]}"
            for k, idx in enumerate(slots):
                if idx is None:
                    continue
                prob += (self.x[idx] == pulp.lpSum(z[p] for p in range(len(pats)) if pats[p][k]),
                         f"elo_{cell[0]}_{cell[1]}_{k}")
            prob += self.x[puzzle.index(*cell)] == 0, f"pista_branca_{cell[0]}_{cell[1]}"

        # --- proibição de 2x2 preto ------------------------------------------
        for row in range(puzzle.rows - 1):
            for col in range(puzzle.cols - 1):
                bloco = [puzzle.index(row, col), puzzle.index(row + 1, col),
                         puzzle.index(row, col + 1), puzzle.index(row + 1, col + 1)]
                prob += (pulp.lpSum(self.x[i] for i in bloco) <= 3,
                         f"sem_2x2_{row}_{col}")

        # --- conectividade por fluxo de raiz única ---------------------------
        if connectivity:
            arcs = []
            for row in range(puzzle.rows):
                for col in range(puzzle.cols):
                    u = puzzle.index(row, col)
                    for dr, dc in DIRS_ORTHO:
                        nr, nc = row + dr, col + dc
                        if puzzle.in_bounds(nr, nc):
                            v = puzzle.index(nr, nc)
                            arcs.append((u, v))
                            arcs.append((v, u))
            f = [pulp.LpVariable(f"f_{k}", lowBound=0, upBound=self.n) for k in range(len(arcs))]
            src = [pulp.LpVariable(f"src_{i}", lowBound=0, upBound=self.n) for i in cells]
            y = {i: pulp.LpVariable(f"y_{i}", lowBound=0, upBound=1) for i in cells}
            entrada: dict[int, list] = {i: [] for i in cells}
            saida: dict[int, list] = {i: [] for i in cells}
            for k, (u, v) in enumerate(arcs):
                prob += f[k] <= self.n * self.x[u], f"cap_u_{k}"
                prob += f[k] <= self.n * self.x[v], f"cap_v_{k}"
                saida[u].append(f[k])
                entrada[v].append(f[k])
            prob += pulp.lpSum(y[i] for i in cells) == 1, "uma_raiz"
            for i in cells:
                prob += y[i] <= self.x[i], f"raiz_preta_{i}"
                prob += src[i] <= self.n * y[i], f"src_raiz_{i}"
                prob += (pulp.lpSum(entrada[i]) + src[i] - pulp.lpSum(saida[i]) == self.x[i],
                         f"conservacao_{i}")

        # objetivo de desempate (não altera a viabilidade)
        prob += pulp.lpSum(self.x[i] for i in cells)

    # ------------------------------------------------------------------ uso
    def apply(self, values: list[int]) -> None:
        """Fixa os limites de x conforme o tabuleiro parcial do nó atual."""
        for i, v in enumerate(values):
            var = self.x[i]
            if v == BLACK:
                var.lowBound, var.upBound = 1, 1
            elif v == WHITE:
                var.lowBound, var.upBound = 0, 0
            else:
                var.lowBound, var.upBound = 0, 1

    def solve(self, time_limit: float | None = None) -> str:
        if self.trivially_infeasible:
            self.stats.solves += 1
            self.stats.infeasible += 1
            return INFEASIBLE
        started = time.monotonic()
        nome = self.solver_name
        tem_highs = _tem_highs()
        if nome == "auto":
            nome = "hi_highs" if tem_highs else "cbc"
        cbc = (pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit) if time_limit
               else pulp.PULP_CBC_CMD(msg=False))
        if nome in {"hi_highs", "highs"} and tem_highs:
            solver = pulp.HiGHS(msg=False, timeLimit=time_limit) if time_limit else pulp.HiGHS(msg=False)
        else:
            solver = cbc
        try:
            status = pulp.LpStatus[self.prob.solve(solver)]
        except Exception:
            # solver opcional indisponível: cai para o CBC em vez de quebrar
            status = pulp.LpStatus[self.prob.solve(cbc)]
        self.stats.solves += 1
        if status == "Infeasible":
            self.stats.infeasible += 1
            return INFEASIBLE
        if status == "Optimal":
            self.stats.optimal += 1
            return FEASIBLE
        self.stats.unknown += 1
        return UNKNOWN_STATUS

    def values(self, time_limit: float | None = None) -> dict[int, float] | None:
        """Uma resolução -> valores de x (usados para escolher a ramificação)."""
        if self.solve(time_limit) != FEASIBLE:
            return None
        return {i: (self.x[i].value() or 0.0) for i in self.x}

    def probe(
        self,
        values: list[int],
        time_limit: float | None = None,
        max_probes: int | None = None,
        deadline: float | None = None,
        only: list[int] | None = None,
    ) -> tuple[bool, dict[int, int]]:
        """Testa x[i]=1 e x[i]=0 por inviabilidade.

        Retorna (nó_viável, células_forçadas). Só "Infeasible" conta como prova:
        qualquer outro status (limite de tempo, indefinido) é tratado como
        "não sei", jamais como dedução.
        """
        self.apply(values)
        if self.solve(time_limit) == INFEASIBLE:
            return False, {}

        livres = only if only is not None else [i for i, v in enumerate(values) if v == UNKNOWN]
        if max_probes is not None:
            # gasta o orçamento primeiro nas células mais "em dúvida"
            vals = {i: (self.x[i].value() or 0.0) for i in livres}
            livres.sort(key=lambda i: -min(vals[i], 1 - vals[i]))
            livres = livres[:max_probes]

        forced: dict[int, int] = {}
        for i in livres:
            if deadline is not None and time.monotonic() > deadline:
                break
            var = self.x[i]
            var.lowBound, var.upBound = 1, 1
            if self.solve(time_limit) == INFEASIBLE:
                forced[i] = WHITE
                var.lowBound, var.upBound = 0, 1
                continue
            var.lowBound, var.upBound = 0, 0
            if self.solve(time_limit) == INFEASIBLE:
                forced[i] = BLACK
            var.lowBound, var.upBound = 0, 1
        return True, forced
