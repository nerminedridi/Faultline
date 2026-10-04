import unittest

from chaos.faults import FAULTS
from scoreboard.runner import score

TRUTH = {"service": "postgres", "kind": "lock-contention"}


class ScoreTest(unittest.TestCase):
    def test_fully_right(self):
        self.assertEqual(score(TRUTH, {"service": "postgres", "kind": "lock-contention"}),
                         {"service_ok": True, "kind_ok": True, "correct": True})

    def test_right_service_wrong_kind(self):
        result = score(TRUTH, {"service": "postgres", "kind": "resource-exhaustion"})
        self.assertTrue(result["service_ok"])
        self.assertFalse(result["correct"])

    def test_no_diagnosis_scores_nothing(self):
        self.assertEqual(score(TRUTH, None), {"service_ok": False, "kind_ok": False, "correct": False})


class CatalogTest(unittest.TestCase):
    def test_every_root_cause_is_something_the_agent_can_answer(self):
        from agent.tools import KINDS, SERVICES
        for fault in FAULTS.values():
            self.assertIn(fault.root_cause.service, SERVICES, fault.id)
            self.assertIn(fault.root_cause.kind, KINDS, fault.id)

    def test_there_is_a_holdout_split(self):
        holdout = [f for f in FAULTS.values() if f.holdout]
        self.assertGreaterEqual(len(holdout), 2)
        self.assertLess(len(holdout), len(FAULTS))


if __name__ == "__main__":
    unittest.main()
