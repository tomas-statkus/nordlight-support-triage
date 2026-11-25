import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import console  # noqa: E402


class ConsoleTests(unittest.TestCase):
    def test_workflow_models_reads_local_and_cloud_models_from_exports(self):
        workflows = [console.load_workflow(console.TRIAGE_WORKFLOW), console.load_workflow(console.KB_WORKFLOW)]

        local, cloud = console.workflow_models(workflows)

        self.assertEqual(local, ["nomic-embed-text:latest", "qwen3.5:4b"])
        self.assertEqual(cloud, ["claude-haiku-4-5-20251001"])

    def test_pipeline_starts_at_trigger_and_marks_cloud_and_review_steps(self):
        steps = console.pipeline_steps(console.load_workflow(console.TRIAGE_WORKFLOW))
        kinds = dict(steps)

        self.assertEqual(steps[0], ("On form submission", "form"))
        self.assertEqual(kinds["Draft (cloud)"], "cloud LLM")
        self.assertEqual(kinds["Classify (Ollama)"], "local LLM")
        self.assertEqual(kinds["Review"], "human review")
        self.assertNotIn("Embeddings Ollama", kinds)

    def test_check_ollama_reports_missing_models(self):
        def fetch(url):
            return {"models": [{"name": "qwen3.5:4b"}]}

        result = console.check_ollama("http://ollama", ["nomic-embed-text:latest", "qwen3.5:4b"], fetch)

        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [("nomic-embed-text:latest", False), ("qwen3.5:4b", True)])
        self.assertIn("nomic-embed-text:latest", result["detail"])

    def test_checks_report_down_instead_of_raising_when_unreachable(self):
        def fetch(url):
            raise ConnectionRefusedError("refused")

        self.assertFalse(console.check_ollama("http://ollama", ["qwen3.5:4b"], fetch)["ok"])
        self.assertFalse(console.check_n8n("http://n8n", fetch)["ok"])


if __name__ == "__main__":
    unittest.main()
