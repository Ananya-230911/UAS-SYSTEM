"""Minimal Groq client: one chat call that must come back as JSON matching
a Pydantic model (docs/CBBA_PLAN.md section 6).

Groq's API is OpenAI-compatible, so this is a plain HTTPS POST with httpx
(already a dependency) -- no extra SDK. Optional by design: with no
GROQ_API_KEY every caller falls back to deterministic rules.

What it guarantees to callers:
  - no key, network error, timeout, 401/403/429/5xx  -> ProviderUnavailable
  - an answer that isn't valid JSON for the model     -> MalformedProviderOutput
  - otherwise a validated instance of the model.
It never retries in a loop and never sees anything it isn't sent.
"""
from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, ValidationError

from app.mission.goal_parser import MalformedProviderOutput, ProviderUnavailable

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq retired the Llama 3.x free-tier models on 16 Aug 2026 (see
# docs/CBBA_PLAN.md section 6); gpt-oss-20b is a production model. Override
# with GROQ_MODEL -- check console.groq.com/docs/models for what's current.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"


def groq_configured() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


class GroqJsonClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, http: httpx.Client | None = None, timeout_s: float = 20.0):
        self.api_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")
        self.model = model or os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
        self._http = http
        self.timeout_s = timeout_s

    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if not self.api_key:
            raise ProviderUnavailable("GROQ_API_KEY is not set")
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": system
                    + "\n\nAnswer with ONE JSON object only, matching this JSON schema:\n"
                    + str(schema.model_json_schema()),
                },
                {"role": "user", "content": user},
            ],
        }
        http = self._http or httpx.Client(timeout=self.timeout_s)
        try:
            resp = http.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"could not reach Groq: {type(exc).__name__}") from exc
        finally:
            if self._http is None:
                http.close()
        if resp.status_code != 200:
            # 401/403 bad key, 404 unknown model, 429 rate limit, 5xx outage:
            # all mean "use the rules instead", never "guess".
            raise ProviderUnavailable(f"Groq returned HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            return schema.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise MalformedProviderOutput(f"Groq's answer did not match the expected format: {exc}") from exc
