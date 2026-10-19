import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.__main__ import load_env


class LoadEnvTest(unittest.TestCase):
    def load(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                load_env(path)
                return dict(os.environ)

    def test_the_example_file_loads_cleanly(self):
        env = self.load((Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8"))
        self.assertEqual(env["LLM_PROVIDER"], "gemini")
        self.assertEqual(env["GEMINI_API_KEY"], "")
        self.assertEqual(env["OLLAMA_URL"], "http://localhost:11434")

    def test_quotes_and_comments(self):
        env = self.load('# a comment\nA="x"  # note\nB=y#not-a-comment\n')
        self.assertEqual(env, {"A": "x", "B": "y#not-a-comment"})


if __name__ == "__main__":
    unittest.main()
