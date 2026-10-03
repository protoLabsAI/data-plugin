---
name: exploring-a-dataset
description: When the user points at local data (a CSV, spreadsheet, Parquet/JSON file, SQLite database, or a folder of them) or asks a question about "my data" — connect it with data_connect, learn its shape with data_schema / data_profile, then answer with read-only SQL via data_query.
---

# Exploring a dataset

You have an embedded, **read-only** SQL engine (DuckDB) over the operator's local files. Use it
instead of reading files with filesystem tools: it is faster, exact, and never loads a whole file
into the conversation.

## The loop

1. **Connect** — `data_connect(path)` for a file or folder. Each file becomes a source (a SQL
   view); a SQLite database gives one per table, a workbook one per sheet. If it says no folders
   are allowlisted, tell the operator to add the folder in *Settings ▸ Plugins ▸ Data Analyst ▸
   Data folders* — you can't change that setting yourself, don't try.
2. **Look before you query** — `data_schema(source)` for columns, types and sample rows;
   `data_profile(source)` before any real analysis (nulls, distinct counts, ranges, skew,
   outliers, the top values of category columns). Note anything odd — a date stored as text, a
   column that is 40% null, a negative quantity — and say it in your answer.
3. **Answer with SQL** — `data_query(sql)`: ONE `SELECT` (or `WITH … SELECT`, or `FROM t …`).
   Aggregate in SQL; don't page through raw rows. Replies are capped, so `GROUP BY`, `ORDER BY …
   LIMIT 10`, `date_trunc('week', ts)`.
4. **Show it** — when a picture answers faster than a table, use `data_chart` (see the
   *building-a-chart* skill). When the operator wants the rows as a file, `data_export`.

## DuckDB SQL that helps

- Dates: `date_trunc('month', d)`, `dayname(d)`, `isodow(d)` (1 = Monday), `strftime(d, '%Y-%m')`,
  `d >= current_date - INTERVAL 90 DAY`. Text dates: `try_strptime(s, '%d/%m/%Y')`.
- Quarters: `quarter(d)`, `date_trunc('quarter', d)`; "last quarter" relative to the DATA is
  usually safer than relative to today — check `max(d)` first.
- Shares: `sum(x) / sum(sum(x)) OVER ()`. Ranks: `row_number() OVER (PARTITION BY g ORDER BY x DESC)`.
- Joins across files work — every source is just a view.
- `SUMMARIZE source`, `DESCRIBE source` and `FROM source LIMIT 5` are all plain SELECTs here.

## Report like an analyst

Lead with the answer and the number, then how you got it (the grouping, the date window), then
caveats (missing data, outliers you excluded). Quote exact figures from query results — never
estimate what you could have queried.

## What you can't do (by design)

Write, update, attach, install or COPY anything; read files outside the connected sources; reach
the network. A refusal saying "Read-only" or "Refused by the read-only engine" is the fence
working — rewrite the query as a SELECT over the connected sources.
