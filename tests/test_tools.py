import time
import unittest

from agent import tools


class IncidentWindowTest(unittest.TestCase):
    def setUp(self):
        tools.open_window(time.time() - 300)  # incident started 5 minutes ago

    def test_lookback_never_reaches_before_the_window(self):
        self.assertEqual(tools._since(None), tools.window_start)
        self.assertEqual(tools._since(60), tools.window_start)  # asked for an hour, gets 5 min
        self.assertGreater(tools._since(2), tools.window_start)  # narrower than the window is fine

    def test_range_selectors_must_fit_the_window(self):
        tools._check_promql("rate(http_requests_total[1m])")
        tools._check_promql("rate(http_requests_total[4m])")
        with self.assertRaisesRegex(tools.ToolError, "before the incident window"):
            tools._check_promql("rate(http_requests_total[10m])")
        with self.assertRaisesRegex(tools.ToolError, "before the incident window"):
            tools._check_promql("max_over_time(up[1h:1m])")  # subqueries too

    def test_offset_is_refused(self):
        with self.assertRaisesRegex(tools.ToolError, "offset"):
            tools._check_promql("rate(http_requests_total[1m] offset 10m)")

    def test_a_metric_named_like_offset_is_fine(self):
        tools._check_promql("kafka_consumer_offset_lag")


class FormattingTest(unittest.TestCase):
    def test_json_line_shows_fields_and_exception_tail(self):
        line = ('{"ts": "2026-10-03T10:00:00.123+00:00", "level": "error", "service": "orders", '
                '"msg": "unhandled error", "request_id": "abc", '
                '"exception": "Traceback\\n  File x\\npsycopg_pool.PoolTimeout: no connection"}')
        out = tools._format_line("orders", "0", line)
        self.assertIn("10:00:00.123 orders error 'unhandled error' request_id=abc", out)
        self.assertIn("PoolTimeout: no connection", out)
        self.assertNotIn("Traceback", out)

    def test_plain_text_line_is_kept_raw(self):
        out = tools._format_line("postgres", str(int(1e18)), "LOG:  checkpoint starting")
        self.assertTrue(out.endswith("postgres | LOG:  checkpoint starting"))

    def test_labels_drop_scrape_noise(self):
        metric = {"__name__": "up", "instance": "orders:8000", "job": "shop", "service": "orders"}
        self.assertEqual(tools._labels(metric), 'up{service="orders"}')

    def test_logql_strings_are_escaped(self):
        self.assertEqual(tools._logql_string('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')


class ChangesTest(unittest.TestCase):
    DECLINES = (("level", "info"), ("msg", "card declined"), ("service", "payments"))
    REQUESTS = (("level", "info"), ("msg", "request"), ("service", "gateway"), ("status", "200"))
    ERRORS = (("level", "error"), ("msg", "request"), ("service", "orders"), ("status", "500"))

    def test_an_info_level_spike_is_reported(self):
        out = tools._changes({self.DECLINES: 2, self.REQUESTS: 270}, {self.DECLINES: 91, self.REQUESTS: 280}, 1)
        self.assertEqual(len(out), 1)
        self.assertIn("UP", out[0])
        self.assertIn("'card declined'", out[0])

    def test_a_new_error_and_a_drop_are_reported_biggest_first(self):
        out = tools._changes({self.REQUESTS: 270}, {self.REQUESTS: 30, self.ERRORS: 240}, 1)
        self.assertIn("'request'  status=500", out[0])
        self.assertIn("DOWN", out[1])

    def test_steady_traffic_and_rare_lines_are_not_changes(self):
        rare = (("msg", "restocked"), ("service", "inventory"))
        self.assertEqual(tools._changes({self.REQUESTS: 270, rare: 0}, {self.REQUESTS: 250, rare: 2}, 1), [])


class RunTest(unittest.TestCase):
    def test_unknown_tool(self):
        self.assertIn("unknown tool", tools.run("rm_rf", {}))

    def test_bad_arguments_are_reported_not_raised(self):
        self.assertIn("bad arguments", tools.run("trace_request", {"nope": 1}))

    def test_invalid_service_is_reported(self):
        self.assertIn("unknown service", tools.run("search_logs", {"service": "mainframe"}))


if __name__ == "__main__":
    unittest.main()
