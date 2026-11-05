"""Fixed evaluation definitions and rank scoring; no user data or network."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evaluate_retrieval", ROOT / "scripts/evaluate_retrieval.py")
EVALUATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATION)


class EvaluationTests(unittest.TestCase):
    def test_rank_metrics_count_relevant_topics_once(self):
        self.assertEqual(EVALUATION.score(["x", "a", "a", "z", "b"], ["a", "b"]),
                         {"recall_at_5": 1, "mrr_at_5": .5})
        self.assertEqual(EVALUATION.score(["x"] * 5 + ["a"], ["a"]),
                         {"recall_at_5": 0, "mrr_at_5": 0})

    def test_empty_query_case_has_separate_oracle(self):
        self.assertEqual(EVALUATION.score([], []),
                         {"recall_at_5": None, "mrr_at_5": None, "empty_correct": True})
        self.assertFalse(EVALUATION.score(["unrelated"], [])["empty_correct"])

    def test_fixed_queries_reference_the_frozen_corpus(self):
        corpus = json.loads((ROOT / "evaluation/corpus.json").read_text())
        suite = json.loads((ROOT / "evaluation/queries.json").read_text())
        paths = {topic["path"] for topic in corpus["manual_topics"]} | {fact["topic"] for fact in corpus["facts"]}
        self.assertEqual(len(paths), 15)
        self.assertEqual(len(suite["queries"]), 12)
        self.assertEqual(len({query["id"] for query in suite["queries"]}), 12)
        self.assertEqual(sum(bool(query["relevant"]) for query in suite["queries"]), 11)
        self.assertTrue(all(set(query["relevant"]) <= paths for query in suite["queries"]))
        self.assertEqual(suite["k"], 5)
        self.assertEqual(suite["budget_bytes"], 6000)
        self.assertEqual(EVALUATION.BASELINE, "2ac1ee2411e8345ec6043bab907a4b6a5b25af5f")


if __name__ == "__main__":
    unittest.main()
