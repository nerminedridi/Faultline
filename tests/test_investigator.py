import time
import unittest
from unittest import mock

from agent import investigator
from agent.llm import Reply, ToolCall

GOOD = {
    "service": "payments", "kind": "errors", "summary": "payments returns 503",
    "evidence": ["payments logs 503"], "ruled_out": ["inventory: healthy"], "confidence": "high",
}


class ScriptedLLM:
    """Plays back a fixed list of replies and records what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, history, tools, only=None):
        self.calls.append({"only": only, "turns": len(history)})
        return self.replies.pop(0)


def call(name, **args):
    return Reply("", [ToolCall(name, args)])


def investigate(llm, **kwargs):
    with mock.patch.object(investigator.tools, "run", return_value="(tool output)"):
        return investigator.investigate(llm, "test page", window_start=time.time() - 120,
                                        echo=lambda *_: None, **kwargs)


class InvestigatorTest(unittest.TestCase):
    def test_tools_then_diagnosis(self):
        llm = ScriptedLLM(call("search_logs", service="payments"), call("submit_diagnosis", **GOOD))
        report = investigate(llm)
        self.assertEqual(report["diagnosis"]["service"], "payments")
        self.assertEqual(report["tool_calls"], 1)
        self.assertEqual(report["usage"]["llm_calls"], 2)

    def test_diagnosis_without_ruled_out_is_sent_back(self):
        lazy = {**GOOD, "ruled_out": []}
        llm = ScriptedLLM(call("submit_diagnosis", **lazy), call("submit_diagnosis", **GOOD))
        report = investigate(llm)
        self.assertEqual(report["diagnosis"]["ruled_out"], GOOD["ruled_out"])
        rejection = report["transcript"][2]["results"][0]["result"]
        self.assertIn("ruled_out is empty", rejection)

    def test_unknown_kind_is_sent_back(self):
        llm = ScriptedLLM(call("submit_diagnosis", **{**GOOD, "kind": "gremlins"}),
                          call("submit_diagnosis", **GOOD))
        self.assertEqual(investigate(llm)["diagnosis"]["kind"], "errors")

    def test_out_of_steps_forces_a_diagnosis(self):
        llm = ScriptedLLM(call("get_alerts"), call("get_alerts"), call("submit_diagnosis", **GOOD))
        report = investigate(llm, max_steps=2)
        self.assertEqual(llm.calls[-1]["only"], "submit_diagnosis")
        self.assertIsNotNone(report["diagnosis"])

    def test_gives_up_after_repeated_chatter(self):
        chatter = Reply("I think it might be payments.", [])
        report = investigate(ScriptedLLM(chatter, chatter, chatter))
        self.assertIsNone(report["diagnosis"])


if __name__ == "__main__":
    unittest.main()
