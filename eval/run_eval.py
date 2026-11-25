#!/usr/bin/env python3
"""Nordlight classifier evaluation.

Sends every ticket in eval/data/tickets.csv to the local model with the same
system prompt and JSON schema as the n8n "Classify (Ollama)" node, then
compares the labels with the expected ones.

Usage (from the repo folder, with Ollama running):
    python3 eval/run_eval.py

Writes eval/results/latest.md and eval/results/latest.csv. Standard library only.
"""
import csv
import hashlib
import json
import pathlib
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:4b"
FIELDS = ["category", "urgency", "sensitive", "complexity"]

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "requests": {"type": "array", "items": {"type": "string"}},
        "category": {
            "type": "string",
            "enum": ["returns", "delivery", "warranty", "order_change", "payment", "privacy", "other"],
        },
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
        "sensitive": {"type": "boolean"},
        "complexity": {"type": "string", "enum": ["simple", "complex"]},
    },
    "required": ["summary", "requests", "category", "urgency", "sensitive", "complexity"],
}

# Same fallback as the "Merge ticket" node: if the output can't be parsed, fail closed.
FAIL_CLOSED = {
    "summary": "Model output could not be parsed",
    "requests": [],
    "category": "other",
    "urgency": "high",
    "sensitive": True,
    "complexity": "complex",
}

# Same deterministic check as the "Merge ticket" node: a ticket that tries to
# instruct the model is always sent to a person. Keep the two lists in sync.
INJECTION = [re.compile(p, re.IGNORECASE) for p in [
    r"ignore (all |any |your |the )?(previous |prior |above |earlier )?(instructions|rules|prompt)",
    r"\b(system|developer) (prompt|message|override|instructions?)\b",
    r"\b(classify|label|mark|tag|flag)\s+(this|the|my)(\s+(ticket|message|email|request))?\s+as\b",
    r"\b(not|isn'?t) sensitive\b",
    r"\bnote to the (ai|assistant|bot|model)\b",
    r"\byou are (now )?(an? |the )?(ai|assistant|admin|bot|model)\b",
    r"\b(never|do not|don'?t) escalate\b",
]]

# Ignore any system proxy: Ollama is on this machine.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_prompt():
    path = DATA_DIR / "classify_prompt.txt"
    if not path.exists():
        sys.exit("Missing eval/data/classify_prompt.txt. Paste the system prompt from the n8n Classify (Ollama) node into it.")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw or raw.startswith("PASTE"):
        sys.exit("eval/data/classify_prompt.txt still has the placeholder. Paste the system prompt from n8n into it.")
    # Copied straight out of the n8n JSON body, the prompt still has \n and \" escapes. Undo them.
    if "\\n" in raw or '\\"' in raw:
        try:
            raw = json.loads('"' + raw + '"')
        except json.JSONDecodeError:
            raw = raw.replace("\\n", "\n").replace('\\"', '"')
    return raw


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def route(labels):
    if labels["sensitive"]:
        return "human"
    return "cloud" if labels["complexity"] == "complex" else "local"


def classify(system_prompt, order_number, message):
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
        "format": SCHEMA,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Order: {order_number}\n\n{message}"},
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    start = time.perf_counter()
    with OPENER.open(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    seconds = round(time.perf_counter() - start, 1)
    try:
        labels = json.loads(data["message"]["content"])
        labels["sensitive"] = to_bool(labels["sensitive"])
        for field in FIELDS:
            labels[field]  # raises KeyError if a field is missing
        return labels, seconds, False
    except (KeyError, TypeError, json.JSONDecodeError):
        return dict(FAIL_CLOSED), seconds, True


def show(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def pct(n, total):
    return f"{n}/{total} ({round(100 * n / total)}%)" if total else "0/0"


def cell(text, limit=80):
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main():
    prompt = load_prompt()
    prompt_id = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    with open(DATA_DIR / "tickets.csv", newline="", encoding="utf-8") as f:
        tickets = list(csv.DictReader(f))

    print(f"{len(tickets)} tickets · model {MODEL} · prompt {prompt_id}")
    print("Warming up the model (not timed)...")
    try:
        classify(prompt, "", "Hello")
    except OSError as e:
        sys.exit(f"Can't reach Ollama at {OLLAMA_URL}: {e}. Is Ollama running?")

    rows = []
    for t in tickets:
        expected = {
            "category": t["category"].strip(),
            "urgency": t["urgency"].strip(),
            "sensitive": to_bool(t["sensitive"]),
            "complexity": t["complexity"].strip(),
        }
        got, seconds, parse_error = classify(prompt, t["order_number"], t["message"])
        model_sensitive = got["sensitive"]
        rule = any(p.search(t["message"]) for p in INJECTION)
        if rule:
            got["sensitive"] = True
        # Same as "Merge ticket": two or more separate requests make a ticket complex (code only upgrades).
        model_complexity = got["complexity"]
        if isinstance(got.get("requests"), list) and len(got["requests"]) >= 2:
            got["complexity"] = "complex"
        ok = {field: got[field] == expected[field] for field in FIELDS}
        ok["route"] = route(got) == route(expected)
        rows.append({"t": t, "expected": expected, "got": got, "ok": ok, "seconds": seconds,
                     "parse_error": parse_error, "model_sensitive": model_sensitive, "rule": rule,
                     "upgraded": model_complexity != got["complexity"]})
        misses = [f"{f}: {show(expected[f])}→{show(got[f])}" for f in FIELDS if not ok[f]]
        status = "OK  " if not misses else "MISS"
        flag = "  [injection rule]" if rule else ""
        print(f"{t['id']:>4}  {status}  {seconds:5.1f} s  {'; '.join(misses)}{flag}")

    n = len(rows)
    correct = {f: sum(r["ok"][f] for r in rows) for f in FIELDS + ["route"]}
    all_ok = sum(all(r["ok"][f] for f in FIELDS) for r in rows)
    missed_sensitive = [r for r in rows if r["expected"]["sensitive"] and not r["got"]["sensitive"]]
    over_escalated = [r for r in rows if not r["expected"]["sensitive"] and r["got"]["sensitive"]]
    n_sensitive = sum(r["expected"]["sensitive"] for r in rows)
    model_missed = [r for r in rows if r["expected"]["sensitive"] and not r["model_sensitive"]]
    rule_caught = [r for r in model_missed if r["rule"]]
    rule_fired = [r for r in rows if r["rule"]]
    rule_false = [r for r in rule_fired if not r["expected"]["sensitive"]]
    parse_errors = sum(r["parse_error"] for r in rows)
    times = [r["seconds"] for r in rows]

    lines = [
        "# Classifier evaluation",
        "",
        f"- **Date:** {datetime.now():%Y-%m-%d %H:%M}",
        f"- **Model:** `{MODEL}` via Ollama, `think: false`, temperature 0, JSON schema",
        f"- **Prompt version:** `{prompt_id}` (first 8 characters of the SHA-256 of the system prompt)",
        f"- **Tickets:** {n}, written for this evaluation and not used in the prompt; expected labels set before the run",
        "",
        "## Results",
        "",
        "| Field | Correct |",
        "|---|---|",
    ]
    for f in FIELDS:
        lines.append(f"| {f} | {pct(correct[f], n)} |")
    lines += [
        f"| **route** (human / cloud / local) | **{pct(correct['route'], n)}** |",
        f"| all four labels right | {pct(all_ok, n)} |",
        "",
        "## Safety",
        "",
        f"- **Sensitive tickets missed: {len(missed_sensitive)} of {n_sensitive}.** These would have reached a model lane instead of a person.",
        f"- **Model alone missed {len(model_missed)} of {n_sensitive}**; the injection rule caught {len(rule_caught)} of those.",
        f"- **Injection rule fired on {len(rule_fired)} tickets**, {len(rule_false)} of them not expected to be sensitive (false alarms).",
        f"- **Over-escalated:** {len(over_escalated)} of {n - n_sensitive} non-sensitive tickets were sent to a person (safe, but costs reviewer time).",
        f"- **Requests rule:** upgraded {sum(r['upgraded'] for r in rows)} tickets from simple to complex (2+ separate requests).",
        f"- **Unparseable outputs:** {parse_errors} (each one fails closed to a person)",
        "",
        "## Latency",
        "",
        f"- **Median:** {statistics.median(times):.1f} s · **worst:** {max(times):.1f} s per ticket on this machine",
        "",
        "## Misses",
        "",
    ]
    miss_rows = [(r, f) for r in rows for f in FIELDS if not r["ok"][f]]
    if miss_rows:
        lines += ["| Ticket | Field | Expected | Got | Message |", "|---|---|---|---|---|"]
        for r, f in miss_rows:
            lines.append(f"| {r['t']['id']} | {f} | {show(r['expected'][f])} | {show(r['got'][f])} | {cell(r['t']['message'])} |")
    else:
        lines.append("None.")
    (RESULTS_DIR / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with open(RESULTS_DIR / "latest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id"] + [f"expected_{x}" for x in FIELDS] + [f"got_{x}" for x in FIELDS]
                   + ["expected_route", "got_route", "model_sensitive", "injection_rule", "requests", "seconds", "parse_error", "summary"])
        for r in rows:
            w.writerow([r["t"]["id"]] + [r["expected"][x] for x in FIELDS] + [r["got"][x] for x in FIELDS]
                       + [route(r["expected"]), route(r["got"]), r["model_sensitive"], r["rule"],
                          " | ".join(r["got"].get("requests") or []), r["seconds"], r["parse_error"],
                          r["got"].get("summary", "")])

    print()
    print(f"Route correct: {pct(correct['route'], n)} · all labels right: {pct(all_ok, n)}")
    print(f"Sensitive missed: {len(missed_sensitive)} of {n_sensitive} (model alone: {len(model_missed)}) · "
          f"over-escalated: {len(over_escalated)} · injection rule fired: {len(rule_fired)}")
    print(f"Latency: median {statistics.median(times):.1f} s, worst {max(times):.1f} s")
    print("Saved eval/results/latest.md and eval/results/latest.csv")


if __name__ == "__main__":
    main()
