# Data Analyst — a protoAgent plugin

**Your agent, your data, your way.** Point the agent at local data — CSV, TSV, Parquet, JSON,
Excel and SQLite files, or folders of them — and it explores them with **read-only SQL** on an
embedded [DuckDB](https://duckdb.org) that queries the files **in place** (no server, no import
step), profiles them, exports results, and charts them in the console's **Artifact panel** as
live, themed Vega-Lite charts.

Charts are fast because the model writes two small things — a SQL query and a few lines of
Vega-Lite (`mark` + `encoding`, no data) — and the plugin inlines the query's rows and hands the
spec to the panel. No hand-written React component, no rows round-tripping through the chat.

> **Requires protoAgent ≥ 0.192.0** — the release that adds the `vega-lite` artifact kind and the
> `artifact.show` plugin service (ADR 0116, [protoAgent#4025](https://github.com/protoLabsAI/protoAgent/pull/4025)).
> **That core release isn't out yet**; until it is, every tool except `data_chart` works on an
> older core, and `data_chart` says why it couldn't render (and returns the spec it would have).

## Quick start

1. Install from git: *Settings ▸ Plugins ▸ Add from URL* →
   `https://github.com/protoLabsAI/data-plugin`, enable it, then **Install dependencies**
   (`duckdb`; `openpyxl` too if you'll read `.xlsx`).
2. In *Settings ▸ Plugins ▸ Data Analyst*, set **Data folders** to the folder(s) holding your
   data, e.g. `/Users/me/Data/coffee-shop`. Only the operator can set this — the agent can't.
3. Ask: *"connect ~/Data/coffee-shop — what were my best weekdays last quarter? chart it"*.

## Tools

| Tool | What it does |
|---|---|
| `data_connect(path, name="")` | Register a file or folder (≤ 3 levels, ≤ 200 files). Each file becomes a source — a SQL view; SQLite gives one per table, a workbook one per sheet. |
| `data_sources()` | The connected sources: name, kind, rows, columns, file. |
| `data_schema(source)` | Columns, types and 5 sample rows. |
| `data_query(sql)` | One read-only `SELECT` (DuckDB SQL); a compact table, row- and time-capped. |
| `data_profile(source)` | Per column: nulls, ≈distinct, min/max, mean/σ/quartiles, IQR outliers, top values. |
| `data_chart(sql, vega_lite_spec, title)` | Runs the query, inlines the rows, renders a live chart in the Artifact panel. |
| `data_export(sql, format, filename)` | Writes CSV or Parquet to `<workspace>/data-exports/` — never into a source folder. |

Skills: **exploring-a-dataset** (connect → schema/profile → SQL → report) and **building-a-chart**
(the spec patterns that look good: sorted bars, temporal lines, stacked/grouped bars, heat maps,
histograms, facets only when needed).

## Safety model

**Read-only, enforced by the engine** — not by pattern-matching SQL. Every query gets a fresh
in-memory DuckDB connection with extension auto-install/auto-load off, a private temp dir,
`allowed_paths` set to exactly the connected source files, `enable_external_access = false`, one
view per source, and then `lock_configuration = true`. Under that, DuckDB itself refuses writes
(`COPY … TO`, `EXPORT DATABASE`), `ATTACH`, `INSTALL`/`LOAD`, `SET`/`RESET`, URLs, and reads of
any other file (`read_text('…/.env')`, `glob()`, `/etc/passwd`). On top, the agent's SQL must
parse — with DuckDB's own parser — to exactly one `SELECT`. Each query is time-capped
(interrupted) and row-capped.

**The `data_dirs` fence** (operator-only: marked `spawns: true`, so the agent's `set_config`
refuses it). Empty refuses every connect. Paths are resolved (symlinks, `..`) *before* the
containment check, at connect time and again before every query; hardlinked files, credential
files and dirs (`.env`, `*.pem`, `id_rsa`, `.ssh`, `.aws`, …) and the agent's home are refused even
inside an allowed folder; a too-broad entry (`/`, your home, the agent's home or a parent) is
ignored. Point it at folders of data the agent can't write into.

**SQLite and Excel** aren't readable by the bundled DuckDB (its sqlite/excel extensions would be a
network install, which the engine refuses), so each table/sheet is **snapshotted to Parquet** in
the plugin's own cache through a private connection the agent's SQL never reaches. SQLite is
opened read-only (`mode=ro`, falling back to `immutable=1`) and never written. A snapshot refreshes
when its file's size or mtime changes.

**Writes** go only to the plugin store (the source registry + snapshots, instance-scoped via
`sdk.plugin_store`) and the workspace's `data-exports/` folder; an export whose destination is
inside any configured data folder is refused.

## Settings

| Key | Default | |
|---|---|---|
| `data_dirs` | `""` | Allowlisted data folders (operator-only). |
| `row_cap` | 200 | Rows a `data_query` reply shows. |
| `chart_row_cap` | 5000 | Rows a chart may carry (aggregate in SQL past this). |
| `timeout_s` | 20 | Per-query time cap. |
| `memory_limit` | `1GB` | DuckDB memory per query (spills to the plugin's temp dir). |
| `export_row_cap` | 1000000 | Rows `data_export` writes. |

Settings apply on the next call — no restart.

## Development

```bash
uv venv && uv pip install -r requirements-dev.txt ruff
ruff check . && ruff format --check . && pytest -q
```

The suite is host-free (the plugin loads under a synthetic package; `graph.sdk` is faked where a
test needs it) and generates its fixture CSV/TSV/Parquet/JSON/SQLite/XLSX files at test time.

## Third-party

| Package | Licence | Use |
|---|---|---|
| [duckdb](https://github.com/duckdb/duckdb) | MIT | the query engine (required) |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | MIT | reading `.xlsx` (optional) |

`tests/test_plugin.py` fails if a declared dependency's licence leaves the permissive allowlist.
Charts render with core's vendored Vega / Vega-Lite / vega-embed (BSD-3-Clause), shipped by the
Artifact plugin — nothing is bundled here.

## Licence

MIT — see [LICENSE](LICENSE).
