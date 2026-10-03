---
name: building-a-chart
description: When the user wants to SEE their data — "chart it", "plot", "graph", "visualize", "show me the trend" — render it with data_chart (SQL plus a small Vega-Lite spec, no data) instead of hand-writing an HTML/React chart.
---

# Building a chart

`data_chart(sql, vega_lite_spec, title)` runs your query, inlines the rows into your spec, and
renders a live chart in the Artifact panel, themed to the console (dark or light). It is fast
because you only write two small things:

- **`sql`** — one SELECT that returns *exactly* the rows to plot, already aggregated. Aim for
  tens to hundreds of rows; the hard cap is a few thousand.
- **`vega_lite_spec`** — a SMALL Vega-Lite JSON object: `mark` + `encoding`, with field names
  matching the query's columns. **No `data`** (it's added for you; URLs are refused), **no
  colours or background** (the theme supplies them), no `$schema` needed. Set `"tooltip": true`
  on the mark.

Do NOT hand-write a React/HTML/D3 chart with `show_artifact` for data you can query — it is
slower, larger, and doesn't follow the theme.

## Patterns that look good

**Ranked categories — bar, sorted descending** (best weekdays, top products):
```json
{"mark": {"type": "bar", "tooltip": true},
 "encoding": {"x": {"field": "weekday", "type": "nominal", "sort": "-y", "title": null},
              "y": {"field": "revenue", "type": "quantitative", "title": "Revenue"}}}
```
For a fixed natural order (Mon→Sun) use `"sort": ["Monday", "Tuesday", …]` instead, and say
which is best in your answer. Many long labels? Swap to horizontal: category on `y`, value on `x`.

**Trend over time — line with a temporal x** (`SELECT date_trunc('week', d) AS week, sum(x) …`):
```json
{"mark": {"type": "line", "point": true, "tooltip": true},
 "encoding": {"x": {"field": "week", "type": "temporal", "title": null},
              "y": {"field": "revenue", "type": "quantitative"}}}
```
Add `"color": {"field": "store", "type": "nominal"}` for a few series (≤ 6). Area for a single
cumulative series: `"mark": "area"`.

**Composition — stacked or grouped bar**:
```json
{"mark": {"type": "bar", "tooltip": true},
 "encoding": {"x": {"field": "month", "type": "ordinal"},
              "y": {"field": "units", "type": "quantitative"},
              "color": {"field": "category", "type": "nominal"}}}
```
Grouped instead of stacked: add `"xOffset": {"field": "category"}`.

**Two dimensions — heat map** (weekday × hour, `SELECT dayname(ts) AS day, hour(ts) AS hour, count(*) AS orders …`):
```json
{"mark": {"type": "rect", "tooltip": true},
 "encoding": {"x": {"field": "hour", "type": "ordinal"},
              "y": {"field": "day", "type": "ordinal", "sort": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]},
              "color": {"field": "orders", "type": "quantitative"}}}
```

**Distribution — histogram**: `"mark": "bar"`, `"x": {"field": "basket", "bin": {"maxbins": 30}}`,
`"y": {"aggregate": "count"}` (here the SQL can return raw values, within the row cap).

**Small multiples — facet only when comparing many groups** (> 6 lines get unreadable):
`"facet": {"field": "store", "columns": 3}` with a `"spec": {mark, encoding}`.

## Rules of thumb

- One question per chart; the title says the answer ("Saturdays sell 38% more than average").
- Sort bars by value unless the axis has a natural order (days, months, sizes).
- Temporal fields: return real dates/timestamps from SQL and use `"type": "temporal"`.
- Use `"title": null` on an axis whose meaning is obvious; give units in the other.
- If the reply says a field isn't in the query's columns, fix the alias in SQL or the spec.
- Iterating? Call `data_chart` again (each call is a new chart), or `update_artifact` for a tiny
  spec tweak on the chart you just made.
