"""Minimal client for any OpenAI-compatible chat completions API (Groq, OpenAI, Gemini, Ollama...)."""

from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self, api_key: str, base_url: str, model: str, timeout: float = 30, client: httpx.AsyncClient | None = None
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def json_chat(self, messages: list[dict], model: str | None = None, max_tokens: int = 900) -> dict:
        """Ask for a JSON object and return it parsed. Raises LLMError on any failure."""
        body = {
            "model": model or self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            r = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if r.status_code == 400 and "response_format" in r.text:
                body.pop("response_format")  # some models/providers don't support JSON mode
                r = await self._client.post(
                    f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.api_key}"}
                )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc
        return extract_json(content)


def extract_json(text: str) -> dict:
    """Parse a JSON object, tolerating ```json fences or text around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("LLM did not return JSON") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LLMError("LLM JSON is not an object")
    return value
