import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import page, retrieval_metrics
from run_retrieval_eval import load_articles, load_cases, parse_articles, rank_articles


class RetrievalEvaluationTests(unittest.TestCase):
    def test_parse_articles_reads_kb_code_and_unescapes_quotes(self):
        code = """const articles = [
  { title: 'Returns', text: 'Customer\\'s item can be returned.' },
];
return articles;"""

        self.assertEqual(
            parse_articles(code),
            [{"title": "Returns", "text": "Customer's item can be returned."}],
        )

    def test_cases_use_shipped_kb_and_v3_summaries(self):
        articles = load_articles()

        self.assertEqual(len(load_cases(articles)), 18)

    def test_rank_articles_returns_top_k_by_cosine_similarity(self):
        articles = [{"title": "Returns"}, {"title": "Delivery"}, {"title": "Warranty"}]
        embeddings = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]

        self.assertEqual(rank_articles([0.9, 0.1], articles, embeddings, 2), ["Returns", "Delivery"])

    def test_retrieval_metrics_count_ticket_hits_and_article_recall(self):
        rows = [
            {
                "expected_titles": "Returns | Delivery",
                "retrieved_titles": "Returns | Warranty",
                "seconds": "1.2",
                "top_k": "3",
            },
            {
                "expected_titles": "Warranty",
                "retrieved_titles": "Warranty | Delivery",
                "seconds": "2.2",
                "top_k": "3",
            },
        ]

        self.assertEqual(
            retrieval_metrics(rows),
            {
                "hit_count": 2,
                "ticket_count": 2,
                "hit_pct": 100,
                "matched_articles": 2,
                "expected_articles": 3,
                "recall_pct": 67,
                "median_seconds": 1.7,
                "top_k": "3",
            },
        )

    def test_retrieval_metrics_empty_results_are_not_reported_as_zero_score(self):
        self.assertIsNone(retrieval_metrics([]))

    def test_dashboard_shows_retrieval_metrics_and_local_mirror_caveat(self):
        rows = [{
            "expected_titles": "Returns",
            "retrieved_titles": "Returns | Delivery",
            "seconds": "0.02",
            "top_k": "3",
            "id": "E01",
        }]

        rendered = page("v3", [], rows)

        self.assertIn("Ticket hit@3", rendered)
        self.assertIn("Median query latency", rendered)
        self.assertIn("not a measurement of n8n's live store", rendered)


if __name__ == "__main__":
    unittest.main()
