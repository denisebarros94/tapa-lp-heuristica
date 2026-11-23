"""Benchmark completo: todos os tabuleiros do dataset × todos os métodos.

Uso:
    python bench_full.py --timeout 6 --node-limit 50000 --workers 2
    python bench_full.py --pilot          # amostra estratificada, para estimar

Saída: results/full_results.jsonl (uma linha por execução) + resumo no terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

FRONT = Path(os.getenv("TAPA_FRONTEND_DIR", Path(__file__).resolve().parent.parent / "Tapa-Puzzle-Frontend"))
SOLVERS = ["deterministico", "lp-first", "lp-frac", "lp-patterns"]
MAX_LP_CELLS = 400


def carregar_tabuleiros() -> list[dict]:
    boards = []
    for nome, fonte in (("boards.json", "ppbench"), ("boards-gridpuzzle.json", "gridpuzzle")):
        dados = json.loads((FRONT / "assets" / nome).read_text(encoding="utf-8"))
        for b in dados:
            b["source"] = fonte
            boards.append(b)
    return boards


def _trabalho(tarefa: dict) -> dict:
    """Roda um (tabuleiro, método). Executa em processo separado."""
    b_, args_s = tarefa["board"], tarefa["solver_spec"]
    from tapa_solver_deterministico.model import BLACK
    from tapa_solver_deterministico.verifier import verify
    from tapa_lp.instrumented import solve_com_progresso
    from tapa_lp.solver_lp import LPConfig, solve_with_lp
    from tapa_lp.jobs import parse_cells

    t0 = time.time()
    base = dict(board=b_["id"], source=b_["source"], width=b_["width"],
                height=b_["height"], area=b_["width"] * b_["height"],
                solver=args_s["solver"])
    try:
        puzzle, _ = parse_cells(b_["width"], b_["height"], b_["cells"])
    except Exception as exc:
        return {**base, "status": "erro-entrada", "error": str(exc), "elapsed": 0.0,
                "nodes": None, "backtracks": None, "lpSolves": None,
                "gabarito": None, "verified": False}

    area = b_["width"] * b_["height"]
    lp = args_s["solver"].startswith("lp-")
    if lp and area > MAX_LP_CELLS:
        return {**base, "status": "n/a (teto PL)", "error": f"{area} células > {MAX_LP_CELLS}",
                "elapsed": 0.0, "nodes": None, "backtracks": None, "lpSolves": None,
                "gabarito": None, "verified": False}

    if lp:
        cfg = LPConfig(use_probe=True,
                       branch={"lp-first": "first", "lp-frac": "most_fractional",
                               "lp-patterns": "fewest_patterns"}[args_s["solver"]],
                       value_order=("black_first" if args_s["solver"] == "lp-first" else "lp_guided"),
                       probe_depth=2, lp_time_limit=2.0,
                       lp_backend=args_s.get("lp_backend", "auto"))
        r = solve_with_lp(puzzle, cfg, timeout_seconds=args_s["timeout"], node_limit=args_s["node_limit"])
        board, solved, msg, s = r.board, r.solved, r.message, r.stats
        lp_solves = s.lp_solves
    else:
        board, s, msg = solve_com_progresso(puzzle, timeout_seconds=args_s["timeout"],
                                            node_limit=args_s["node_limit"])
        solved, lp_solves = board is not None, 0

    verified = bool(board is not None and verify(board).valid)
    gabarito = None
    if b_.get("solution") and board is not None and len(b_["solution"]) == len(board.values):
        gabarito = all((1 if v == BLACK else 0) == int(sv) for v, sv in zip(board.values, b_["solution"]))

    status = "resolvido" if solved else ("timeout" if s.timed_out else
                                         "limite-nós" if s.node_limit_reached else "sem-solução")
    if not solved and not s.timed_out and not s.node_limit_reached:
        status = "sem-solução"
    return {**base, "status": status, "verified": verified, "gabarito": gabarito,
            "nodes": s.nodes, "backtracks": s.backtracks, "decisions": s.decisions,
            "lpSolves": lp_solves, "elapsed": round(time.time() - t0, 3),
            "solverElapsed": round(s.elapsed_seconds, 3), "message": msg, "error": None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--node-limit", type=int, default=50_000)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--deadline", type=float, default=0, help="segundos de orçamento total")
    ap.add_argument("--solver", action="append", choices=SOLVERS, help="restringe os métodos (pode repetir)")
    ap.add_argument("--lp-backend", choices=["auto", "highs", "pulp"], default="auto",
                    help="backend da relaxação: highspy persistente (auto/highs) ou PuLP (pulp)")
    ap.add_argument("--porte", action="append", choices=["pequeno", "médio", "grande", "extragrande"])
    ap.add_argument("--out", default="results/full_results.jsonl")
    a = ap.parse_args()

    boards = carregar_tabuleiros()
    if a.porte:
        def _porte(b):
            d = max(b["width"], b["height"])
            return "pequeno" if d <= 8 else ("médio" if d <= 10 else ("grande" if d <= 15 else "extragrande"))
        boards = [b for b in boards if _porte(b) in a.porte]
    if a.solver:
        SOLVERS[:] = a.solver
    if a.pilot:
        por_porte: dict[int, list] = {}
        for b in boards:
            por_porte.setdefault(b["width"] * b["height"], []).append(b)
        sel = []
        for area in sorted(por_porte):
            sel += por_porte[area][:2]
        boards = sel
    if a.limit:
        boards = boards[: a.limit]

    tarefas = [{"board": b, "solver_spec": {"solver": s, "timeout": a.timeout,
                                            "node_limit": a.node_limit,
                                            "lp_backend": a.lp_backend}}
               for b in boards for s in SOLVERS]
    # Mais caros primeiro: reduz a cauda quando os últimos workers ficam ociosos.
    tarefas.sort(key=lambda t: -(t["board"]["width"] * t["board"]["height"]))
    print(f"tabuleiros={len(boards)} métodos={len(SOLVERS)} execuções={len(tarefas)} "
          f"backend={a.lp_backend}", flush=True)

    Path("results").mkdir(exist_ok=True)
    saida = Path(a.out)
    if saida.exists():
        saida.unlink()
    feito = 0
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futuros = {ex.submit(_trabalho, t): t for t in tarefas}
        with saida.open("a", encoding="utf-8") as f:
            for fut in as_completed(futuros):
                if a.deadline and time.time() - t0 > a.deadline:
                    for restante in futuros:
                        restante.cancel()
                    print("orçamento esgotado — gravando resultados parciais", flush=True)
                    break
                try:
                    r = fut.result()
                except Exception as exc:
                    b_ = futuros[fut]["board"]
                    r = {"board": b_["id"], "source": b_.get("source"),
                         "width": b_["width"], "height": b_["height"],
                         "area": b_["width"] * b_["height"],
                         "solver": futuros[fut]["solver_spec"]["solver"],
                         "status": "erro-interno", "error": repr(exc),
                         "nodes": None, "backtracks": None, "lpSolves": None,
                         "gabarito": None, "verified": False, "elapsed": None}
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                feito += 1
                if feito % 100 == 0:
                    dt = time.time() - t0
                    print(f"  {feito}/{len(tarefas)}  {dt:.0f}s  eta {dt/feito*(len(tarefas)-feito):.0f}s", flush=True)
    print(f"concluído em {time.time()-t0:.0f}s -> {saida}")
