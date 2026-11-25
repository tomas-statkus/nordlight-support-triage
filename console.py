#!/usr/bin/env python3
"""Nordlight Console: a read-only local homepage for the whole project.

Shows local service health (Ollama, n8n), the pipeline and models read from the
exported workflows, the knowledge base, and an evaluation snapshot. It never
reads tickets from n8n and never triggers workflows.

Usage (from the repo folder):
    python3 console.py
Standard library only.
"""
import argparse
import html
import json
import re
import statistics
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "eval"))

import app as eval_app  # noqa: E402
from run_retrieval_eval import load_articles  # noqa: E402

TRIAGE_WORKFLOW = ROOT / "workflows" / "support-triage.json"
KB_WORKFLOW = ROOT / "workflows" / "kb-loader.json"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_N8N_URL = "http://127.0.0.1:5678"
CHECK_TIMEOUT_S = 1.5

# Ignore any system proxy: both services run on this machine.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

NODE_KINDS = {
    "n8n-nodes-base.formTrigger": "form",
    "n8n-nodes-base.code": "code",
    "n8n-nodes-base.httpRequest": "HTTP call",
    "n8n-nodes-base.switch": "router",
    "n8n-nodes-base.dataTable": "data table",
    "n8n-nodes-base.wait": "human review",
    "@n8n/n8n-nodes-langchain.vectorStoreInMemory": "retrieval",
    "@n8n/n8n-nodes-langchain.chainLlm": "cloud LLM",
}


def load_workflow(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_model(name):
    return name if ":" in name else f"{name}:latest"


def workflow_models(workflows):
    """Return (local Ollama models, cloud models) referenced by the workflows."""
    local, cloud = set(), set()
    for workflow in workflows:
        for node in workflow["nodes"]:
            params = node.get("parameters", {})
            node_type = node["type"]
            if node_type.endswith("embeddingsOllama") and params.get("model"):
                local.add(normalize_model(params["model"]))
            elif node_type == "n8n-nodes-base.httpRequest" and ":11434" in params.get("url", ""):
                local.update(normalize_model(m) for m in re.findall(r'"model":\s*"([^"]+)"', params.get("jsonBody", "")))
            elif node_type.endswith("lmChatAnthropic"):
                model = params.get("model")
                cloud.add(model.get("value") if isinstance(model, dict) else model)
    return sorted(local), sorted(m for m in cloud if m)


def pipeline_steps(workflow):
    """Main-flow nodes in breadth-first order from the trigger, with a short kind label."""
    nodes = {node["name"]: node for node in workflow["nodes"]}
    connections = workflow.get("connections", {})
    targets = {
        link["node"]
        for outputs in connections.values()
        for link in (l for branch in outputs.get("main", []) for l in (branch or []))
    }
    queue = [name for name, node in nodes.items() if node["type"].endswith("Trigger") and name not in targets]
    seen, steps = set(queue), []
    while queue:
        name = queue.pop(0)
        node = nodes[name]
        kind = NODE_KINDS.get(node["type"], node["type"].rsplit(".", 1)[-1])
        if kind == "HTTP call" and ":11434" in node.get("parameters", {}).get("url", ""):
            kind = "local LLM"
        steps.append((name, kind))
        for branch in connections.get(name, {}).get("main", []):
            for link in branch or []:
                if link["node"] not in seen:
                    seen.add(link["node"])
                    queue.append(link["node"])
    return steps


def form_path(workflow):
    for node in workflow["nodes"]:
        if node["type"] == "n8n-nodes-base.formTrigger" and node.get("webhookId"):
            return f"/form/{node['webhookId']}"
    return None


def fetch_json(url):
    with OPENER.open(url, timeout=CHECK_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def check_ollama(base_url, required_models, fetch=fetch_json):
    try:
        data = fetch(f"{base_url}/api/tags")
    except (OSError, ValueError) as error:
        return {"ok": False, "detail": f"Not reachable at {base_url} ({error})", "models": []}
    installed = {normalize_model(m.get("name", "")) for m in data.get("models", [])}
    models = [(model, model in installed) for model in required_models]
    missing = [model for model, present in models if not present]
    detail = f"Missing: {', '.join(missing)}" if missing else "All workflow models installed"
    return {"ok": not missing, "detail": detail, "models": models}


def check_n8n(base_url, fetch=fetch_json):
    try:
        data = fetch(f"{base_url}/healthz")
    except (OSError, ValueError) as error:
        return {"ok": False, "detail": f"Not reachable at {base_url} ({error})"}
    ok = data.get("status") == "ok"
    return {"ok": ok, "detail": "Healthy" if ok else f"Unexpected health response: {data}"}


def quality_snapshot():
    rows = eval_app.load_results("v3")
    snapshot = {"classifier": None, "retrieval": None}
    if rows:
        sensitive = [r for r in rows if r["expected_sensitive"] == "True"]
        snapshot["classifier"] = {
            "route_pct": eval_app.accuracy(rows, "route"),
            "tickets": len(rows),
            "sensitive_leaked": sum(r["got_route"] != "human" for r in sensitive),
            "sensitive_total": len(sensitive),
            "median_seconds": statistics.median(float(r["seconds"]) for r in rows),
        }
    snapshot["retrieval"] = eval_app.retrieval_metrics(eval_app.load_retrieval_results())
    return snapshot


def status_badge(ok):
    return '<span class="badge ok">● up</span>' if ok else '<span class="badge down">● down</span>'


def card(label, value, note=""):
    note_html = f"<small>{html.escape(note)}</small>" if note else ""
    return f'<div class="card"><strong>{html.escape(label)}</strong><b>{html.escape(value)}</b>{note_html}</div>'


def home_page(ollama, n8n, steps, local_models, cloud_models, articles, snapshot, n8n_url, form):
    e = html.escape

    model_items = "".join(
        f'<li><code>{e(model)}</code> {"✅" if present else "❌ not installed"}</li>'
        for model, present in ollama["models"]
    ) or "".join(f"<li><code>{e(model)}</code></li>" for model in local_models)
    cloud_items = "".join(f"<li><code>{e(m)}</code> (cloud, not checked; redacted text only)</li>" for m in cloud_models)

    step_items = "".join(
        f'<li><span class="kind kind-{e(kind.replace(" ", "-"))}">{e(kind)}</span> {e(name)}</li>'
        for name, kind in steps
    )
    article_items = "".join(f"<li>{e(a['title'])}</li>" for a in articles)

    quality_cards = []
    classifier = snapshot["classifier"]
    if classifier:
        quality_cards += [
            card("Route correct (v3)", f"{classifier['route_pct']}%", f"{classifier['tickets']} held-out tickets"),
            card("Sensitive reaching a model", f"{classifier['sensitive_leaked']} of {classifier['sensitive_total']}", "should be 0"),
            card("Classifier median latency", f"{classifier['median_seconds']:.1f} s", "per ticket, model warm"),
        ]
    retrieval = snapshot["retrieval"]
    if retrieval:
        quality_cards += [
            card(f"Retrieval hit@{retrieval['top_k']}", f"{retrieval['hit_pct']}%", f"{retrieval['hit_count']}/{retrieval['ticket_count']} tickets"),
            card(f"Article recall@{retrieval['top_k']}", f"{retrieval['recall_pct']}%", f"{retrieval['matched_articles']}/{retrieval['expected_articles']} articles"),
        ]
    quality_html = (
        f'<div class="cards">{"".join(quality_cards)}</div>'
        if quality_cards
        else "<p>No evaluation results yet. Run the commands below.</p>"
    )

    form_html = (
        f'<li><a href="{e(n8n_url + form)}">Ticket form</a> (the Support triage workflow must be active; use fake data only)</li>'
        if form else ""
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nordlight Console</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #080d17;
      color: #e8edf7;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      --surface: #101827;
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
    .page-header p {{ max-width: 720px; margin: 0; color: var(--muted); line-height: 1.65; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ color: #a4f3dc; text-decoration: underline; }}
    section {{ margin-top: 34px; }}
    h2 {{ margin: 0 0 16px; font-size: 20px; letter-spacing: -.025em; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .panel {{
      min-width: 0;
      padding: 18px 20px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: linear-gradient(145deg, #141f30, #0f1725);
      box-shadow: 0 10px 28px #00000018;
    }}
    .panel h3 {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 10px; font-size: 16px; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
    .card {{
      min-width: 0;
      min-height: 108px;
      padding: 16px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(145deg, #141f30, #0f1725);
    }}
    .card strong {{ display: block; color: var(--muted); font-size: 11px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }}
    .card b {{ display: block; margin: 12px 0 3px; color: #f2f6ff; font-size: 24px; letter-spacing: -.035em; }}
    .card small {{ display: block; color: var(--muted); font-size: 11px; }}
    .badge {{ padding: 5px 10px; border-radius: 999px; font-size: 11px; font-weight: 750; letter-spacing: .05em; text-transform: uppercase; white-space: nowrap; }}
    .ok {{ color: #86e5be; background: #143427; }}
    .down {{ color: #ff9a9a; background: #3b2029; }}
    .note {{ color: var(--muted); font-size: 13px; line-height: 1.6; }}
    .panel ul, .panel ol {{ padding-left: 20px; }}
    .panel li {{ margin: 8px 0; color: #c5cede; font-size: 13px; line-height: 1.55; }}
    code {{ padding: 3px 6px; border: 1px solid var(--border); border-radius: 5px; background: #141e2d; color: var(--accent); font-size: 12px; }}
    .kind {{ display: inline-block; min-width: 92px; padding: 3px 7px; margin-right: 6px; border-radius: 5px;
             background: #202c3e; color: #c5cede; font-size: 11px; text-align: center; }}
    .kind-cloud-LLM {{ border: 1px dashed #bb873d; background: #342918; color: #f4c778; }}
    .kind-human-review {{ background: #18304a; color: #8cc7ff; }}
    .kind-local-LLM {{ background: #143427; color: #86e5be; }}
    pre {{ overflow-x: auto; padding: 15px 17px; border: 1px solid var(--border); border-radius: 12px;
           background: #0e1623; color: #c5cede; font-size: 12px; line-height: 1.8; }}
    @media (max-width: 900px) {{
      body {{ padding: 36px 20px 56px; }}
      .cards {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 600px) {{
      body {{ padding: 26px 14px 40px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }}
      .card {{ min-height: 98px; padding: 13px; }}
      .card b {{ font-size: 21px; }}
      .panel {{ padding: 16px; }}
      pre {{ font-size: 11px; }}
    }}
  </style>
</head>
<body>
  <main>
  <header class="page-header">
    <div class="eyebrow">NORDLIGHT · LOCAL-FIRST OPERATIONS</div>
    <h1>Nordlight Console</h1>
    <p>Local-first support ticket triage: n8n + Ollama, with a redacted-only cloud lane and human review.
       Read-only view; no ticket data is loaded from n8n. <a href="/">Refresh</a> · <a href="/eval">Evaluation details →</a></p>
  </header>

  <section>
    <h2>Local services</h2>
    <div class="grid">
      <div class="panel">
        <h3>Ollama {status_badge(ollama["ok"])}</h3>
        <p class="note">{e(ollama["detail"])}</p>
        <ul>{model_items}{cloud_items}</ul>
      </div>
      <div class="panel">
        <h3>n8n {status_badge(n8n["ok"])}</h3>
        <p class="note">{e(n8n["detail"])}</p>
        <ul>
          <li><a href="{e(n8n_url)}">Open the n8n editor</a></li>
          {form_html}
        </ul>
        <p class="note">The vector store is in memory: re-run the <b>KB loader</b> workflow after every n8n restart.
           This console can't check whether it has been loaded.</p>
      </div>
    </div>
  </section>

  <section>
    <h2>Quality snapshot</h2>
    {quality_html}
  </section>

  <section class="grid">
    <div class="panel">
      <h3>Support triage pipeline</h3>
      <p class="note">Read from <code>workflows/support-triage.json</code>. The dashed step is the only one that leaves the machine.</p>
      <ol>{step_items}</ol>
    </div>
    <div class="panel">
      <h3>Knowledge base ({len(articles)} articles)</h3>
      <p class="note">Read from <code>workflows/kb-loader.json</code>.</p>
      <ul>{article_items}</ul>
    </div>
  </section>

  <section>
    <h2>Quick commands</h2>
    <pre>NODE_OPTIONS=--dns-result-order=ipv4first npx n8n   # start n8n
python3 eval/run_eval.py                              # classifier evaluation (writes eval/results/latest.csv/.md)
python3 eval/run_retrieval_eval.py                    # retrieval evaluation
python3 -m unittest discover -s eval/tests            # tests</pre>
  </section>
  </main>
</body>
</html>"""


def not_found_page():
    return '<!doctype html><meta charset="utf-8"><h1>Not found</h1><p><a href="/">Back to the console</a></p>'


class Handler(BaseHTTPRequestHandler):
    ollama_url = DEFAULT_OLLAMA_URL
    n8n_url = DEFAULT_N8N_URL

    def do_GET(self):
        url = urlparse(self.path)
        status = 200
        if url.path == "/":
            body = self.render_home()
        elif url.path == "/eval":
            version = parse_qs(url.query).get("run", ["v3"])[0]
            if version not in ("v1", "v2", "v3"):
                version = "v3"
            rows = eval_app.load_results(version)
            body = (
                eval_app.page(version, rows, eval_app.load_retrieval_results(), home_href="/")
                if rows is not None
                else '<h1>No results found</h1><p><a href="/">Back to the console</a></p>'
            )
        else:
            status, body = 404, not_found_page()

        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def render_home(self):
        triage = load_workflow(TRIAGE_WORKFLOW)
        local_models, cloud_models = workflow_models([triage, load_workflow(KB_WORKFLOW)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            ollama = pool.submit(check_ollama, self.ollama_url, local_models)
            n8n = pool.submit(check_n8n, self.n8n_url)
            ollama, n8n = ollama.result(), n8n.result()
        return home_page(
            ollama, n8n, pipeline_steps(triage), local_models, cloud_models,
            load_articles(), quality_snapshot(), self.n8n_url, form_path(triage),
        )

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Nordlight Console: read-only local project homepage")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--n8n-url", default=DEFAULT_N8N_URL)
    args = parser.parse_args()

    Handler.ollama_url = args.ollama_url.rstrip("/")
    Handler.n8n_url = args.n8n_url.rstrip("/")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Nordlight Console: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
