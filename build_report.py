"""Gera a tabela comparativa a partir de results/full_results.jsonl.

Produz:
  results/tabela_completa.csv          (uma linha por execução)
  results/resumo.json                  (agregado por método e por porte)
  results/benchmark_tapa_completo.xlsx (4 abas, com gráficos)

Uso:  python build_report.py [caminho_do_jsonl]
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ENTRADA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/full_results.jsonl")
SAIDA = ENTRADA.parent
METODOS = ["deterministico", "lp-first", "lp-frac", "lp-patterns"]
NOME = {
    "deterministico": "Determinístico",
    "lp-first": "PL + 1ª célula",
    "lp-frac": "PL + mais fracionária",
    "lp-patterns": "PL + mais restringida",
}
ORDEM = {m: i for i, m in enumerate(METODOS)}
CAMPOS = ["Método", "Tabuleiro", "Fonte", "Dimensão", "Células", "Status", "Gabarito",
          "Nós", "Backtracks", "Decisões", "Chamadas PL", "Tempo (s)", "Tempo solver (s)", "Mensagem"]


def porte(r: dict) -> str:
    """Mesma classificação do bench_full: usa a maior dimensão do tabuleiro."""
    width, height = r.get("width"), r.get("height")
    dim = max(width, height) if width and height else int((r.get("area") or 0) ** 0.5)
    return "pequeno" if dim <= 8 else ("médio" if dim <= 10 else ("grande" if dim <= 15 else "extragrande"))


def _dimensao(r: dict) -> str:
    width, height = r.get("width"), r.get("height")
    return f"{width}x{height}" if width and height else "—"


def linha(r: dict) -> list:
    g = r.get("gabarito")
    solver = r.get("solver", "?")
    return [NOME.get(solver, solver), r.get("board", "?"), r.get("source", ""),
            _dimensao(r), r.get("area", ""), r.get("status", "?"),
            "confere" if g is True else ("diferente" if g is False else "—"),
            r.get("nodes"), r.get("backtracks"), r.get("decisions"), r.get("lpSolves"),
            r.get("elapsed"), r.get("solverElapsed"), (r.get("message") or r.get("error") or "")]


def main() -> None:
    if not ENTRADA.exists():
        sys.exit(f"não encontrei {ENTRADA}. Rode primeiro: python bench_full.py")
    rows = [json.loads(l) for l in ENTRADA.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        sys.exit(f"{ENTRADA} está vazio")
    rows.sort(key=lambda r: (r["board"], ORDEM.get(r["solver"], 9)))

    with (SAIDA / "tabela_completa.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CAMPOS)
        for r in rows:
            w.writerow(linha(r))

    por_metodo = []
    for m in METODOS:
        rs = [r for r in rows if r["solver"] == m]
        if not rs:
            continue
        ok = [r for r in rs if r["status"] == "resolvido"]
        por_metodo.append({
            "Método": NOME[m], "Execuções": len(rs), "Resolvidos": len(ok),
            "Taxa": f"{100 * len(ok) / len(rs):.1f}%",
            "Tempo total (s)": round(sum(r["elapsed"] or 0 for r in rs), 1),
            "Nós (soma)": sum(r["nodes"] or 0 for r in rs),
            "Backtracks (soma)": sum(r["backtracks"] or 0 for r in rs),
            "Chamadas PL": sum(r["lpSolves"] or 0 for r in rs),
            "Nós médios (ok)": round(sum(r["nodes"] for r in ok) / len(ok), 1) if ok else 0,
        })

    portes = ["pequeno", "médio", "grande", "extragrande"]
    por_porte = []
    for m in METODOS:
        rs_m = [r for r in rows if r["solver"] == m]
        if not rs_m:
            continue
        linha_p = {"Método": NOME[m]}
        for p in portes:
            rs = [r for r in rs_m if r.get("width") and r.get("height") and porte(r) == p
                  and r.get("status") != "n/a (teto PL)"]
            ok = [r for r in rs if r["status"] == "resolvido"]
            linha_p[p] = f"{len(ok)}/{len(rs)}" if rs else "—"
        por_porte.append(linha_p)

    (SAIDA / "resumo.json").write_text(
        json.dumps({"por_metodo": por_metodo, "por_porte": por_porte, "total": len(rows)},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"execuções: {len(rows)} | tabuleiros: {len({r['board'] for r in rows})}\n")
    larg = max(len(r["Método"]) for r in por_metodo) + 2
    print(f"{'Método':<{larg}}{'Resolvidos':>11}{'Taxa':>8}{'Tempo (s)':>11}{'Nós':>10}{'Backtr':>9}{'PLs':>9}")
    for r in por_metodo:
        print(f"{r['Método']:<{larg}}{r['Resolvidos']:>11}{r['Taxa']:>8}"
              f"{r['Tempo total (s)']:>11}{r['Nós (soma)']:>10}{r['Backtracks (soma)']:>9}{r['Chamadas PL']:>9}")
    print()
    for r in por_porte:
        print(r)

    _xlsx(rows, por_metodo, por_porte, SAIDA)
    print(f"\n-> {SAIDA/'tabela_completa.csv'}\n-> {SAIDA/'resumo.json'}\n-> {SAIDA/'benchmark_tapa_completo.xlsx'}")


def _xlsx(rows, por_metodo, por_porte, saida: Path) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("(openpyxl não instalado — pulei o .xlsx; pip install openpyxl)")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Tabela completa"
    ws.append(CAMPOS)
    for r in rows:
        ws.append(linha(r))
    cabe = PatternFill("solid", fgColor="1F2937")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = cabe
        c.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    for i, largura in enumerate([24, 26, 12, 9, 8, 14, 11, 10, 12, 10, 13, 11, 17, 34], start=1):
        ws.column_dimensions[get_column_letter(i)].width = largura
    amarelo = PatternFill("solid", fgColor="FEF3C7")
    for row in ws.iter_rows(min_row=2):
        if row[5].value != "resolvido":
            for c in row:
                c.fill = amarelo

    w2 = wb.create_sheet("Resumo por método")
    cab = list(por_metodo[0].keys())
    w2.append(cab)
    for r in por_metodo:
        w2.append([r[k] for k in cab])
    for c in w2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = cabe
    w2.freeze_panes = "A2"
    for i in range(1, len(cab) + 1):
        w2.column_dimensions[get_column_letter(i)].width = 17

    w3 = wb.create_sheet("Resolvidos por porte")
    cab3 = list(por_porte[0].keys())
    w3.append(cab3)
    for r in por_porte:
        w3.append([r[k] for k in cab3])
    for c in w3[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = cabe
    for i in range(1, len(cab3) + 1):
        w3.column_dimensions[get_column_letter(i)].width = 24

    w4 = wb.create_sheet("Gráfico")
    w4.append(["Método", "Tempo total (s)", "Nós (soma)", "Chamadas PL"])
    for r in por_metodo:
        w4.append([r["Método"], r["Tempo total (s)"], r["Nós (soma)"], r["Chamadas PL"]])
    for i in range(1, 5):
        w4.column_dimensions[get_column_letter(i)].width = 26
    pos = {2: "F2", 3: "F18", 4: "F34"}
    titulos = {2: "Tempo total (s) por método", 3: "Nós por método", 4: "Chamadas de PL por método"}
    for col, ancora in pos.items():
        ch = BarChart()
        ch.type = "col"
        ch.title = titulos[col]
        ch.height, ch.width = 7.5, 15
        ch.add_data(Reference(w4, min_col=col, min_row=1, max_row=len(por_metodo) + 1), titles_from_data=True)
        ch.set_categories(Reference(w4, min_col=1, min_row=2, max_row=len(por_metodo) + 1))
        ch.legend = None
        w4.add_chart(ch, ancora)

    wb.save(saida / "benchmark_tapa_completo.xlsx")


if __name__ == "__main__":
    main()
