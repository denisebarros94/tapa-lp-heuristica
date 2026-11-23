# Rodar a plataforma completa (front + solvers)

## Estrutura esperada

```
pasta/
├── Tapa-Puzzle-Frontend/
└── tapa-lp-heuristica/
```

## Instalar

```bash
cd tapa-lp-heuristica
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,highs]"   # Windows: py -m venv .venv  /  .venv\Scripts\python.exe -m pip install -e ".[dev,highs]"
```

O extra `highs` instala o `highspy`, usado pelo backend de PL persistente. Sem
ele, a API continua funcionando com o backend PuLP/CBC (mais lento).

## Subir

```bash
.venv/bin/tapa-web                     # Windows: .venv\Scripts\tapa-web.exe
```

Abre `http://127.0.0.1:8000` com front + API na mesma origem.

## Variáveis de ambiente

| Variável | Padrão | Para quê |
|---|---|---|
| `TAPA_PORT` | 8000 | porta do servidor |
| `TAPA_FRONTEND_DIR` | `../Tapa-Puzzle-Frontend` | pasta do front |
| `TAPA_MAX_LP_CELLS` | 400 | teto de células para os métodos com PL |
| `TAPA_DATA_DIR` | `./data` | onde fica `runs.json` |

## Alternativa: só a API (porta 8002)

```bash
tapa-solver-api      # /docs em http://127.0.0.1:8002/docs
```

Aí o front precisa rodar separado (`python3 -m http.server 8000` dentro de
`Tapa-Puzzle-Frontend`) — o `assets/api-config.js` já tenta `:8002` como segunda
origem e o CORS do servidor aceita qualquer origem local.
