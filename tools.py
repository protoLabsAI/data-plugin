"""The seven agent tools: connect, sources, schema, query, profile, chart, export.

Every reply is compact and model-facing — a markdown table and a line of context — because the
whole point is speed: the model writes a little SQL (and, for a chart, a small Vega-Lite spec
with NO data), the plugin does the heavy lifting, and the rows never round-trip through the
conversation more than once.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from langchain_core.tools import tool

from . import engine, fence, paths, settings, sources

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
_VIEW_KEYS = ("mark", "layer", "facet", "hconcat", "vconcat", "concat", "repeat", "spec")
MAX_SPEC_BYTES = 480 * 1024  # under the Artifact panel's default 512 KB per-version cap


def _caps() -> tuple[int, float]:
    return settings.int_setting("row_cap", 1, 10_000), float(settings.int_setting("timeout_s", 1, 600))


def _notes(notes: list[str]) -> str:
    return ("\n\n" + "\n".join(f"- {n}" for n in notes[:10])) if notes else ""


def _no_sources(notes: list[str]) -> str:
    """Nothing connected yet — and WHERE the operator said the data lives, so a question like
    "what were my best weekdays?" can go straight to data_connect instead of asking for a path."""
    allowed, root_notes = fence.roots(settings.cfg().get("data_dirs"))
    if allowed:
        where = (
            " The operator's allowlisted data folders — connect one (or a file inside it): "
            + ", ".join(f"`{r}`" for r in allowed)
            + "."
        )
    else:
        where = f" No data folders are allowlisted yet — the operator sets them in {fence.SETTINGS_HINT}."
    return "No usable data sources — connect one with data_connect(path)." + where + _notes(notes + root_notes)


@tool
def data_connect(path: str, name: str = "") -> str:
    """Connect a local data FILE or FOLDER so it can be queried with SQL.

    Supported: .csv .tsv .parquet .json/.jsonl/.ndjson .xlsx (one source per sheet) and SQLite
    .sqlite/.sqlite3/.db (one source per table). A folder is walked up to 3 levels deep (≤ 200
    files); each file becomes a named source — a SQL view you can SELECT from. ``name`` names a
    single-file source (or prefixes a folder's). Only files inside the operator's allowlisted
    data folders can be connected; credential files and the agent's home never can.
    Re-connecting the same file refreshes it. Next: data_schema(source) or data_profile(source).
    """
    allowed, root_notes = fence.roots(settings.cfg().get("data_dirs"))
    if not allowed:
        return (
            "No data folders are allowlisted, so nothing can be connected. Ask the operator to add the folder "
            f"that holds this data in {fence.SETTINGS_HINT} (operator-only — the agent can't set it)."
            + _notes(root_notes)
        )
    try:
        files, skipped = sources.discover(path, allowed)
    except engine.QueryError as e:
        return str(e) + _notes(root_notes)
    if not files:
        return f"No supported data files under {path!r}." + _notes(skipped)
    srcs = sources.load()
    by_key = {(s["path"], s.get("table")): n for n, s in srcs.items()}
    prefix = sources.sanitize(name) if name.strip() else ""
    added: list[str] = []
    problems: list[str] = list(skipped)
    entries: list[dict] = []
    for f in files:
        try:
            entries.extend(sources.entries_for(f))
        except engine.QueryError as e:
            problems.append(str(e))
    single = len(entries) == 1
    for entry in entries:
        key = (entry["path"], entry.get("table"))
        if key in by_key:
            nm = by_key[key]
        else:
            base = sources.default_name(entry, prefix if (single or not prefix) else "", single)
            if prefix and not single:
                base = sources.sanitize(f"{prefix}_{base}")
            nm = sources._unique(base, set(srcs))
        rec = {k: v for k, v in entry.items()}
        if entry["kind"] in sources.SNAPSHOT_KINDS:
            try:
                rec = sources.snapshot(rec)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{Path(entry['path']).name}:{entry.get('table')}: {engine._first_line(e)}")
                continue
        srcs[nm] = rec
        by_key[key] = nm
        added.append(nm)
    sources.save(srcs)
    # Shape check: open every new source once, so a file DuckDB can't parse is reported now.
    ok, notes = sources.usable({n: srcs[n] for n in added})
    rows = []
    timeout = _caps()[1]
    for s in ok:
        try:
            r = engine.run_query([s], f"SELECT count(*) FROM {engine.ident(s['name'])}", cap=1, timeout_s=timeout)
            n_rows = r.rows[0][0]
            d = engine.run_query([s], f"SELECT * FROM {engine.ident(s['name'])} LIMIT 0", cap=1, timeout_s=timeout)
            srcs[s["name"]]["rows"] = n_rows
            srcs[s["name"]]["columns"] = len(d.columns)
            rows.append((s["name"], s["kind"], n_rows, len(d.columns), _short(s["path"], s.get("table"))))
        except engine.QueryError as e:
            problems.append(f"`{s['name']}` couldn't be read: {e}")
            srcs.pop(s["name"], None)
    sources.save(srcs)
    if not rows:
        return "Nothing connected." + _notes(problems + notes)
    return (
        f"Connected {len(rows)} source(s):\n"
        + engine.md_table(["source", "kind", "rows", "cols", "from"], rows)
        + "\nQuery them by name in data_query (e.g. SELECT * FROM "
        + rows[0][0]
        + " LIMIT 5)."
        + _notes(problems + notes)
    )


def _short(path: str, table) -> str:
    p = Path(path)
    s = f"{p.parent.name}/{p.name}" if p.parent.name else p.name
    return f"{s}:{table}" if table is not None else s


@tool
def data_sources() -> str:
    """List the connected data sources (SQL view name, kind, rows, columns, file)."""
    srcs = sources.load()
    ok, notes = sources.usable(srcs)
    if not ok:
        return _no_sources(notes)
    rows = [
        (s["name"], s["kind"], s.get("rows", "—"), s.get("columns", "—"), _short(s["path"], s.get("table"))) for s in ok
    ]
    return engine.md_table(["source", "kind", "rows", "cols", "from"], rows) + _notes(notes)


def _one(source: str) -> tuple[list[dict], dict | None, str]:
    ok, notes = sources.usable()
    want = sources.sanitize(source) if source not in {s["name"] for s in ok} else source
    hit = next((s for s in ok if s["name"] == want), None)
    if hit is None:
        names = ", ".join(s["name"] for s in ok) or "(none)"
        return ok, None, f"No connected source {source!r}. Connected: {names}." + _notes(notes)
    return ok, hit, ""


@tool
def data_schema(source: str) -> str:
    """Columns, types and 5 sample rows of one connected source."""
    ok, s, err = _one(source)
    if err:
        return err
    _, timeout = _caps()
    v = engine.ident(s["name"])
    try:
        desc = engine.run_query([s], f"DESCRIBE SELECT * FROM {v}", cap=2000, timeout_s=timeout)
        sample = engine.run_query([s], f"SELECT * FROM {v} LIMIT 5", cap=5, timeout_s=timeout)
    except engine.QueryError as e:
        return str(e)
    cols = [(r[0], r[1]) for r in desc.rows]
    return (
        f"`{s['name']}` ({s['kind']}, {s.get('rows', '?')} rows) — {len(cols)} columns:\n"
        + engine.md_table(["column", "type"], cols)
        + "\n\nSample:\n"
        + engine.md_table(sample.columns, sample.rows, width=40)
    )


@tool
def data_query(sql: str) -> str:
    """Run ONE read-only SQL query (DuckDB dialect) over the connected sources.

    Sources are views named as data_sources lists them. Only SELECT / WITH / FROM queries run —
    nothing can be written, attached or installed. Results are capped (row cap, time cap):
    aggregate or LIMIT in SQL rather than paging through raw rows.
    """
    ok, notes = sources.usable()
    if not ok:
        return _no_sources(notes)
    cap, timeout = _caps()
    try:
        r = engine.run_query(ok, sql, cap=cap, timeout_s=timeout)
    except engine.QueryError as e:
        return str(e) + _notes(notes)
    tail = (
        f"\n({len(r.rows)} rows shown — TRUNCATED at the {cap}-row cap; aggregate or LIMIT)"
        if r.truncated
        else f"\n({len(r.rows)} row{'s' if len(r.rows) != 1 else ''}, {r.elapsed * 1000:.0f} ms)"
    )
    return engine.md_table(r.columns, r.rows) + tail + _notes(notes)


_NUMERIC = re.compile(r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|U\w*INT|FLOAT|DOUBLE|REAL|DECIMAL)", re.I)


@tool
def data_profile(source: str) -> str:
    """Profile one source: per column nulls, distinct count, min/max, mean/std, quartiles,
    IQR outlier counts (numeric), and top values (low-cardinality text). Run this before
    analysing a dataset you haven't seen — it shows what's clean, sparse, skewed or odd."""
    ok, s, err = _one(source)
    if err:
        return err
    _, timeout = _caps()
    v = engine.ident(s["name"])
    try:
        summ = engine.run_query([s], f"SUMMARIZE SELECT * FROM {v}", cap=500, timeout_s=timeout)
    except engine.QueryError as e:
        return str(e)
    idx = {c: i for i, c in enumerate(summ.columns)}

    def g(row, k):
        return row[idx[k]] if k in idx else None

    rows, extra = [], []
    for r in summ.rows:
        col, typ = g(r, "column_name"), str(g(r, "column_type"))
        count, nullp, uniq = g(r, "count"), g(r, "null_percentage"), g(r, "approx_unique")
        nulls = round(float(count or 0) * float(nullp or 0) / 100)
        stats = ""
        outliers = ""
        if _NUMERIC.match(typ):
            mean, std = g(r, "avg"), g(r, "std")
            stats = f"μ {engine.cell(_f(mean), 12)} σ {engine.cell(_f(std), 12)} q1 {g(r, 'q25')} q3 {g(r, 'q75')}"
            q1, q3 = _f(g(r, "q25")), _f(g(r, "q75"))
            if q1 is not None and q3 is not None:
                lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
                c = engine.ident(col)
                try:
                    o = engine.run_query(
                        [s], f"SELECT count(*) FROM {v} WHERE {c} < {lo!r} OR {c} > {hi!r}", cap=1, timeout_s=timeout
                    )
                    outliers = str(o.rows[0][0])
                except engine.QueryError:
                    outliers = "?"
        elif "VARCHAR" in typ.upper() and uniq is not None and int(uniq) <= 25:
            c = engine.ident(col)
            try:
                t = engine.run_query(
                    [s],
                    f"SELECT {c}, count(*) AS n FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5",
                    cap=5,
                    timeout_s=timeout,
                )
                stats = "top: " + ", ".join(f"{engine.cell(a, 20)} ({b})" for a, b in t.rows)
            except engine.QueryError:
                pass
        rows.append(
            (
                col,
                typ,
                f"{nulls} ({float(nullp or 0):.1f}%)",
                uniq,
                engine.cell(g(r, "min"), 24),
                engine.cell(g(r, "max"), 24),
                stats or "—",
                outliers or "—",
            )
        )
    head = f"`{s['name']}` — {summ.rows[0][idx['count']] if summ.rows and 'count' in idx else '?'} rows, {len(rows)} columns"
    return (
        head
        + "\n"
        + engine.md_table(["column", "type", "nulls", "≈distinct", "min", "max", "stats", "IQR outliers"], rows, 70)
        + "".join(extra)
    )


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ── charts ──────────────────────────────────────────────────────────────────


def _strip_urls(node, dropped: list[str]):
    """Remove every ``data`` that loads from a URL (the panel refuses them anyway)."""
    if isinstance(node, dict):
        d = node.get("data")
        if isinstance(d, dict) and ("url" in d or "values" not in d and "name" not in d and "sequence" not in d):
            if "url" in d:
                dropped.append(str(d.get("url"))[:80])
            node.pop("data", None)
        for k, v in list(node.items()):
            if k != "data":
                _strip_urls(v, dropped)
    elif isinstance(node, list):
        for x in node:
            _strip_urls(x, dropped)


def build_spec(spec_in, title: str, columns: list[str], rows: list[tuple]) -> tuple[dict, list[str]]:
    """The spec the panel renders: the model's spec + the query rows inlined as ``data.values``."""
    if isinstance(spec_in, str):
        try:
            spec = json.loads(spec_in) if spec_in.strip() else {}
        except ValueError as e:
            raise engine.QueryError(f"vega_lite_spec isn't valid JSON: {e}") from None
    else:
        spec = json.loads(json.dumps(spec_in or {}))
    if not isinstance(spec, dict):
        raise engine.QueryError("vega_lite_spec must be a JSON object (a Vega-Lite spec).")
    if not any(k in spec for k in _VIEW_KEYS):
        raise engine.QueryError(
            "vega_lite_spec needs a `mark` (or `layer` / `facet` / `concat` / `repeat`) — e.g. "
            '{"mark": "bar", "encoding": {"x": {"field": "day", "type": "nominal"}, '
            '"y": {"field": "revenue", "type": "quantitative"}}}'
        )
    notes: list[str] = []
    dropped: list[str] = []
    top = spec.get("data")
    if top is not None:
        notes.append("replaced the spec's `data` with the query rows (charts take their data from `sql`)")
    spec.pop("data", None)
    _strip_urls(spec, dropped)
    if dropped:
        notes.append(f"dropped {len(dropped)} external data URL(s) — only inline data renders")
    values = [{c: engine.json_safe(v) for c, v in zip(columns, r)} for r in rows]
    spec["data"] = {"values": values}
    spec.setdefault("$schema", SCHEMA)
    if title and "title" not in spec:
        spec["title"] = title
    missing = sorted(_fields(spec) - set(columns))
    if missing:
        notes.append(
            f"spec encodes field(s) not in the query's columns: {', '.join(missing)} (columns: {', '.join(columns)})"
        )
    return spec, notes


def _fields(node) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        enc = node.get("encoding")
        if isinstance(enc, dict):
            for ch in enc.values():
                for c in ch if isinstance(ch, list) else [ch]:
                    if isinstance(c, dict) and isinstance(c.get("field"), str) and "aggregate" not in c:
                        out.add(c["field"].split(".")[0])
        for k, v in node.items():
            if k not in ("data", "encoding", "transform"):
                out |= _fields(v)
        if "transform" in node:  # derived fields — don't second-guess them
            return set()
    elif isinstance(node, list):
        for x in node:
            out |= _fields(x)
    return out


def _service():
    try:
        from graph import sdk  # host import — lazy
    except Exception:  # noqa: BLE001
        return None
    get = getattr(sdk, "service", None)
    if not callable(get):
        return None
    try:
        return get("artifact.show")
    except Exception:  # noqa: BLE001
        return None


@tool
def data_chart(sql: str, vega_lite_spec: str, title: str = "") -> str:
    """Chart a query in the console's Artifact panel (a live, themed Vega-Lite chart).

    ``sql``: ONE read-only SELECT over the connected sources that returns exactly the rows to
    plot — aggregate in SQL (≤ a few thousand rows). ``vega_lite_spec``: a SMALL Vega-Lite JSON
    spec WITHOUT data — just mark + encoding (fields = the query's column names), e.g.
    {"mark": "bar", "encoding": {"x": {"field": "weekday", "type": "nominal", "sort": "-y"},
    "y": {"field": "revenue", "type": "quantitative"}}}. The query rows are inlined for you; don't
    set colours or background (the chart follows the console theme). ``title`` labels it.
    """
    t0 = time.monotonic()
    ok, notes = sources.usable()
    if not ok:
        return _no_sources(notes)
    cap = settings.int_setting("chart_row_cap", 1, 50_000)
    _, timeout = _caps()
    try:
        r = engine.run_query(ok, sql, cap=cap, timeout_s=timeout)
    except engine.QueryError as e:
        return str(e) + _notes(notes)
    if r.truncated:
        return (
            f"That query returns more than {cap} rows — too many to chart. Aggregate in SQL "
            "(GROUP BY a category or date_trunc('week', …)) or LIMIT to the top N, then chart again."
        )
    if not r.rows:
        return "The query returned no rows — nothing to chart. Check the filters with data_query first."
    try:
        spec, spec_notes = build_spec(vega_lite_spec, title, r.columns, r.rows)
    except engine.QueryError as e:
        return str(e)
    code = json.dumps(spec, separators=(",", ":"), ensure_ascii=False)
    if len(code.encode()) > MAX_SPEC_BYTES:
        return f"The chart data is {len(code) // 1024} KB — over the panel's limit. Aggregate further in SQL."
    show = _service()
    summary = f"{len(r.rows)} rows × {len(r.columns)} cols ({', '.join(r.columns)})"
    if show is None:
        bare = {k: v for k, v in spec.items() if k != "data"}
        return (
            "Chart NOT rendered: the Artifact panel isn't available (enable the Artifact plugin, or this "
            "protoAgent is older than 0.192.0, which added Vega-Lite charts). The query ran fine — "
            f"{summary}. Spec (data omitted): {json.dumps(bare, separators=(',', ':'))[:1500]}"
            + _notes(spec_notes + notes)
        )
    try:
        res = show(kind="vega-lite", code=code, title=title or str(spec.get("title") or "Chart"))
    except Exception as e:  # noqa: BLE001
        return f"The Artifact panel refused the chart: {engine._first_line(e)}"
    res = res if isinstance(res, dict) else {"ok": False, "message": str(res)}
    msg = str(res.get("message") or ("Chart created." if res.get("ok") else "Chart not created."))
    elapsed = time.monotonic() - t0
    return f"{msg}\nData: {summary}, {elapsed:.1f}s." + _notes(spec_notes + notes) + str(res.get("ref") or "")


# ── export ──────────────────────────────────────────────────────────────────

_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}$")


@tool
def data_export(sql: str, format: str = "csv", filename: str = "") -> str:
    """Export a read-only query's result to a CSV or Parquet FILE in the agent's workspace
    (``data-exports/``) — for handing data to another tool or to the operator. Never writes
    into the source folders. ``format`` is "csv" or "parquet"; ``filename`` is a bare name
    (no folders). Returns the written path."""
    fmt = (format or "csv").strip().lower()
    if fmt not in ("csv", "parquet"):
        return 'format must be "csv" or "parquet".'
    fn = (filename or "").strip() or time.strftime("export-%Y%m%d-%H%M%S")
    if "/" in fn or "\\" in fn or ".." in fn or not _FILENAME.match(fn):
        return f"{filename!r}: give a bare file name (letters, digits, space, _ . -; no folders or '..')."
    if not fn.lower().endswith("." + fmt):
        fn += "." + fmt
    out_dir = paths.export_dir()
    out = (out_dir / fn).resolve()
    if out.parent != out_dir:
        return f"{filename!r} escapes the export folder — refused."
    for root in fence.configured_paths(settings.cfg().get("data_dirs")):
        if fence.within(out, root) or fence.within_any_case(out, root):
            return (
                f"Refused: the export folder ({out_dir}) is inside the data folder {root} — exports never "
                "write into source folders. The operator can move the workspace or narrow data_dirs."
            )
    ok, notes = sources.usable()
    if not ok:
        return _no_sources(notes)
    cap = settings.int_setting("export_row_cap", 1, 100_000_000)
    _, timeout = _caps()
    try:
        n = engine.export(ok, sql, out, fmt, cap=cap, timeout_s=max(timeout, 60.0))
    except engine.QueryError as e:
        return str(e) + _notes(notes)
    capped = f" (capped at {cap} rows)" if n >= cap else ""
    return f"Exported {n} rows{capped} → {out} ({out.stat().st_size // 1024} KB, {fmt})." + _notes(notes)


TOOLS = [data_connect, data_sources, data_schema, data_query, data_profile, data_chart, data_export]
