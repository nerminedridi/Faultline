"""Command line for the root-cause agent.

    python -m agent investigate                      investigate whatever is wrong right now
    python -m agent investigate -c "checkout slow"   with a specific complaint
    python -m agent investigate --provider ollama    use a local model instead of Gemini

Settings come from .env at the repo root (or the environment): LLM_PROVIDER (gemini | ollama),
GEMINI_API_KEY, GEMINI_MODEL, OLLAMA_MODEL, OLLAMA_URL.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_COMPLAINT = "Something may be wrong with the shop. Check whether customers are affected."


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader: KEY=value lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cmd_investigate(args: argparse.Namespace) -> int:
    from agent import llm as llm_mod
    from agent.investigator import dumps, investigate

    model = llm_mod.from_env(args.provider, args.model)
    print(f"Investigating with {type(model).__name__.lower()} / {model.model}")
    report = investigate(model, args.complaint, minutes=args.minutes, max_steps=args.max_steps)
    report["provider"], report["model"] = type(model).__name__.lower(), model.model

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{report['provider']}.json"
    path.write_text(dumps(report) + "\n", encoding="utf-8")

    d = report["diagnosis"]
    print()
    if d is None:
        print("No diagnosis submitted.")
    else:
        print(f"Root cause: {d['service']} / {d['kind']}  (confidence: {d.get('confidence', '?')})")
        print(f"  {d['summary']}")
        for item in d.get("evidence", []):
            print(f"  - {item}")
    u = report["usage"]
    print(f"\n{report['tool_calls']} tool calls, {u['llm_calls']} model calls, "
          f"{u['input_tokens']:,} input / {u['output_tokens']:,} output tokens, {report['duration_s']} s")
    print(f"Report: {path.relative_to(ROOT)}")
    return 0 if d else 1


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m agent", description="Faultline root-cause agent")
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("investigate", help="find the root cause of what is happening now")
    inv.add_argument("-c", "--complaint", default=DEFAULT_COMPLAINT, help="what the page or user reported")
    inv.add_argument("-m", "--minutes", type=int, default=10, help="how far back to look (default 10)")
    inv.add_argument("--max-steps", type=int, default=15, help="tool-call budget (default 15)")
    inv.add_argument("--provider", choices=["gemini", "ollama"], help="overrides LLM_PROVIDER")
    inv.add_argument("--model", help="overrides GEMINI_MODEL / OLLAMA_MODEL")
    inv.set_defaults(fn=cmd_investigate)

    args = parser.parse_args()
    from agent.llm import LLMError
    try:
        return args.fn(args)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
