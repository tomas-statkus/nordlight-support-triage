#!/usr/bin/env python3
"""Evaluate top-k retrieval against the shipped knowledge base using local Ollama embeddings.

Usage (from the repo folder, with Ollama running and nomic-embed-text installed):
    python3 eval/run_retrieval_eval.py
"""
import argparse
import csv
import json
import math
import pathlib
import re
import statistics
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
MODEL = "nomic-embed-text:latest"
ARTICLE_PATTERN = re.compile(
    r"\{\s*title:\s*'((?:\\.|[^'\\])*)',\s*text:\s*'((?:\\.|[^'\\])*)'\s*\}",
    re.DOTALL,
)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def decode_js_string(value):
    return re.sub(r"\\(['\\])", r"\1", value)


def parse_articles(code):
    start = code.find("const articles = [")
    end = code.find("\n];", start)
    if start < 0 or end < 0:
        raise ValueError("Could not find the article array in the KB loader workflow")

    array_text = code[start + len("const articles = ["):end]
    objects = re.findall(r"\{[^{}]*\}", array_text)
    articles = []
    for article in objects:
        match = ARTICLE_PATTERN.fullmatch(article)
        if match is None:
            raise ValueError("KB article format changed; update the retrieval evaluator parser")
        articles.append({
            "title": decode_js_string(match.group(1)),
            "text": decode_js_string(match.group(2)),
        })
    if not articles:
        raise ValueError("No KB articles found in the KB loader workflow")
    titles = [article["title"] for article in articles]
    if len(titles) != len(set(titles)):
        raise ValueError("KB article titles must be unique")
    return articles


def load_articles():
    workflow = json.loads((ROOT / "workflows/kb-loader.json").read_text(encoding="utf-8"))
    node = next((node for node in workflow["nodes"] if node.get("name") == "KB articles"), None)
    if node is None:
        raise ValueError("Could not find the KB articles node in workflows/kb-loader.json")
    return parse_articles(node["parameters"]["jsCode"])


def load_cases(articles):
    with (RESULTS_DIR / "results_v3.csv").open(newline="", encoding="utf-8") as handle:
        summaries = {row["id"]: row["summary"] for row in csv.DictReader(handle)}
    article_titles = {article["title"] for article in articles}
    cases = []
    with (DATA_DIR / "retrieval_cases.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            expected = [title.strip() for title in row["expected_titles"].split("|") if title.strip()]
            if not expected or not set(expected) <= article_titles:
                raise ValueError(f"{row['id']} has missing or unknown expected article titles")
            query = summaries.get(row["id"], "").strip()
            if not query:
                raise ValueError(f"No v3 classifier summary found for {row['id']}")
            cases.append({"id": row["id"], "query": query, "expected": expected})
    if not cases:
        raise ValueError("No retrieval cases found")
    return cases


def embed(texts):
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({"model": MODEL, "input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with OPENER.open(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise ValueError("Ollama returned an unexpected embedding response")
    if any(not isinstance(vector, list) or not vector for vector in embeddings):
        raise ValueError("Ollama returned an empty or invalid embedding")
    return embeddings


def cosine_similarity(left, right):
    if len(left) != len(right):
        raise ValueError("Embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        raise ValueError("Ollama returned a zero-length embedding")
    return dot / (left_norm * right_norm)


def rank_articles(query_embedding, articles, article_embeddings, top_k):
    if len(articles) != len(article_embeddings):
        raise ValueError("Article and embedding counts do not match")
    scored = [
        (cosine_similarity(query_embedding, embedding), index, article["title"])
        for index, (article, embedding) in enumerate(zip(articles, article_embeddings))
    ]
    scored.sort(key=lambda result: (-result[0], result[1]))
    return [title for _, _, title in scored[:top_k]]


def evaluate(cases, articles, article_embeddings, top_k):
    rows = []
    for case in cases:
        start = time.perf_counter()
        query_embedding = embed([case["query"]])[0]
        retrieved = rank_articles(query_embedding, articles, article_embeddings, top_k)
        matched = set(case["expected"]) & set(retrieved)
        rows.append({
            "id": case["id"],
            "expected_titles": " | ".join(case["expected"]),
            "retrieved_titles": " | ".join(retrieved),
            "matched_titles": " | ".join(title for title in case["expected"] if title in matched),
            "article_recall": f"{len(matched) / len(case['expected']):.3f}",
            "hit_at_k": bool(matched),
            "seconds": f"{time.perf_counter() - start:.2f}",
            "top_k": top_k,
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=3, help="number of articles to retrieve (default: 3)")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")

    try:
        articles = load_articles()
        cases = load_cases(articles)
        print(f"Warming up {MODEL} (not timed)...")
        embed(["retrieval evaluation warmup"])
        article_embeddings = embed([article["text"] for article in articles])
        rows = evaluate(cases, articles, article_embeddings, args.top_k)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        sys.exit(f"Retrieval evaluation failed: {error}")

    ticket_hits = sum(row["hit_at_k"] for row in rows)
    matched_articles = sum(
        len(row["matched_titles"].split(" | ")) if row["matched_titles"] else 0
        for row in rows
    )
    expected_articles = sum(len(row["expected_titles"].split(" | ")) for row in rows)
    times = [float(row["seconds"]) for row in rows]

    output = RESULTS_DIR / "retrieval_results.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Ticket hit@{args.top_k}: {ticket_hits}/{len(rows)} ({round(100 * ticket_hits / len(rows))}%)")
    print(f"Article recall@{args.top_k}: {matched_articles}/{expected_articles} "
          f"({round(100 * matched_articles / expected_articles)}%)")
    print(f"Query latency: median {statistics.median(times):.2f} s · worst {max(times):.2f} s")
    print(f"Saved {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
