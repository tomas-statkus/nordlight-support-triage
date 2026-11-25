#!/usr/bin/env python3
"""A tiny local web app for viewing Nordlight evaluation results."""

import argparse
import csv
import html
import statistics
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"


def load_results(version):
    path = RESULTS_DIR / f"results_{version}.csv"
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_retrieval_results():
    path = RESULTS_DIR / "retrieval_results.csv"
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def accuracy(rows, field):
    if not rows:
        return 0
    correct = sum(row[f"expected_{field}"] == row[f"got_{field}"] for row in rows)
    return round(correct * 100 / len(rows))


def split_titles(value):
    return [title.strip() for title in value.split("|") if title.strip()]


def retrieval_metrics(rows):
    if not rows:
        return None

    matched_articles = 0
    expected_articles = 0
    hits = 0
    for row in rows:
        expected = set(split_titles(row["expected_titles"]))
        retrieved = set(split_titles(row["retrieved_titles"]))
        matched = expected & retrieved
        expected_articles += len(expected)
        matched_articles += len(matched)
        hits += bool(matched)

    return {
        "hit_count": hits,
        "ticket_count": len(rows),
        "hit_pct": round(100 * hits / len(rows)),
        "matched_articles": matched_articles,
        "expected_articles": expected_articles,
        "recall_pct": round(100 * matched_articles / expected_articles) if expected_articles else 0,
        "median_seconds": round(statistics.median(float(row["seconds"]) for row in rows), 2),
        "top_k": rows[0]["top_k"],
    }


def page(version, rows, retrieval_rows, home_href=None):
    fields = ["category", "urgency", "sensitive", "complexity"]
    base = "/eval" if home_href else "/"
    links = " ".join(
        f'<a href="{base}?run={name}">{name.upper()}</a>'
        for name in ("v1", "v2", "v3")
    )
    if home_href:
        links = f'<a href="{html.escape(home_href)}">← Console</a> ' + links

    cards = [
        ("Route", accuracy(rows, "route")),
        *[(field.title(), accuracy(rows, field)) for field in fields],
    ]
    cards_html = "".join(
        f'<div class="card"><strong>{html.escape(label)}</strong><b>{value}%</b></div>'
        for label, value in cards
    )

    table_rows = []
    for row in rows:
        route_ok = row["expected_route"] == row["got_route"]
        got_fields = " | ".join(
            f"{field}: {row[f'got_{field}']}" for field in fields
        )
        table_rows.append(
            "<tr>"
            f'<td>{html.escape(row["id"])}</td>'
            f'<td><span class="{"ok" if route_ok else "miss"}">'
            f'{html.escape(row["got_route"])}</span></td>'
            f"<td>{html.escape(got_fields)}</td>"
            f'<td>{html.escape(row["summary"])}</td>'
            "</tr>"
        )

    if not retrieval_rows:
        retrieval_html = (
            '<section class="section"><div class="section-heading">'
            "<h2>Retrieval evaluation</h2></div>"
            '<p class="section-copy">No retrieval results yet. Run '
            "<code>python3 eval/run_retrieval_eval.py</code> to generate them.</p></section>"
        )
    else:
        metrics = retrieval_metrics(retrieval_rows)
        retrieval_cards = [
            (f"Ticket hit@{metrics['top_k']}", f"{metrics['hit_count']}/{metrics['ticket_count']} ({metrics['hit_pct']}%)"),
            (f"Article recall@{metrics['top_k']}", f"{metrics['matched_articles']}/{metrics['expected_articles']} ({metrics['recall_pct']}%)"),
            ("Median query latency", f"{metrics['median_seconds']:.2f} s"),
        ]
        retrieval_cards_html = "".join(
            f'<div class="card"><strong>{html.escape(label)}</strong><b>{html.escape(value)}</b></div>'
            for label, value in retrieval_cards
        )
        retrieval_table_rows = []
        for row in retrieval_rows:
            expected = split_titles(row["expected_titles"])
            retrieved = split_titles(row["retrieved_titles"])
            matched = set(expected) & set(retrieved)
            if len(matched) == len(expected):
                status = "complete"
                status_class = "ok"
            elif matched:
                status = "partial"
                status_class = "miss"
            else:
                status = "miss"
                status_class = "miss"
            retrieval_table_rows.append(
                "<tr>"
                f'<td>{html.escape(row["id"])}</td>'
                f'<td>{html.escape(", ".join(expected))}</td>'
                f'<td>{html.escape(", ".join(retrieved))}</td>'
                f'<td><span class="{status_class}">{status} ({len(matched)}/{len(expected)})</span></td>'
                f'<td>{float(row["seconds"]):.2f} s</td>'
                "</tr>"
            )
        retrieval_html = (
            '<section class="section"><div class="section-heading">'
            "<h2>Retrieval evaluation</h2>"
            '<span class="section-note">LOCAL BENCHMARK · TOP-K RESULTS</span></div>'
            f'<div class="cards retrieval-cards">{retrieval_cards_html}</div>'
            '<div class="table-wrap"><table><thead><tr><th>ID</th><th>Expected articles</th>'
            "<th>Retrieved articles</th><th>Hit</th><th>Latency</th></tr></thead>"
            f"<tbody>{''.join(retrieval_table_rows)}</tbody></table></div>"
            '<p class="section-copy">Ticket hit means at least one expected article was found; article recall '
            "counts all expected articles found. Timings exclude model warmup and indexing. "
            "This is a local retrieval mirror, not a measurement of n8n's live store.</p></section>"
        )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nordlight evaluations</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #080d17;
      color: #e8edf7;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      --surface: #101827;
      --surface-raised: #151f30;
      --border: #243147;
      --muted: #91a0b7;
      --accent: #73e0c1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      max-width: 1180px;
      min-height: 100vh;
      margin: 0 auto;
      padding: 52px 28px 72px;
      background: radial-gradient(ellipse at 50% -18%, #1b2b43 0, #080d17 52%);
    }}
    .page-header {{ padding: 12px 0 30px; border-bottom: 1px solid var(--border); }}
    .eyebrow {{ color: var(--accent); font-size: 11px; font-weight: 750; letter-spacing: .16em; }}
    h1 {{ margin: 13px 0 8px; font-size: clamp(32px, 5vw, 48px); letter-spacing: -.045em; }}
    .subtitle {{ max-width: 660px; margin: 0; color: var(--muted); line-height: 1.65; }}
    nav {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 24px; }}
    nav a {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 0 13px;
      border: 1px solid var(--border);
      border-radius: 999px;
      color: #aebbd0;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .04em;
      text-decoration: none;
      transition: border-color .16s ease, color .16s ease, background .16s ease;
    }}
    nav a:hover {{ color: var(--accent); border-color: #3b756d; background: #10251f; }}
    .section {{ margin-top: 34px; }}
    .section-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 16px; }}
    h2 {{ margin: 0; font-size: 20px; letter-spacing: -.025em; }}
    .section-note {{ color: var(--muted); font-size: 12px; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
    .card {{
      min-height: 108px;
      padding: 17px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(145deg, #141f30, #0f1725);
      box-shadow: 0 8px 24px #00000014;
    }}
    .card strong {{ display: block; color: var(--muted); font-size: 11px; font-weight: 650; letter-spacing: .07em; text-transform: uppercase; }}
    .card b {{ display: block; margin-top: 12px; color: #f2f6ff; font-size: 26px; letter-spacing: -.04em; }}
    .retrieval-cards {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .table-wrap {{ overflow-x: auto; margin-top: 16px; border: 1px solid var(--border); border-radius: 12px; background: #0e1623cc; }}
    table {{ width: 100%; border-collapse: collapse; text-align: left; }}
    th {{ color: #9baac0; background: #141e2d; font-size: 10px; font-weight: 750; letter-spacing: .1em; text-transform: uppercase; }}
    th, td {{ padding: 13px 15px; border-bottom: 1px solid #202c3e; vertical-align: top; }}
    td {{ color: #c5cede; font-size: 13px; line-height: 1.55; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover {{ background: #172235; }}
    td:first-child {{ color: #8998ae; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .ok, .miss {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }}
    .ok {{ color: #86e5be; background: #143427; }}
    .miss {{ color: #ff9a9a; background: #3b2029; }}
    .section-copy {{ margin: 13px 0 0; color: var(--muted); font-size: 12px; line-height: 1.65; }}
    code {{ padding: 3px 6px; border: 1px solid var(--border); border-radius: 5px; background: #141e2d; color: var(--accent); }}
    @media (max-width: 820px) {{
      body {{ padding: 32px 20px 56px; }}
      .cards {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 560px) {{
      body {{ padding: 24px 14px 40px; }}
      .cards, .retrieval-cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }}
      .card {{ min-height: 96px; padding: 13px; }}
      .card b {{ font-size: 23px; }}
      .section-heading {{ align-items: flex-start; flex-direction: column; gap: 5px; }}
      th, td {{ padding: 11px 12px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="page-header">
      <div class="eyebrow">NORDLIGHT · MODEL QUALITY</div>
      <h1>Evaluation dashboard</h1>
      <p class="subtitle">A closer look at classifier performance and knowledge retrieval across the local benchmark.</p>
      <nav aria-label="Evaluation runs">{links}</nav>
    </header>
    <section class="section" aria-labelledby="classifier-heading">
      <div class="section-heading">
        <h2 id="classifier-heading">Classifier performance</h2>
        <span class="section-note">{html.escape(version.upper())} · {len(rows)} held-out tickets</span>
      </div>
      <div class="cards">{cards_html}</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>Got route</th><th>Predicted labels</th><th>Summary</th></tr></thead>
          <tbody>{''.join(table_rows)}</tbody>
        </table>
      </div>
    </section>
    {retrieval_html}
  </main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        version = query.get("run", ["v3"])[0]
        if version not in ("v1", "v2", "v3"):
            version = "v3"

        rows = load_results(version)
        if rows is None:
            body = "<h1>No results found</h1>"
        else:
            body = page(version, rows, load_retrieval_results())

        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Open http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()