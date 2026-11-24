"""Local OpenAI-compatible JSON completions with bounded retries and usage."""
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

load_dotenv()

MAX_OUTPUT_TOKENS = 768
MODEL_CONTEXT_TOKENS = 8192
REPEAT_PENALTY = 1.0


class LocalLLMClient:
    def __init__(self, model: str | None = None, timeout: float = 120,
                 max_output_tokens: int = MAX_OUTPUT_TOKENS):
        self.model = model or os.getenv("LLM_MODEL", "qwen3:4b")
        self.base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        parsed = urlsplit(self.base_url)
        self.native_url = f"{parsed.scheme}://{parsed.netloc}/api/chat"
        self.api_key = os.getenv("LLM_API_KEY", "ollama")
        self.max_output_tokens = max_output_tokens
        self.client = httpx.Client(timeout=timeout, trust_env=False,
                                   headers={"Authorization": f"Bearer {self.api_key}"})

    def connectivity(self) -> dict:
        response = self.client.get(f"{self.base_url}/models")
        response.raise_for_status()
        names = {m["id"] for m in response.json().get("data", [])}
        if self.model not in names:
            raise RuntimeError(f"Configured local model {self.model} is unavailable")
        completion = self.complete("Reply using a JSON object.", "What is 1 plus 1? Give the result in the field value.")
        if not isinstance(completion["data"], dict) or not completion["data"]:
            raise RuntimeError("Local model did not return a JSON object")
        return {"model": self.model, "model_available": True, "chat_ok": True,
                "usage": completion["usage"]}

    def complete(self, system: str, user: str) -> dict:
        failures = (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                    httpx.RemoteProtocolError)
        start = time.monotonic()
        for attempt in range(2):
            try:
                response = self.client.post(self.native_url, json={
                    "model": self.model, "stream": False, "format": "json", "think": False,
                    "options": {"temperature": 0, "num_predict": self.max_output_tokens,
                                "num_ctx": MODEL_CONTEXT_TOKENS,
                                "repeat_penalty": REPEAT_PENALTY},
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                })
                response.raise_for_status()
                payload = response.json()
                data = json.loads(payload["message"]["content"] or "{}")
                prompt_tokens = int(payload.get("prompt_eval_count") or 0)
                completion_tokens = int(payload.get("eval_count") or 0)
                return {"data": data, "latency_s": time.monotonic() - start,
                        "usage": {"prompt_tokens": prompt_tokens,
                                  "completion_tokens": completion_tokens,
                                  "total_tokens": prompt_tokens + completion_tokens},
                        "calls": attempt + 1}
            except httpx.HTTPStatusError as exc:
                exc.attempts = attempt + 1
                # Repeating the same deterministic request cannot repair this
                # Ollama generation failure.
                repeat_abort = "token repeat limit" in exc.response.text.lower()
                if repeat_abort or exc.response.status_code not in (429, 500, 502, 503, 504) or attempt == 1:
                    raise
            except failures as exc:
                exc.attempts = attempt + 1
                if attempt == 1:
                    raise
            time.sleep(min(4, 2 ** attempt))
        raise RuntimeError("Local inference failed after bounded retries")


def credentials_available() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))
