# Tapa — resolvedor determinístico + heurística de Programação Linear

Projeto que **mantém intacto** o motor determinístico do Tapa
(`tapa_solver_deterministico`: propagação de regras + backtracking em ordem
fixa) e acrescenta, ao lado, uma **heurística de Programação Linear (PL)** que
decide células antes de o branch-and-bound abrir a primeira ramificação.

Nada do pacote original foi alterado. A heurística vive em `src/tapa_lp/` e
reaproveita a mesma tabela de padrões do motor original — o que garante que
propagação e PL nunca discordem sobre o significado de uma pista.

## Estrutura

```
tapa-lp-heuristica/
├── README.md
├── pyproject.toml
├── requirements.txt
├── docs/
│   ├── LEIAME-original.md          # README do projeto original (referência)
│   └── pyproject-original.toml
├── examples/
│   ├── 4x4_fixture.txt             # tabuleiro do teste original
│   ├── 6x6_01.txt
│   ├── 8x8_01.txt
│   └── 10x10_01.txt
├── src/
│   ├── tapa_solver_deterministico/ # ORIGINAL, sem alterações
│   │   ├── api.py  model.py  patterns.py
│   │   ├── propagation.py  solver.py  verifier.py
│   └── tapa_lp/                    # NOVO: heurística de PL
│       ├── lp_model.py             # relaxação linear (PuLP/CBC, fallback)
│       ├── highs_backend.py        # relaxação persistente (highspy, opcional)
│       ├── solver_lp.py            # busca com PL integrada
│       ├── io_puzzles.py           # leitura/escrita de tabuleiros .txt
│       ├── uniqueness.py           # contagem de soluções
│       ├── instrumented.py         # motor determinístico com progresso
│       ├── jobs.py  runs.py        # API web: jobs e histórico
│       ├── server.py               # frontend + API na mesma porta
│       └── cli.py                  # comandos solve, bench e check
└── tests/
    ├── test_api.py                 # teste original da API
    └── test_lp.py                  # correção das deduções da PL
```

## Instalação

Requer **Python 3.11+**.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

O solver CBC vem embutido no pacote `pulp`; não é preciso instalar nada além.

Para o backend rápido de PL (recomendado), instale o extra `highs`:

```bash
python -m pip install -e ".[dev,highs]"
```

Sem `highspy`, o projeto continua funcionando com o backend PuLP/CBC.

## Backend de PL: PuLP/CBC vs highspy persistente

O PuLP reescreve o modelo e reinicia o solver **a cada resolução** (~6–10 ms
neste projeto). O `highs_backend.py` monta o modelo **uma vez** no `highspy` e,
em cada nó, troca apenas os bounds — o mesmo princípio do Cap. 8.3 do manual.
O custo cai para ~0,1–0,2 ms por resolução (**~30–50×**), mantendo exatamente
as mesmas restrições e a mesma regra de dedução (só `Infeasible` prova).

O backend é escolhido por `LPConfig(lp_backend=...)`:

| valor | comportamento |
|---|---|
| `"auto"` (padrão) | highspy se estiver instalado; senão PuLP |
| `"highs"` | força highspy (se instalado) |
| `"pulp"` | mantém o backend antigo (CBC, ou HiGHS do PuLP) |

`connectivity_lp=True` sempre usa o PuLP, porque o backend highspy implementa
só o modelo sem fluxo (a configuração padrão).

### Medição no piloto (89 tabuleiros × 3 métodos, 3 s, 4 workers)

| backend | resolvidos | tempo de parede |
|---|---|---|
| PuLP/CBC | 121/267 (45,3%) | 127 s |
| **highspy persistente** | **185/267 (69,3%)** | **72 s (-43%)** |

Mesma máquina, mesmos tabuleiros e mesmo orçamento por execução. O ganho não
vem de procurar mais nós "no escuro": o teste de equivalência
(`test_backend_highs_concorda_com_o_pulp`) confirma **as mesmas deduções** nos
exemplos; o que muda é o custo por dedução.

## Otimizações que reduzem o tempo (medidas)

Um benchmark de 833 tabuleiros mostrou que a PL corta nós (−96%) mas paga caro
por nó. Aplicando quatro otimizações — **nenhuma altera a correção**, todas
seguem deduzindo só por inviabilidade:

| # | Otimização | O que muda |
|---|---|---|
| 1 | **Uma PL por nó, não duas** | `choose_branch` chamava `values()` e de novo no `value_order`; agora resolve uma vez e reusa |
| 2 | **Saída integral** | se a PL do nó raiz já é 0/1 e passa no verificador, devolve direto — sem abrir busca |
| 3 | **Probing só em células de anel de pista** | `probe_scope="near_clue"`: células longe de qualquer pista raramente são forçadas; cada uma custava 2 PLs |
| 4 | **Conectividade fora do LP** | `connectivity_lp=False`: o bloco de fluxo é o mais caro do modelo (2 arcos × 2 restrições + n conservações) e é a parte mais frouxa da relaxação |

### Medição (180 tabuleiros GridPuzzle, orçamento de 3 s, mesma máquina)

| Variante | Resolvidos | Gabarito confere | Tempo total | Nós | Chamadas PL |
|---|---|---|---|---|---|
| A — base | 170/180 | 170 | 129.0 s | 699 | 8363 |
| C — 1+2+3 | 173/180 | 173 | 112.7 s (-13%) | 679 | 7261 |
| **D — 1+2+3+4** | **180/180** | **180** | **56.3 s (-56%)** | 799 | 7894 |

A variante D é a configuração padrão recomendada: mesma taxa de resolução,
**-56% de tempo** e as mesmas soluções validadas.

**Esta é a configuração padrão** desde a v0.4.0 (tanto em `LPConfig()` quanto
nos três solvers servidos pela API web). Para voltar ao comportamento antigo:

```python
LPConfig(probe_scope="all", integral_exit=False, connectivity_lp=True)
```

Para repetir a medição: `python compare_otim.py` (comparação de variantes) —
os resultados ficam em `results/comparacao_otimizacoes.json`.

## Benchmark no dataset completo (833 tabuleiros)

Reproduz a comparação dos 4 métodos em todos os tabuleiros do dataset
Pencil Puzzle Bench (653, já decodificados em `assets/boards.json` do frontend)
mais os 180 do GridPuzzle (com gabarito) — **3.332 execuções**.

### 1. Tenha as duas pastas lado a lado

```
pasta/
├── Tapa-Puzzle-Frontend/   # fornece os tabuleiros (assets/*.json)
└── tapa-lp-heuristica/     # roda o benchmark
```

Se o front estiver em outro lugar:
`$env:TAPA_FRONTEND_DIR="C:\caminho\Tapa-Puzzle-Frontend"` (Windows) ou
`export TAPA_FRONTEND_DIR=/caminho/Tapa-Puzzle-Frontend`.

### 2. Teste rápido primeiro (2 tabuleiros, ~10 s)

```bash
.venv/bin/python bench_full.py --limit 2 --timeout 5 --out /tmp/teste.jsonl
.venv/bin/python build_report.py /tmp/teste.jsonl
```

### 3. Rodada completa

```bash
.venv/bin/python bench_full.py --timeout 3 --node-limit 20000 --workers 4
.venv/bin/python build_report.py
```

Tempo medido: **~45 min** com 2 núcleos e orçamento de 3 s por execução. Com o
backend highspy e 4 workers, o mesmo conjunto fica bem mais rápido; o número
exato depende de quantos tabuleiros cabem no orçamento de 3 s.

| Opção | Para quê |
|---|---|
| `--timeout 3` | segundos por execução (o orçamento que define o resultado) |
| `--node-limit 20000` | teto de nós por execução |
| `--workers 4` | processos em paralelo (use o nº de núcleos físicos) |
| `--lp-backend auto` | `auto`/`highs` usam highspy persistente; `pulp` usa o backend antigo |
| `--pilot` | amostra estratificada (2 por tamanho, ~2 min com highspy) para estimar antes |
| `--porte médio --porte grande` | restringe por porte |
| `--solver lp-patterns` | roda um método só |
| `--deadline 3000` | para tudo e grava o que já saiu |
| `--out arquivo.jsonl` | onde gravar (padrão `results/full_results.jsonl`) |

### 4. Saídas

| Arquivo | Conteúdo |
|---|---|
| `results/tabela_completa.csv` | uma linha por execução: Método, Tabuleiro, Fonte, Dimensão, Células, Status, Gabarito, Nós, Backtracks, Decisões, Chamadas PL, Tempo, Tempo solver, Mensagem |
| `results/resumo.json` | agregado por método e por porte |
| `results/benchmark_tapa_completo.xlsx` | 4 abas (tabela filtrável, resumo, porte, gráficos) |

O `.xlsx` só é gerado se `openpyxl` estiver instalado (`pip install openpyxl`).

### 5. Conferir a origem dos tabuleiros (opcional)

```bash
.venv/bin/python scripts/extract_tapa.py --check
```

Baixa o `full_dataset.jsonl` (663 MB) do HuggingFace **em streaming**, sem
gravar em disco, e conta os `"pid":"tapa"` — deve dar 653. Ressalva honesta:
esse arquivo **não** contém pistas nem grade de solução (a solução vem como
sequência de cliques), por isso o benchmark usa os JSONs já decodificados do
frontend, que saíram do mesmo dataset.

## Uso com o frontend (plataforma completa)

O backend serve o front `Tapa-Puzzle-Frontend` e a API na mesma porta:

```bash
.venv/bin/tapa-web          # http://127.0.0.1:8000
```

Detalhes em `docs/INTEGRACAO-FRONTEND.md`. Sem o front ao lado, o servidor
sobe só a API e avisa no terminal.

## Uso (linha de comando)

### Resolver um tabuleiro

```bash
python -m tapa_lp.cli solve examples/8x8_01.txt --strategy lp-patterns
```

Estratégias disponíveis:

| `--strategy` | O que faz |
|---|---|
| `deterministico` | motor original: propagação + primeira célula em ordem linear |
| `lp-first` | PL só para deduções; ramifica na primeira célula desconhecida |
| `lp-frac` | PL para deduções; ramifica na célula mais fracionária |
| `lp-patterns` | PL para deduções; ramifica na célula mais restringida |

### Comparar todas as estratégias

```bash
python -m tapa_lp.cli bench examples
```

### Conferir se o tabuleiro tem solução única

```bash
python -m tapa_lp.cli check examples/10x10_01.txt
```

Saída: quantas soluções foram encontradas (até `--max`, padrão 2). Isso importa
porque **um tabuleiro ambíguo admite mais de uma solução válida** — e aí dois
motores podem devolver tabuleiros diferentes, ambos corretos.

### Subir a API original (porta 8002)

```bash
tapa-solver-api
# documentação: http://127.0.0.1:8002/docs
```

### Rodar os testes

```bash
pytest -q
```

## Formato dos tabuleiros

```
# nome opcional
8 8
. . . 1,2 . . . .
. . . . . 3 . .
...
```

- `.` — célula sem pista
- `1,2` — pista com dois grupos (1 preta isolada + 2 pretas juntas)
- `?` — curinga (tamanho de bloco desconhecido)

## Como a heurística funciona

### 1. O modelo de PL (`lp_model.py`)

| Bloco | Restrição |
|---|---|
| escolha de padrão | `Σ_p z[c,p] = 1` para cada pista `c` |
| elo padrão↔célula | `x[nb] = Σ_{p ∋ nb} z[c,p]` para cada vizinho do anel |
| pista é branca | `x[c] = 0` |
| sem 2×2 preto | `Σ x` de cada bloco 2×2 `≤ 3` |
| conectividade | uma raiz (`Σ y = 1`), fluxo `f ≤ n·x` nas duas pontas, `src ≤ n·y`, conservação `Σ_in f + src − Σ_out f = x` |

Toda solução inteira válida do Tapa satisfaz essas restrições. Logo a
relaxação **contém** o conjunto viável inteiro — e é essa inclusão que
autoriza a dedução.

O modelo é montado **uma vez por tabuleiro**; cada nó da busca apenas troca os
limites (`bounds`) das variáveis conforme as células já decididas.

### 2. Dedução por inviabilidade (probing)

Para cada célula desconhecida `i`:

1. force `x[i] = 1` e resolva a PL. Se der **inviável**, `i` é **branca**.
2. force `x[i] = 0` e resolva. Se der **inviável**, `i` é **preta**.

Só o status `Infeasible` conta como prova. `Optimal`, limite de tempo ou
status indefinido significam "não sei" — nunca viram dedução.

Isso é **propagação de restrições cujo propagador é uma PL**: 2×2, canto,
anel forçado, conectividade — tudo cai junto, porque tudo está no modelo.

### 3. Escolha da célula de decisão

- `first` — primeira célula desconhecida em ordem linear (comportamento original)
- `most_fractional` — célula cujo valor na PL está mais perto de 0,5
- `fewest_patterns` — célula com menos padrões compatíveis nas pistas que a veem

### 4. Ordem dos valores

Com `value_order="lp_guided"`, tenta primeiro a cor sugerida pela PL
(`x ≥ 0,5` → preta). Isso reduz drasticamente os retrocessos.

### 5. Controle de custo

`probe_depth` limita até que profundidade a PL é usada (padrão: 2). Como cada
nó custa dezenas de resoluções de PL, limitar a profundidade evita que o tempo
por nó coma o ganho em nós.

## Resultados medidos (nesta máquina, CBC, 1 núcleo)

| tabuleiro | estratégia | nós | retrocessos | PLs | segundos |
|---|---|---|---|---|---|
| 8x8_01 | deterministico | 86 | 72 | 0 | 0,115 |
| 8x8_01 | lp-patterns | **7** | **0** | 149 | 1,586 |
| 10x10_01 | deterministico | 484 | 451 | 0 | 0,541 |
| 10x10_01 | lp-first | 280 | 252 | 500 | 7,836 |
| 10x10_01 | lp-frac | 118 | 86 | 664 | 9,912 |
| 10x10_01 | lp-patterns | **29** | **0** | 546 | 8,132 |

**Leitura honesta:** a PL corta nós de forma expressiva (484 → 29 no 10×10,
86 → 7 no 8×8), mas **custa mais tempo de parede** nesses tamanhos, porque cada
nó resolve centenas de PLs e o CBC tem custo fixo por resolução. Em tabuleiros
pequenos (4×4, 6×6) a PL é puro prejuízo. O ganho de tempo só aparece quando o
número de nós domina o custo — tabuleiros maiores ou buscas com mais
retrocessos.

## Unicidade dos exemplos

`count_solutions` (em `src/tapa_lp/uniqueness.py`) resolve o modelo **binário**
e bloqueia cada solução encontrada com um corte *no-good*, enumerando até um
limite. Estado real dos arquivos:

| arquivo | pistas | soluções | veredito |
|---|---|---|---|
| `4x4_fixture.txt` | 6 | 1 | único |
| `6x6_01.txt` | 5 | 1 | único |
| `8x8_01.txt` | 11 | 2 | ambíguo |
| `10x10_01.txt` | 13 | 2 | ambíguo |

`8x8_01` e `10x10_01` são **ambíguos de propósito**: foram mantidos no
benchmark porque servem para testar justamente o caso em que dois motores
podem terminar em soluções diferentes. Os testes refletem isso:

- `test_motores_concordam_quando_a_solucao_e_unica` — igualdade exata só nos
  arquivos de solução única;
- `test_deducoes_da_pl_sao_corretas_em_todas_as_solucoes` — dedução por
  inviabilidade é forçada em **todas** as soluções, então vale comparar com
  qualquer uma;
- `test_contagem_de_solucoes_bate_com_o_esperado` — trava a contagem e valida
  cada solução encontrada.

## Limitações

- A relaxação é **frouxa na conectividade** (fluxo com big-M `n`).
- `most_fractional` **não** prova nada: valor 0,5 é sintoma, não veredito. A
  dedução válida é sempre por inviabilidade.
- O probing gasta 2 PLs por célula por rodada; `max_probes_per_node` limita isso.
- O backend highspy implementa o modelo **sem** fluxo de conectividade. Se
  `connectivity_lp=True`, o código cai automaticamente no PuLP.
- O scheduler de threads do HiGHS é global; o backend não fixa `threads` para
  não conflitar com modelos criados por outros backends no mesmo processo.
- Os tabuleiros em `examples/` foram gerados e validados para o benchmark —
  não são tabuleiros publicados.
