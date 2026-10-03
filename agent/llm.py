"""LLM providers behind one small interface, using only the standard library.

The investigator keeps a provider-neutral history:
    {"role": "user", "text": str}
    {"role": "assistant", "reply": Reply}
    {"role": "tool", "results": [(ToolCall, str)]}
and each provider translates it to its own wire format.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = ""  # set when the provider pairs calls and results by id


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    raw: Any = None  # the provider's own message, replayed verbatim (Gemini thought signatures)
    usage: dict = field(default_factory=dict)


MAX_RETRY_WAIT = 120  # seconds; per-minute rate limits clear well within this


class LLMError(RuntimeError):
    pass


def _post(url: str, body: dict, headers: dict, timeout: float, retries: int = 5) -> dict:
    """POST JSON, retrying rate limits and transient server errors."""
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise LLMError(f"HTTP {exc.code}: {detail[:500]}") from exc
            # Gemini says how long to back off ("retryDelay": "23s"); otherwise back off exponentially.
            hint = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail)
            wait = float(hint.group(1)) + 1 if hint else 2 ** (attempt + 1)
            if "PerDay" in detail or wait > MAX_RETRY_WAIT:
                # A daily quota resets in hours: waiting it out would look like a hang.
                raise LLMError(f"model API quota exhausted (retry in {wait / 3600:.1f} h): {detail[:300]}")
            print(f"  [{exc.code} from model API, retrying in {wait:.0f} s]", flush=True)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise LLMError(f"cannot reach {url.split('?')[0]}: {exc}") from exc
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


class Gemini:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str, api_key: str):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (put it in .env)")
        self.model, self._key = model, api_key

    def chat(self, system: str, history: list[dict], tools: list[dict], only: str | None = None) -> Reply:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [self._content(turn) for turn in history],
            "tools": [{"functionDeclarations": tools}],
        }
        if only:  # force a call to this one tool
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [only]}}
        resp = _post(self.URL.format(model=self.model), body, {"x-goog-api-key": self._key}, timeout=180)

        candidate = (resp.get("candidates") or [{}])[0]
        content = candidate.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts", [])
        calls = [
            ToolCall(p["functionCall"]["name"], p["functionCall"].get("args", {}), p["functionCall"].get("id", ""))
            for p in parts if "functionCall" in p
        ]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not parts:
            text = f"[no content, finishReason={candidate.get('finishReason')}]"
        meta = resp.get("usageMetadata", {})
        usage = {
            "input_tokens": meta.get("promptTokenCount", 0),
            "output_tokens": meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0),
        }
        return Reply(text.strip(), calls, raw={"role": "model", "parts": parts or [{"text": text}]}, usage=usage)

    @staticmethod
    def _content(turn: dict) -> dict:
        if turn["role"] == "user":
            return {"role": "user", "parts": [{"text": turn["text"]}]}
        if turn["role"] == "assistant":
            return turn["reply"].raw
        parts = []
        for call, result in turn["results"]:
            response = {"name": call.name, "response": {"result": result}}
            if call.id:
                response["id"] = call.id
            parts.append({"functionResponse": response})
        return {"role": "user", "parts": parts}


class Ollama:
    def __init__(self, model: str, url: str):
        self.model, self.url = model, url.rstrip("/")

    def chat(self, system: str, history: list[dict], tools: list[dict], only: str | None = None) -> Reply:
        if only:  # no forced tool choice in Ollama: offer just that tool
            tools = [t for t in tools if t["name"] == only]
        messages = [{"role": "system", "content": system}]
        for turn in history:
            if turn["role"] == "user":
                messages.append({"role": "user", "content": turn["text"]})
            elif turn["role"] == "assistant":
                reply = turn["reply"]
                messages.append({
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": [{"function": {"name": c.name, "arguments": c.args}} for c in reply.tool_calls],
                })
            else:
                for call, result in turn["results"]:
                    messages.append({"role": "tool", "content": result, "tool_name": call.name})
        body = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": t} for t in tools],
            "stream": False,
            # Ollama's default context (a few thousand tokens) would silently cut off log dumps.
            "options": {"num_ctx": 32768},
        }
        resp = _post(f"{self.url}/api/chat", body, {}, timeout=1800, retries=1)
        msg = resp.get("message", {})
        calls = [
            ToolCall(c["function"]["name"], c["function"].get("arguments") or {})
            for c in msg.get("tool_calls") or []
        ]
        # Reasoning models wrap their thinking in <think> tags; keep only the answer.
        text = re.sub(r"<think>.*?</think>", "", msg.get("content", ""), flags=re.S).strip()
        usage = {"input_tokens": resp.get("prompt_eval_count", 0), "output_tokens": resp.get("eval_count", 0)}
        return Reply(text, calls, usage=usage)


def from_env(provider: str | None = None, model: str | None = None):
    provider = provider or os.getenv("LLM_PROVIDER", "gemini")
    if provider == "gemini":
        return Gemini(model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), os.getenv("GEMINI_API_KEY", ""))
    if provider == "ollama":
        return Ollama(model or os.getenv("OLLAMA_MODEL", "qwen3:4b"), os.getenv("OLLAMA_URL", "http://localhost:11434"))
    raise LLMError(f"unknown LLM_PROVIDER {provider!r} (use gemini or ollama)")
