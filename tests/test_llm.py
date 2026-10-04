import io
import unittest
import urllib.error
from unittest import mock

from agent import llm
from agent.llm import Gemini, Reply, ToolCall


def http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


class RetryTest(unittest.TestCase):
    def test_daily_quota_fails_fast_instead_of_waiting_hours(self):
        body = '{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}, ' \
               '{"retryDelay": "43443s"}]}}'
        with mock.patch("urllib.request.urlopen", side_effect=http_error(429, body)), \
                mock.patch("time.sleep") as sleep:
            with self.assertRaisesRegex(llm.LLMError, "quota exhausted"):
                llm._post("https://x", {}, {}, timeout=1)
        sleep.assert_not_called()

    def test_per_minute_limit_waits_the_suggested_delay_then_retries(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value = io.BytesIO(b'{"ok": true}')
        errors = [http_error(429, '{"retryDelay": "7s"}'), ok]
        with mock.patch("urllib.request.urlopen", side_effect=errors), mock.patch("time.sleep") as sleep:
            self.assertEqual(llm._post("https://x", {}, {}, timeout=1), {"ok": True})
        sleep.assert_called_once_with(8.0)

    def test_client_errors_are_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400, "bad")) as urlopen:
            with self.assertRaises(llm.LLMError):
                llm._post("https://x", {}, {}, timeout=1)
        self.assertEqual(urlopen.call_count, 1)


class GeminiFormatTest(unittest.TestCase):
    def test_model_turns_are_replayed_verbatim(self):
        # Gemini attaches thought signatures to its parts; they must come back unchanged.
        raw = {"role": "model", "parts": [{"functionCall": {"name": "get_alerts", "args": {}},
                                           "thoughtSignature": "sig"}]}
        turn = {"role": "assistant", "reply": Reply("", [ToolCall("get_alerts", {})], raw=raw)}
        self.assertIs(Gemini._content(turn), raw)

    def test_tool_results_become_function_responses(self):
        turn = {"role": "tool", "results": [(ToolCall("get_alerts", {}, id="c1"), "none"),
                                             (ToolCall("log_summary", {}), "lines")]}
        parts = Gemini._content(turn)["parts"]
        self.assertEqual(parts[0]["functionResponse"],
                         {"name": "get_alerts", "response": {"result": "none"}, "id": "c1"})
        self.assertNotIn("id", parts[1]["functionResponse"])

    def test_missing_key_is_a_clear_error(self):
        with self.assertRaisesRegex(llm.LLMError, "GEMINI_API_KEY"):
            Gemini("gemini-test", "")


if __name__ == "__main__":
    unittest.main()
